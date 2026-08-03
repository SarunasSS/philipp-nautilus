import asyncio

from dataclasses import dataclass
from dataclasses import field
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from enum import Enum


from nautilus_trader.model.data import Bar
from nautilus_trader.model.data import BarType
from nautilus_trader.model.enums import OrderSide
from nautilus_trader.model.enums import TimeInForce
from nautilus_trader.model.instruments import Instrument
from nautilus_trader.model.orders import Order
from nautilus_trader.model.position import Position

from strategies.base import BaseStrategy
from strategies.base import BaseStrategyConfig
from strategies.base import Stratlet


class TradeDirection(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"


class OrderMode(str, Enum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"


class HTFSweepCISDStrategyConfig(BaseStrategyConfig, frozen=True):
    htf_bar_type: str
    ltf_bar_type: str
    original_bar_type: str
    comparison_tolerance: float = 0.0
    entry_order_type: str = OrderMode.LIMIT.value
    entry_limit_offset: float = 0.0
    entry_order_expire_minutes: int | None = None
    trade_notional: float = 1_000.0
    stop_order_type: str = OrderMode.MARKET.value
    stop_loss_distance_ratio: float = 1.0
    take_profit_multiplier: float = 2.0
    stop_limit_offset: float = 0.0
    signal_cooldown_seconds: float | None = None


@dataclass(frozen=True)
class SignalCandidate:
    swing_price: float
    cisd_level: float


@dataclass
class SignalSideState:
    candidate: SignalCandidate | None = None
    blocked: bool = False


@dataclass
class Entry(Stratlet):
    config: HTFSweepCISDStrategyConfig
    instrument: Instrument
    direction: TradeDirection
    swing_price: float
    cisd_level: float
    last_close: float
    orders: dict[str, Order] = field(default_factory=dict, init=False)
    position: Position | None = field(default=None, init=False)
    stopping: bool = field(default=False, init=False)
    stopped: bool = field(default=False, init=False)

    async def on_start(self) -> None:
        raw_quantity = self.config.trade_notional / (self.last_close * float(self.instrument.multiplier))
        try:
            quantity = self.instrument.make_qty(raw_quantity, round_down=True)
        except ValueError as exc:
            self.strategy.log.warning(f"Skipping entry; trade_notional is too small for instrument quantity: {exc}")
            return

        order_side = OrderSide.BUY if self.direction == TradeDirection.LONG else OrderSide.SELL
        entry_order_type = OrderMode(self.config.entry_order_type.upper())
        if entry_order_type == OrderMode.MARKET:
            entry_order = self.strategy.order_factory.market(
                instrument_id=self.instrument.id,
                order_side=order_side,
                quantity=quantity,
                tags=["HTF_CISD", "ENTRY"],
            )
        else:
            if self.direction == TradeDirection.LONG:
                entry_limit_price = self.instrument.make_price(
                    self.last_close * (1 - self.config.entry_limit_offset),
                )
            else:
                entry_limit_price = self.instrument.make_price(
                    self.last_close * (1 + self.config.entry_limit_offset),
                )

            if self.config.entry_order_expire_minutes is None:
                time_in_force = TimeInForce.GTC
                expire_time = None
            else:
                time_in_force = TimeInForce.GTD
                expire_time = datetime.fromtimestamp(
                    self.strategy.clock.timestamp_ns() / 1_000_000_000,
                    UTC,
                ) + timedelta(minutes=self.config.entry_order_expire_minutes)

            entry_order = self.strategy.order_factory.limit(
                instrument_id=self.instrument.id,
                order_side=order_side,
                quantity=quantity,
                price=entry_limit_price,
                time_in_force=time_in_force,
                expire_time=expire_time,
                tags=["HTF_CISD", "ENTRY"],
            )

        self.orders["entry"] = entry_order
        self.strategy.submit_order(entry_order)
        self.strategy.log.info(
            f"Submitted {self.direction.value} {entry_order_type.value} entry "
            f"{entry_order.client_order_id} qty={quantity}",
        )

        while not self.stopping and not self.strategy.cache.is_order_closed(entry_order.client_order_id):
            await asyncio.sleep(self.poll_delay)
        if self.stopping:
            return

        closed_entry_order = self.strategy.cache.order(entry_order.client_order_id) or entry_order
        if closed_entry_order.filled_qty.as_double() <= 0:
            self.strategy.log.info(f"Entry order {entry_order.client_order_id} closed without fill")
            return

        self.position = self.strategy.cache.position_for_order(entry_order.client_order_id)
        if self.position is None:
            raise RuntimeError(f"No position found for filled entry {entry_order.client_order_id}")

        entry_price = closed_entry_order.avg_px

        if self.direction == TradeDirection.LONG:
            raw_stop_price = self.cisd_level - (
                (self.cisd_level - self.swing_price) * self.config.stop_loss_distance_ratio
            )
            if raw_stop_price >= entry_price:
                raise ValueError("long stop price must be below entry price")

            stop_price = self.instrument.make_price(raw_stop_price)
            stop_distance = entry_price - stop_price.as_double()
            if stop_distance <= 0:
                raise ValueError("long stop distance must be positive")

            take_profit_price = self.instrument.make_price(entry_price + (self.config.take_profit_multiplier * stop_distance))
            stop_limit_price = self.instrument.make_price(
                stop_price.as_double() * (1 - self.config.stop_limit_offset),
            )
            exit_side = OrderSide.SELL
        else:
            raw_stop_price = self.cisd_level + (
                (self.swing_price - self.cisd_level) * self.config.stop_loss_distance_ratio
            )
            if raw_stop_price <= entry_price:
                raise ValueError("short stop price must be above entry price")

            stop_price = self.instrument.make_price(raw_stop_price)
            stop_distance = stop_price.as_double() - entry_price
            if stop_distance <= 0:
                raise ValueError("short stop distance must be positive")

            take_profit_price = self.instrument.make_price(entry_price - (self.config.take_profit_multiplier * stop_distance))
            stop_limit_price = self.instrument.make_price(
                stop_price.as_double() * (1 + self.config.stop_limit_offset),
            )
            exit_side = OrderSide.BUY

        stop_order_type = OrderMode(self.config.stop_order_type.upper())
        if stop_order_type == OrderMode.MARKET:
            stop_order = self.strategy.order_factory.stop_market(
                instrument_id=self.instrument.id,
                order_side=exit_side,
                quantity=closed_entry_order.filled_qty,
                trigger_price=stop_price,
                reduce_only=True,
                tags=["HTF_CISD", "STOP_LOSS"],
            )
        else:
            stop_order = self.strategy.order_factory.stop_limit(
                instrument_id=self.instrument.id,
                order_side=exit_side,
                quantity=closed_entry_order.filled_qty,
                price=stop_limit_price,
                trigger_price=stop_price,
                reduce_only=True,
                tags=["HTF_CISD", "STOP_LOSS"],
            )

        take_profit_order = self.strategy.order_factory.limit(
            instrument_id=self.instrument.id,
            order_side=exit_side,
            quantity=closed_entry_order.filled_qty,
            price=take_profit_price,
            reduce_only=True,
            tags=["HTF_CISD", "TAKE_PROFIT"],
        )
        self.orders["stop_loss"] = stop_order
        self.orders["take_profit"] = take_profit_order
        self.strategy.submit_order_list(
            self.strategy.order_factory.create_list(
                [stop_order, take_profit_order],
            ),
            position_id=self.position.id,
        )
        self.strategy.log.info(
            f"Submitted exits for {entry_order.client_order_id}: "
            f"tp={take_profit_price}, sl={stop_price}",
        )

        while not self.stopping:
            closed_orders = [
                self.strategy.cache.order(order.client_order_id) or order
                for order in (stop_order, take_profit_order)
                if self.strategy.cache.is_order_closed(order.client_order_id)
            ]
            filled_exit_order = next(
                (order for order in closed_orders if order.filled_qty.as_double() > 0),
                None,
            )
            if filled_exit_order is not None:
                break
            if closed_orders:
                raise RuntimeError(f"Exit order {closed_orders[0].client_order_id} closed without fill")

            await asyncio.sleep(self.poll_delay)
        if self.stopping:
            return

        if filled_exit_order.client_order_id == take_profit_order.client_order_id:
            sibling_order = stop_order
        else:
            sibling_order = take_profit_order
        if not self.strategy.cache.is_order_closed(sibling_order.client_order_id):
            self.strategy.cancel_order(self.strategy.cache.order(sibling_order.client_order_id) or sibling_order)

        while not self.strategy.cache.is_order_closed(sibling_order.client_order_id):
            await asyncio.sleep(self.poll_delay)

        while not self.strategy.cache.is_position_closed(self.position.id):
            await asyncio.sleep(self.poll_delay)

        self.strategy.log.info(
            f"Trade exit filled by {filled_exit_order.client_order_id} at {filled_exit_order.avg_px}",
        )

    async def on_stop(self, wait_for_commands: bool = True) -> None:
        if self.stopped:
            return
        if self.stopping:
            while not self.stopped:
                await asyncio.sleep(self.poll_delay)
            return

        self.stopping = True
        try:
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
                    tags=["HTF_CISD", "CLEANUP"],
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
        finally:
            self.stopped = True


class HTFSweepCISDStrategy(BaseStrategy):
    def __init__(self, config: HTFSweepCISDStrategyConfig) -> None:
        super().__init__(config)

        self._ltf_bar_type = BarType.from_str(config.ltf_bar_type)
        self._htf_bar_type = BarType.from_str(config.htf_bar_type)
        self._original_bar_type = BarType.from_str(config.original_bar_type)
        self._validate_config()

        self._htf_bars: list[Bar] = []
        self._ltf_bars: list[Bar] = []
        self._instrument: Instrument | None = None

        self._short = SignalSideState()
        self._long = SignalSideState()

        self._signal_cooldown_until_ns = 0

    def on_start(self) -> None:
        super().on_start()

        self._instrument = self.cache.instrument(self._ltf_bar_type.instrument_id)
        if self._instrument is None:
            raise RuntimeError(f"No instrument found for {self._ltf_bar_type.instrument_id}")

        self.subscribe_bars(self._original_bar_type)
        self.subscribe_bars(self._ltf_bar_type)
        self.subscribe_bars(self._htf_bar_type)

    def on_bar(self, bar: Bar) -> None:
        if bar.bar_type.standard() == self._htf_bar_type.standard():
            self._htf_bars.append(bar)
            del self._htf_bars[:-1]
            self._ltf_bars = [ltf_bar for ltf_bar in self._ltf_bars if ltf_bar.ts_event > bar.ts_event]
            self._short = SignalSideState()
            self._long = SignalSideState()
        elif bar.bar_type.standard() == self._ltf_bar_type.standard():
            last_ltf_bar = bar
            self._ltf_bars.append(last_ltf_bar)
            if self._htf_bars:
                self._evaluate_signal(last_ltf_bar)

        super().on_bar(bar)

    def on_stop(self) -> None:
        self.unsubscribe_bars(self._htf_bar_type)
        self.unsubscribe_bars(self._ltf_bar_type)
        super().on_stop()

    def _validate_config(self) -> None:
        self._validate_bar_types()
        for value, field_name in (
            (self.config.entry_order_type, "entry_order_type"),
            (self.config.stop_order_type, "stop_order_type"),
        ):
            try:
                OrderMode(value.upper())
            except ValueError as exc:
                allowed = ", ".join(order_mode.value for order_mode in OrderMode)
                raise ValueError(f"{field_name} must be one of: {allowed}") from exc

        if not 0 <= self.config.comparison_tolerance <= 1:
            raise ValueError("comparison_tolerance must be between 0 and 1")
        if not 0 <= self.config.entry_limit_offset <= 1:
            raise ValueError("entry_limit_offset must be between 0 and 1")
        if self.config.entry_order_expire_minutes is not None and self.config.entry_order_expire_minutes <= 0:
            raise ValueError("entry_order_expire_minutes must be positive when set")
        if self.config.trade_notional <= 0:
            raise ValueError("trade_notional must be positive")
        if not 0 < self.config.stop_loss_distance_ratio <= 1:
            raise ValueError("stop_loss_distance_ratio must be between 0 and 1")
        if not 0 <= self.config.stop_limit_offset <= 1:
            raise ValueError("stop_limit_offset must be between 0 and 1")
        if self.config.signal_cooldown_seconds is not None and self.config.signal_cooldown_seconds < 0:
            raise ValueError("signal_cooldown_seconds must be non-negative")

    def _validate_bar_types(self) -> None:
        if self._htf_bar_type.instrument_id != self._originial_bar_type.instrument_id:
            raise ValueError("htf_bar_type and original_bar_type must use the same instrument")
        if self._ltf_bar_type.instrument_id != self._originial_bar_type.instrument_id:
                    raise ValueError("ltf_bar_type and original_bar_type must use the same instrument")
        if not self._htf_bar_type.is_composite():
            raise ValueError("htf_bar_type must be passed as a composite bar type")
        if not self._htf_bar_type.is_internally_aggregated():
            raise ValueError("htf_bar_type must be internally aggregated")
        if not self._ltf_bar_type.is_composite():
            raise ValueError("ltf_bar_type must be passed as a composite bar type")
        if not self._ltf_bar_type.is_internally_aggregated():
            raise ValueError("ltf_bar_type must be internally aggregated")
        if not self._original_bar_type.is_externally_aggregated():
            raise ValueError("original_bar_type must be externally aggregated")
        if not self._htf_bar_type.spec.is_time_aggregated() or not self._ltf_bar_type.spec.is_time_aggregated():
            raise ValueError("htf_bar_type and ltf_bar_type must be time-aggregated bars")

        htf_interval_ns = self._htf_bar_type.spec.get_interval_ns()
        ltf_interval_ns = self._ltf_bar_type.spec.get_interval_ns()
        original_interval_ns = self._original_bar_type.spec.get_interval_ns()
        if htf_interval_ns <= ltf_interval_ns:
            raise ValueError("htf_bar_type interval must be greater than ltf_bar_type interval")
        if htf_interval_ns % original_interval_ns != 0:
            raise ValueError("htf_bar_type interval must be an exact multiple of original_bar_type interval")
        if ltf_interval_ns % original_interval_ns != 0:
             raise ValueError("ltf_bar_type interval must be an exact multiple of original_bar_type interval")
        if self._htf_bar_type.composite().standard() != self._original_bar_type.standard():
            raise ValueError("htf_bar_type composite source must match original_bar_type")
        if self._ltf_bar_type.composite().standard() != self._original_bar_type.standard():
             raise ValueError("ltf_bar_type composite source must match original_bar_type")

    def _evaluate_signal(self, last_ltf_bar: Bar) -> None:
        if len(self._ltf_bars) < 3:
            return

        now_ns = self.clock.timestamp_ns()
        if now_ns < self._signal_cooldown_until_ns:
            return

        tolerance = self.config.comparison_tolerance
        center_index = -2
        previous_htf_bar = self._htf_bars[-1]
        left = self._ltf_bars[center_index - 1]
        center = self._ltf_bars[center_index]
        right = self._ltf_bars[center_index + 1]

        # 1. Confirm the latest completed LTF swing and create a pending short CISD setup.
        if (
            not self._short.blocked
            and self._short.candidate is None
            and center.high.as_double() > left.high.as_double() * (1 + tolerance)
            and center.high.as_double() > right.high.as_double() * (1 + tolerance)
            and center.high.as_double() > previous_htf_bar.high.as_double() * (1 + tolerance)
        ):
            cisd_level = None
            index = center_index - 1
            while index >= -len(self._ltf_bars):
                if self._ltf_bars[index].close.as_double() > self._ltf_bars[index].open.as_double() * (1 + tolerance):
                    leftmost_index = index
                    while (
                        leftmost_index - 1 >= -len(self._ltf_bars)
                        and self._ltf_bars[leftmost_index - 1].close.as_double()
                        > self._ltf_bars[leftmost_index - 1].open.as_double() * (1 + tolerance)
                    ):
                        leftmost_index -= 1

                    cisd_level = self._ltf_bars[leftmost_index].open.as_double()
                    break

                index -= 1

            if cisd_level is None:
                self._short.blocked = True
            else:
                self._short.candidate = SignalCandidate(
                    swing_price=center.high.as_double(),
                    cisd_level=cisd_level,
                )

        # 2. Confirm the latest completed LTF swing and create a pending long CISD setup.
        if (
            not self._long.blocked
            and self._long.candidate is None
            and center.low.as_double() < left.low.as_double() * (1 - tolerance)
            and center.low.as_double() < right.low.as_double() * (1 - tolerance)
            and center.low.as_double() < previous_htf_bar.low.as_double() * (1 - tolerance)
        ):
            cisd_level = None
            index = center_index - 1
            while index >= -len(self._ltf_bars):
                if self._ltf_bars[index].close.as_double() < self._ltf_bars[index].open.as_double() * (1 - tolerance):
                    leftmost_index = index
                    while (
                        leftmost_index - 1 >= -len(self._ltf_bars)
                        and self._ltf_bars[leftmost_index - 1].close.as_double()
                        < self._ltf_bars[leftmost_index - 1].open.as_double() * (1 - tolerance)
                    ):
                        leftmost_index -= 1

                    cisd_level = self._ltf_bars[leftmost_index].open.as_double()
                    break

                index -= 1

            if cisd_level is None:
                self._long.blocked = True
            else:
                self._long.candidate = SignalCandidate(
                    swing_price=center.low.as_double(),
                    cisd_level=cisd_level,
                )

        signal_direction: TradeDirection | None = None
        signal_candidate: SignalCandidate | None = None

        # 3. Invalidate or confirm the pending short setup.
        if self._short.candidate is not None:
            if last_ltf_bar.high.as_double() > self._short.candidate.swing_price * (1 + tolerance):
                self._short.candidate = None
                self._short.blocked = True
            elif last_ltf_bar.close.as_double() < self._short.candidate.cisd_level * (1 - tolerance):
                signal_direction = TradeDirection.SHORT
                signal_candidate = self._short.candidate

        # 4. Invalidate or confirm the pending long setup.
        if signal_direction is None and self._long.candidate is not None:
            if last_ltf_bar.low.as_double() < self._long.candidate.swing_price * (1 - tolerance):
                self._long.candidate = None
                self._long.blocked = True
            elif last_ltf_bar.close.as_double() > self._long.candidate.cisd_level * (1 + tolerance):
                signal_direction = TradeDirection.LONG
                signal_candidate = self._long.candidate

        if signal_direction is None or signal_candidate is None:
            return
        if self._instrument is None:
            raise RuntimeError("Instrument is not initialized")

        self.log.info("Short_signal" if signal_direction == TradeDirection.SHORT else "long_signal")
        if self.config.signal_cooldown_seconds is None:
            cooldown_ns = self._htf_bar_type.spec.get_interval_ns()
        else:
            cooldown_ns = int(self.config.signal_cooldown_seconds * 1_000_000_000)
        self._signal_cooldown_until_ns = now_ns + cooldown_ns
        self._short = SignalSideState()
        self._long = SignalSideState()

        entry = Entry(
            strategy=self,
            config=self.config,
            instrument=self._instrument,
            direction=signal_direction,
            swing_price=signal_candidate.swing_price,
            cisd_level=signal_candidate.cisd_level,
            last_close=last_ltf_bar.close.as_double(),
        )
        self._add_stratlet(entry)
