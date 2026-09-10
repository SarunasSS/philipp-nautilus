import asyncio
import math


from collections.abc import Sequence
from dataclasses import dataclass
from dataclasses import field
from datetime import UTC
from datetime import date
from datetime import datetime
from datetime import time
from enum import Enum
from zoneinfo import ZoneInfo


from nautilus_trader.model.data import Bar
from nautilus_trader.model.data import BarType
from nautilus_trader.model.data import TradeTick
from nautilus_trader.model.enums import OrderSide
from nautilus_trader.model.events import PositionClosed
from nautilus_trader.model.events import PositionEvent
from nautilus_trader.model.instruments import Instrument
from nautilus_trader.model.objects import Price
from nautilus_trader.model.objects import Quantity
from nautilus_trader.model.orders import Order
from nautilus_trader.model.position import Position

from strategies.base import BaseStrategy
from strategies.base import BaseStrategyConfig
from strategies.base import Stratlet


# Three streams of one instrument: the VWAP anchor accumulates on the finest, the signal bar freezes
# it and decides direction, and the execution bar triggers. Sessions are plain calendar days in the
# session timezone, so no overnight anchor is needed.
PERCENT = 100.0
SECONDS_PER_HOUR = 3_600
SECONDS_PER_MINUTE = 60
STRATEGY_TAG = "DRIFT_PULLBACK"
# Ticks carry no interval to measure the accumulation window's first event against, so the guard
# that refuses a half-built session VWAP uses a minute, matching the bar source it replaces.
TICK_VWAP_OPEN_TOLERANCE_SECONDS = 60


