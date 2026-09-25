import asyncio
import math


from dataclasses import dataclass
from dataclasses import field
from datetime import UTC
from datetime import date
from datetime import datetime
from datetime import time
from enum import Enum
from typing import ClassVar
from zoneinfo import ZoneInfo


from nautilus_trader.model.data import Bar
from nautilus_trader.model.orders import Order
from nautilus_trader.model.position import Position

from strategies.base import Stratlet


class TradeDirection(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"


def parse_time(value: str, field_name: str, example: str) -> time:
    try:
        return time.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be an ISO 8601 time such as {example}") from exc


def parse_time_range(
    value: str,
    field_name: str,
    range_example: str,
    time_example: str,
) -> tuple[time, time]:
    parts = value.split("-")
    if len(parts) != 2:
        raise ValueError(f"{field_name} must be an ISO 8601 time range such as {range_example}")

    return (
        parse_time(parts[0].strip(), field_name, time_example),
        parse_time(parts[1].strip(), field_name, time_example),
    )


def parse_timezone(value: str) -> ZoneInfo:
    try:
        return ZoneInfo(value)
    except (KeyError, ValueError) as exc:
        raise ValueError("session_timezone must be an IANA timezone name such as America/New_York") from exc


def seconds_since_midnight(value: time) -> int:
    return (value.hour * 3_600) + (value.minute * 60) + value.second


def typical_price(bar: Bar) -> float:
    return (bar.high.as_double() + bar.low.as_double() + bar.close.as_double()) / 3


def points_to_ticks(points: float, tick_size: float) -> int:
    # make_price rounds to precision, not the price increment, so distances use whole ticks.
    return math.floor((points / tick_size) + 0.5)


def session_local(bar: Bar, timezone: ZoneInfo) -> datetime:
    return datetime.fromtimestamp(bar.ts_event / 1_000_000_000, UTC).astimezone(timezone)


def boundary_ns(session_date: date, boundary: time, timezone: ZoneInfo) -> int:
    return int(datetime.combine(session_date, boundary, tzinfo=timezone).timestamp() * 1_000_000_000)


@dataclass(kw_only=True)
class ManagedEntry(Stratlet):
    cleanup_tag: ClassVar[str]
    orders: dict[str, Order] = field(default_factory=dict, init=False)
    position: Position | None = field(default=None, init=False)
    _closed: bool = field(default=False, init=False)

    async def _on_stop(self, wait_for_commands: bool = True) -> None:
        for order in self.orders.values():
            if self.strategy.cache.is_order_closed(order.client_order_id):
                continue

            self.strategy.cancel_order(self.strategy.cache.order(order.client_order_id) or order)
            if wait_for_commands:
                while not self.strategy.cache.is_order_closed(order.client_order_id):
                    await asyncio.sleep(self.poll_delay)

        entry_order = self.orders.get("entry")
        if self.position is None and entry_order is not None:
            self.position = self.strategy.cache.position_for_order(entry_order.client_order_id)

        if self.position is not None and not self.strategy.cache.is_position_closed(self.position.id):
            self.strategy.close_position(
                self.strategy.cache.position(self.position.id) or self.position,
                tags=[self.cleanup_tag, "CLEANUP"],
            )

        if not wait_for_commands:
            await asyncio.sleep(self.poll_delay)
            return

        if self.position is not None and not self.strategy.cache.is_position_closed(self.position.id):
            while not self.strategy.cache.is_position_closed(self.position.id):
                position = self.strategy.cache.position(self.position.id) or self.position
                closing_order_id = position.closing_order_id
                if closing_order_id is not None and self.strategy.cache.is_order_closed(closing_order_id):
                    closing_order = self.strategy.cache.order(closing_order_id)
                    if closing_order is not None and closing_order.filled_qty.as_double() <= 0:
                        raise RuntimeError(f"Cleanup order {closing_order_id} closed without fill")

                await asyncio.sleep(self.poll_delay)

    async def on_close(self, wait_for_commands: bool = True) -> None:
        if self._closed or (self.stopped and not self._stop_failed):
            return

        self._closed = True
        self.stopping = True
        for order in self.orders.values():
            try:
                if not self.strategy.cache.is_order_closed(order.client_order_id):
                    self.strategy.cancel_order(self.strategy.cache.order(order.client_order_id) or order)
            except Exception as exc:
                self.strategy.log.error(f"Failed to cancel {order.client_order_id} during close: {exc}")

        entry_order = self.orders.get("entry")
        if self.position is None and entry_order is not None:
            try:
                self.position = self.strategy.cache.position_for_order(entry_order.client_order_id)
            except Exception as exc:
                self.strategy.log.error(f"Failed to find position for {entry_order.client_order_id}: {exc}")

        if self.position is not None:
            try:
                if not self.strategy.cache.is_position_closed(self.position.id):
                    self.strategy.close_position(
                        self.strategy.cache.position(self.position.id) or self.position,
                        tags=[self.cleanup_tag, "CLEANUP"],
                    )
            except Exception as exc:
                self.strategy.log.error(f"Failed to close position {self.position.id}: {exc}")