class TradeDirection(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"


class VwapSource(str, Enum):
    BARS = "BARS"
    TRADE_TICKS = "TRADE_TICKS"


class DriftPullbackStrategyConfig(BaseStrategyConfig, frozen=True):
    bar_type: str
    signal_bar_type: str
    # Unset when the anchor comes from prints rather than from a bar stream.
    vwap_bar_type: str | None = None
    vwap_source: str = VwapSource.BARS.value
    session_timezone: str = "America/New_York"
    entry_window: str = "10:30-15:30"
    session_cutoff: str = "15:55"
    vwap_window: str = "09:30-16:00"
    # Not from the spec: a charting platform always holds the whole day, but a live process started
    # mid-session would accumulate from wherever it began and call the result a session VWAP.
    require_full_vwap_session: bool = True
    drift_lookback_bars: int = 4
    drift_threshold_pct: float = 0.10
    pullback_window_bars: int = 6
    trade_longs: bool = True
    trade_shorts: bool = True
    long_stop_points: float = 80.0
    long_target_points: float = 40.0
    short_stop_points: float = 80.0
    short_target_points: float = 50.0
    stop_slip_ticks: int = 2
    max_trades_per_day: int = 4
    max_losses_per_day: int = 2
    contracts: int = 1


def _parse_time(value: str, field_name: str) -> time:
    try:
        return time.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be an ISO 8601 time such as 10:30") from exc


def _parse_time_range(value: str, field_name: str) -> tuple[time, time]:
    parts = value.split("-")
    if len(parts) != 2:
        raise ValueError(f"{field_name} must be an ISO 8601 time range such as 10:30-15:30")

    return _parse_time(parts[0].strip(), field_name), _parse_time(parts[1].strip(), field_name)


def _parse_timezone(value: str) -> ZoneInfo:
    try:
        return ZoneInfo(value)
    except (KeyError, ValueError) as exc:
        raise ValueError("session_timezone must be an IANA timezone name such as America/New_York") from exc


def _seconds_since_midnight(value: time) -> int:
    return (value.hour * SECONDS_PER_HOUR) + (value.minute * SECONDS_PER_MINUTE) + value.second


@dataclass
class SessionVwap:
    price_volume: float = 0.0
    volume: float = 0.0
    value: float = 0.0
    # The two snapshots the slope check compares: the anchor at this signal boundary and the one at
    # the boundary before it. They give a cumulative line the same pair of values a moving average
    # gets for free.
    last: float = 0.0
    previous: float = 0.0
    started_at_open: bool = False
    accumulated: bool = False


@dataclass
class ArmedSetup:
    direction: TradeDirection
    bars: int = 0


def _typical_price(bar: Bar) -> float:
    return (bar.high.as_double() + bar.low.as_double() + bar.close.as_double()) / 3


def _drift_pct(closes: Sequence[float], lookback: int) -> float | None:
    # The percentage change of the signal close against the one lookback bars back. None until the
    # history is deep enough, and on a zero reference, which is the spec's divide-by-zero guard.
    if len(closes) <= lookback:
        return None

    reference = closes[-1 - lookback]
    if reference == 0:
        return None

    return (closes[-1] - reference) / reference * PERCENT


def _points_to_ticks(points: float, tick_size: float) -> int:
    # Half up, so 80 points on a 0.25 grid is 320 ticks and a distance landing between two ticks
    # resolves the same way everywhere. make_price rounds to the price precision rather than to the
    # increment, so working in whole ticks is what keeps every derived price on the grid.
    return math.floor((points / tick_size) + 0.5)


def _stop_price(
    instrument: Instrument,
    direction: TradeDirection,
    entry_price: float,
    stop_points: float,
    slip_ticks: int,
) -> Price:
    # The slippage ticks widen the stop rather than modelling a worse fill at the same level, which
    # is what the spec does: the stop both costs and risks the extra distance.
    tick_size = instrument.price_increment.as_double()
    offset = (_points_to_ticks(stop_points, tick_size) + slip_ticks) * tick_size
    if direction == TradeDirection.LONG:
        return instrument.make_price(entry_price - offset)

    return instrument.make_price(entry_price + offset)


def _take_profit_price(
    instrument: Instrument,
    direction: TradeDirection,
    entry_price: float,
    target_points: float,
) -> Price:
    # Targets carry no slippage: a limit fills at its price or not at all.
    tick_size = instrument.price_increment.as_double()
    offset = _points_to_ticks(target_points, tick_size) * tick_size
    if direction == TradeDirection.LONG:
        return instrument.make_price(entry_price + offset)

    return instrument.make_price(entry_price - offset)


@dataclass
class Entry(Stratlet):
    config: DriftPullbackStrategyConfig
    instrument: Instrument
    direction: TradeDirection
    stop_points: float
    target_points: float
    quantity: Quantity
    flatten_ns: int
    orders: dict[str, Order] = field(default_factory=dict, init=False)
    position: Position | None = field(default=None, init=False)
    stopping: bool = field(default=False, init=False)
    stopped: bool = field(default=False, init=False)

    async def on_start(self) -> None:
        # The spec sends the entry at market on the next bar, so the fill is the open of the bar
        # after the trigger and there is no intrabar entry to model.
        order_side = OrderSide.BUY if self.direction == TradeDirection.LONG else OrderSide.SELL
        entry_order = self.strategy.order_factory.market(
            instrument_id=self.instrument.id,
            order_side=order_side,
            quantity=self.quantity,
            tags=[STRATEGY_TAG, "ENTRY"],
        )

        self.orders["entry"] = entry_order
        self.strategy.submit_order(entry_order)
        self.strategy.log.info(
            f"Submitted {self.direction.value} MARKET entry {entry_order.client_order_id} qty={self.quantity}",
        )

        while not self.stopping and not self.strategy.cache.is_order_closed(entry_order.client_order_id):
            if self._past(self.flatten_ns):
                self.strategy.log.info(f"Flat time reached; canceling entry {entry_order.client_order_id}")
                await self.on_stop()
                return

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

        # Both exits are fixed distances from the fill, which is what EntryPrice means in the spec,
        # so neither level exists until the market entry has filled and a bracket cannot express them.
        entry_price = closed_entry_order.avg_px
        stop_price = _stop_price(
            instrument=self.instrument,
            direction=self.direction,
            entry_price=entry_price,
            stop_points=self.stop_points,
            slip_ticks=self.config.stop_slip_ticks,
        )
        take_profit_price = _take_profit_price(
            instrument=self.instrument,
            direction=self.direction,
            entry_price=entry_price,
            target_points=self.target_points,
        )
        exit_side = OrderSide.SELL if self.direction == TradeDirection.LONG else OrderSide.BUY

        stop_order = self.strategy.order_factory.stop_market(
            instrument_id=self.instrument.id,
            order_side=exit_side,
            quantity=closed_entry_order.filled_qty,
            trigger_price=stop_price,
            reduce_only=True,
            tags=[STRATEGY_TAG, "STOP_LOSS"],
        )
        take_profit_order = self.strategy.order_factory.limit(
            instrument_id=self.instrument.id,
            order_side=exit_side,
            quantity=closed_entry_order.filled_qty,
            price=take_profit_price,
            reduce_only=True,
            tags=[STRATEGY_TAG, "TAKE_PROFIT"],
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
            f"entry={entry_price}, tp={take_profit_price}, sl={stop_price}",
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
            if self._past(self.flatten_ns):
                self.strategy.log.info(f"Flat time reached; flattening position {self.position.id}")
                await self.on_stop()
                return

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

    def _past(self, boundary_ns: int) -> bool:
        return self.strategy.clock.timestamp_ns() >= boundary_ns

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
                    tags=[STRATEGY_TAG, "CLEANUP"],
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


class DriftPullbackStrategy(BaseStrategy):
    def __init__(self, config: DriftPullbackStrategyConfig) -> None:
        super().__init__(config)

        self._bar_type = BarType.from_str(config.bar_type)
        self._signal_bar_type = BarType.from_str(config.signal_bar_type)
        self._vwap_bar_type = BarType.from_str(config.vwap_bar_type) if config.vwap_bar_type else None
        # A bar type with no composite source cannot be built from a bar catalog, so the bar types
        # themselves name the data the engine has to be fed. There is no separate selector to
        # contradict them.
        self._tick_driven = not self._bar_type.is_composite()
        self._vwap_source = self._parse_vwap_source(config.vwap_source)
        self._timezone = _parse_timezone(config.session_timezone)
        self._entry_start, self._entry_end = _parse_time_range(config.entry_window, "entry_window")
        self._vwap_start, self._vwap_end = _parse_time_range(config.vwap_window, "vwap_window")
        self._session_cutoff = _parse_time(config.session_cutoff, "session_cutoff")
        self._validate_config()

        self._vwap_interval_seconds = (
            self._vwap_bar_type.spec.get_interval_ns() // 1_000_000_000
            if self._vwap_bar_type is not None
            else TICK_VWAP_OPEN_TOLERANCE_SECONDS
        )

        self._instrument: Instrument | None = None

        self._session_date: date | None = None
        self._vwap: SessionVwap = SessionVwap()
        self._signal_closes: list[float] = []
        self._signal_ts_event: int = 0
        self._setup: ArmedSetup | None = None
        self._trades: int = 0
        self._losses: int = 0
        self._entry: Entry | None = None

    def on_start(self) -> None:
        super().on_start()

        self._instrument = self.cache.instrument(self._bar_type.instrument_id)
        if self._instrument is None:
            raise RuntimeError(f"No instrument found for {self._bar_type.instrument_id}")

        # A configuration may point two roles at one bar type, so subscribe to the distinct set.
        for bar_type in self._subscribed_bar_types():
            self.subscribe_bars(bar_type)
        if self._vwap_source == VwapSource.TRADE_TICKS:
            self.subscribe_trade_ticks(self._bar_type.instrument_id)

    def on_bar(self, bar: Bar) -> None:
        # Coincident bars are delivered VWAP source first, then the signal bar, then the execution
        # bar, which is the order the rules need: accumulate, then freeze the anchor and arm, then
        # age the setup and trigger. Independent tests rather than a chain, so a configuration that
        # points two roles at one bar type still runs them in that order.
        standard = bar.bar_type.standard()
        if self._vwap_bar_type is not None and standard == self._vwap_bar_type.standard():
            self._on_vwap_bar(bar)
        if standard == self._signal_bar_type.standard():
            self._on_signal_bar(bar)
        if standard == self._bar_type.standard():
            self._on_execution_bar(bar)

        super().on_bar(bar)

    def on_trade_tick(self, tick: TradeTick) -> None:
        if self._vwap_source == VwapSource.TRADE_TICKS:
            self._on_vwap_tick(tick)

        super().on_trade_tick(tick)

    def on_position_event(self, event: PositionEvent) -> None:
        if isinstance(event, PositionClosed) and event.realized_pnl is not None:
            self._record_closed_trade(event.realized_pnl.as_double())

        super().on_position_event(event)

    def on_stop(self) -> None:
        for bar_type in self._subscribed_bar_types():
            self.unsubscribe_bars(bar_type)
        if self._vwap_source == VwapSource.TRADE_TICKS:
            self.unsubscribe_trade_ticks(self._bar_type.instrument_id)

        super().on_stop()

    def _subscribed_bar_types(self) -> tuple[BarType, ...]:
        streams = (self._vwap_bar_type, self._signal_bar_type, self._bar_type)

        return tuple(dict.fromkeys(bar_type for bar_type in streams if bar_type is not None))

    @staticmethod
    def _parse_vwap_source(value: str) -> VwapSource:
        try:
            return VwapSource(value.upper())
        except ValueError as exc:
            allowed = ", ".join(source.value for source in VwapSource)
            raise ValueError(f"vwap_source must be one of: {allowed}") from exc

    def _record_closed_trade(self, realized_pnl: float) -> None:
        # Split out of on_position_event so the loss budget is reachable without an execution engine.
        if realized_pnl < 0:
            self._losses += 1
            self.log.info(f"Losing trade closed at {realized_pnl}; {self._losses}/{self.config.max_losses_per_day} today")

    def _validate_config(self) -> None:
        self._validate_bar_types()

        # A trade-tick anchor needs prints in the stream, and only a tick-fed run has them. Requiring
        # vwap_bar_type to be unset in that mode keeps a config from carrying a value it ignores.
        if self._vwap_source == VwapSource.TRADE_TICKS:
            if not self._tick_driven:
                raise ValueError("vwap_source TRADE_TICKS requires bar types aggregated from ticks")
            if self._vwap_bar_type is not None:
                raise ValueError("vwap_bar_type must be unset when vwap_source is TRADE_TICKS")
        elif self._vwap_bar_type is None:
            raise ValueError("vwap_bar_type is required when vwap_source is BARS")

        if self._entry_start >= self._entry_end:
            raise ValueError("entry_window start must be earlier than its end")
        if self._vwap_start >= self._vwap_end:
            raise ValueError("vwap_window start must be earlier than its end")
        if self._session_cutoff < self._entry_end:
            raise ValueError("session_cutoff must not be earlier than entry_window ends")
        if self.config.drift_lookback_bars < 1:
            raise ValueError("drift_lookback_bars must be positive")
        if self.config.drift_threshold_pct < 0:
            raise ValueError("drift_threshold_pct must not be negative")
        if self.config.pullback_window_bars < 1:
            raise ValueError("pullback_window_bars must be positive")
        if not self.config.trade_longs and not self.config.trade_shorts:
            raise ValueError("trade_longs and trade_shorts must not both be disabled")
        for value, field_name in (
            (self.config.long_stop_points, "long_stop_points"),
            (self.config.long_target_points, "long_target_points"),
            (self.config.short_stop_points, "short_stop_points"),
            (self.config.short_target_points, "short_target_points"),
        ):
            if value <= 0:
                raise ValueError(f"{field_name} must be positive")
        if self.config.stop_slip_ticks < 0:
            raise ValueError("stop_slip_ticks must not be negative")
        if self.config.max_trades_per_day < 1:
            raise ValueError("max_trades_per_day must be positive")
        if self.config.max_losses_per_day < 1:
            raise ValueError("max_losses_per_day must be positive")
        if self.config.contracts < 1:
            raise ValueError("contracts must be positive")

    def _validate_bar_types(self) -> None:
        streams = [(self._bar_type, "bar_type"), (self._signal_bar_type, "signal_bar_type")]
        if self._vwap_bar_type is not None:
            streams.append((self._vwap_bar_type, "vwap_bar_type"))

        for bar_type, field_name in streams:
            if not bar_type.spec.is_time_aggregated():
                raise ValueError(f"{field_name} must be a time-aggregated bar type")
            if bar_type.instrument_id != self._bar_type.instrument_id:
                raise ValueError(f"{field_name} must name the same instrument as bar_type")
            if self._tick_driven:
                # Every stream is aggregated from the prints the engine is fed, so none of them may
                # name a catalog source: mixing the two shapes would need data that is not there.
                if bar_type.is_composite():
                    raise ValueError(f"{field_name} must not name a composite source when bar_type does not")
                if not bar_type.is_internally_aggregated():
                    raise ValueError(f"{field_name} must be internally aggregated to build from ticks")

                continue

            if bar_type.is_composite() and not bar_type.composite().is_externally_aggregated():
                raise ValueError(f"{field_name} composite source must be externally aggregated")
            # All three streams are aggregated from one catalog load, so they have to share a source.
            if self._catalog_source(bar_type) != self._catalog_source(self._bar_type):
                raise ValueError(f"{field_name} must be aggregated from the same source as bar_type")

        signal_interval_ns = self._signal_bar_type.spec.get_interval_ns()
        execution_interval_ns = self._bar_type.spec.get_interval_ns()
        if signal_interval_ns <= execution_interval_ns:
            raise ValueError("signal_bar_type interval must be longer than the bar_type interval")
        if signal_interval_ns % execution_interval_ns != 0:
            raise ValueError("signal_bar_type interval must be an exact multiple of the bar_type interval")
        if self._vwap_bar_type is None:
            return

        # The anchor is snapshotted on the signal boundary, so the accumulation has to complete there.
        vwap_interval_ns = self._vwap_bar_type.spec.get_interval_ns()
        if vwap_interval_ns > signal_interval_ns:
            raise ValueError("vwap_bar_type interval must not be longer than the signal_bar_type interval")
        if signal_interval_ns % vwap_interval_ns != 0:
            raise ValueError("vwap_bar_type interval must divide the signal_bar_type interval exactly")

    @staticmethod
    def _catalog_source(bar_type: BarType) -> str:
        return str(bar_type.composite().standard()) if bar_type.is_composite() else str(bar_type.standard())

    def _session_local(self, bar: Bar) -> datetime:
        return datetime.fromtimestamp(bar.ts_event / 1_000_000_000, UTC).astimezone(self._timezone)

    def _roll_session(self, session_date: date) -> None:
        # Every handler calls this, because all three streams share timestamps at midnight and a
        # roll in only one of them would let the new day accumulate before the reset ran.
        self._session_date = session_date
        self._vwap: SessionVwap = SessionVwap()
        self._setup = None
        self._trades = 0
        self._losses = 0

    def _on_vwap_bar(self, bar: Bar) -> None:
        bar_close = self._session_local(bar)
        if bar_close.date() != self._session_date:
            self._roll_session(bar_close.date())

        # Internal aggregation emits a flat, volumeless bar for every interval the venue was shut,
        # and those bars do not exist on the chart the spec was written against.
        if bar.volume.as_double() <= 0:
            return
        if bar.ts_event == self._signal_ts_event:
            self.log.warning(
                f"VWAP bar at {bar_close} arrived after the signal bar sharing its timestamp; "
                "the anchor snapshot missed it",
            )

        # Bars are stamped on close, so the accumulation window is the bars closing after its start
        # up to and including its end. This is the opposite convention to the entry window, and both
        # are literal readings of the spec.
        bar_time = bar_close.time()
        if not self._vwap_start < bar_time <= self._vwap_end:
            return

        if not self._vwap.accumulated:
            # The first accumulated bar has to be the window's own first bar, or the strategy joined
            # a session already in progress and what it would build is not a session VWAP.
            first_close = _seconds_since_midnight(self._vwap_start) + self._vwap_interval_seconds
            self._vwap.started_at_open = _seconds_since_midnight(bar_time) <= first_close
            self._vwap.accumulated = True
            if not self._vwap.started_at_open:
                self.log.warning(
                    f"VWAP accumulation for {self._session_date} began at {bar_time}, after the window opened",
                )

        self._vwap.price_volume += _typical_price(bar) * bar.volume.as_double()
        self._vwap.volume += bar.volume.as_double()
        if self._vwap.volume > 0:
            self._vwap.value = self._vwap.price_volume / self._vwap.volume

    def _on_vwap_tick(self, tick: TradeTick) -> None:
        stamped = datetime.fromtimestamp(tick.ts_event / 1_000_000_000, UTC).astimezone(self._timezone)
        if stamped.date() != self._session_date:
            self._roll_session(stamped.date())

        # A print carries no interval, so this window is half-open where the bar window is
        # half-closed. The two select the same trades: the bars closing in (09:30, 16:00] are built
        # from exactly the prints in [09:30:00, 16:00:00).
        stamped_time = stamped.time()
        if not self._vwap_start <= stamped_time < self._vwap_end:
            return

        if not self._vwap.accumulated:
            # The first print has to fall in the window's opening minute, or the strategy joined a
            # session already in progress and what it would build is not a session VWAP.
            first_print = _seconds_since_midnight(self._vwap_start) + self._vwap_interval_seconds
            self._vwap.started_at_open = _seconds_since_midnight(stamped_time) < first_print
            self._vwap.accumulated = True
            if not self._vwap.started_at_open:
                self.log.warning(
                    f"VWAP accumulation for {self._session_date} began at {stamped_time}, after the window opened",
                )

        # The true VWAP: traded price weighted by traded size, with no typical-price approximation.
        size = tick.size.as_double()
        self._vwap.price_volume += tick.price.as_double() * size
        self._vwap.volume += size
        if self._vwap.volume > 0:
            self._vwap.value = self._vwap.price_volume / self._vwap.volume

    def _on_signal_bar(self, bar: Bar) -> None:
        bar_close = self._session_local(bar)
        if bar_close.date() != self._session_date:
            self._roll_session(bar_close.date())

        if bar.volume.as_double() <= 0:
            return

        self._signal_ts_event = bar.ts_event

        # The anchor is snapshotted here and frozen until the next boundary, which is what makes the
        # signal constant inside a signal bar and the backtest match what a live run would see.
        self._vwap.previous = self._vwap.last
        self._vwap.last = self._vwap.value

        self._signal_closes.append(bar.close.as_double())
        del self._signal_closes[: -(self.config.drift_lookback_bars + 1)]

        self._evaluate_arming(bar)

    def _evaluate_arming(self, bar: Bar) -> None:
        anchor = self._vwap.last
        # The first snapshot of a session has no predecessor, so the slope check is neutralised
        # rather than invented; no session VWAP setup can arm on the window's first signal bar.
        previous_anchor = self._vwap.previous if self._vwap.previous > 0 else anchor
        anchor_ok = anchor > 0 and (self._vwap.started_at_open or not self.config.require_full_vwap_session)
        drift = _drift_pct(self._signal_closes, self.config.drift_lookback_bars)
        close = bar.close.as_double()
        threshold = self.config.drift_threshold_pct

        # The three conditions of the spec: location against the anchor, the anchor's own slope, and
        # the drift. The else branch is a rule and not a default - a setup that no longer qualifies
        # on this boundary is gone rather than surviving on inertia.
        if (
            anchor_ok
            and drift is not None
            and close > anchor
            and anchor > previous_anchor
            and drift >= threshold
        ):
            self._setup = ArmedSetup(direction=TradeDirection.LONG)
        elif (
            anchor_ok
            and drift is not None
            and close < anchor
            and anchor < previous_anchor
            and drift <= -threshold
        ):
            self._setup = ArmedSetup(direction=TradeDirection.SHORT)
        else:
            self._setup = None

            return

        self.log.info(
            f"Armed {self._setup.direction.value} on {self._session_date}: close={close} "
            f"anchor={anchor} previous_anchor={previous_anchor} drift={drift}",
        )

    def _on_execution_bar(self, bar: Bar) -> None:
        if self._instrument is None:
            raise RuntimeError("Instrument is not initialized")

        bar_close = self._session_local(bar)
        if bar_close.date() != self._session_date:
            self._roll_session(bar_close.date())

        if bar.volume.as_double() <= 0:
            return

        # 1. The armed setup ages on every execution bar, including the one that armed it, and dies
        #    once it has outlived the pullback window.
        setup = self._setup
        if setup is None:
            return

        setup.bars += 1
        if setup.bars > self.config.pullback_window_bars:
            self.log.info(f"{setup.direction.value} setup expired without a pullback")
            self._setup = None

            return

        # 2. The gate stack, before the trigger. A setup blocked by any of these keeps its flag: only
        #    a trigger consumes it.
        if self._entry is not None and not self._entry.completed:
            return

        # Bars are stamped on close and the entry fills on the bar after the trigger, so the literal
        # reading of the spec is an inclusive start and an exclusive end.
        bar_time = bar_close.time()
        if not self._entry_start <= bar_time < self._entry_end:
            return
        if self._trades >= self.config.max_trades_per_day:
            return
        if self._losses >= self.config.max_losses_per_day:
            return
        if setup.direction == TradeDirection.LONG and not self.config.trade_longs:
            return
        if setup.direction == TradeDirection.SHORT and not self.config.trade_shorts:
            return

        # 3. The pullback itself: one bar against the drift. Not a retracement level, not a band.
        open_ = bar.open.as_double()
        close = bar.close.as_double()
        if setup.direction == TradeDirection.LONG and close >= open_:
            return
        if setup.direction == TradeDirection.SHORT and close <= open_:
            return

        # 4. The flag is spent on the decision, whether or not the order goes on to fill.
        self._setup = None
        self._trades += 1
        self.log.info("long_signal" if setup.direction == TradeDirection.LONG else "short_signal")

        if setup.direction == TradeDirection.LONG:
            stop_points = self.config.long_stop_points
            target_points = self.config.long_target_points
        else:
            stop_points = self.config.short_stop_points
            target_points = self.config.short_target_points

        entry = Entry(
            strategy=self,
            config=self.config,
            instrument=self._instrument,
            direction=setup.direction,
            stop_points=stop_points,
            target_points=target_points,
            quantity=self._instrument.make_qty(self.config.contracts),
            flatten_ns=self._boundary_ns(bar_close.date(), self._session_cutoff),
        )
        self._entry = entry
        self._add_stratlet(entry)

    def _boundary_ns(self, session_date: date, boundary: time) -> int:
        return int(datetime.combine(session_date, boundary, tzinfo=self._timezone).timestamp() * 1_000_000_000)
