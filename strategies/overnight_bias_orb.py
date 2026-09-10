import asyncio
import math


from collections import deque
from dataclasses import dataclass
from dataclasses import field
from datetime import UTC
from datetime import date
from datetime import datetime
from datetime import time
from enum import Enum
from zoneinfo import ZoneInfo


from nautilus_trader.indicators.averages import WilderMovingAverage
from nautilus_trader.model.data import Bar
from nautilus_trader.model.data import BarType
from nautilus_trader.model.enums import OrderSide
from nautilus_trader.model.instruments import Instrument
from nautilus_trader.model.objects import Price
from nautilus_trader.model.objects import Quantity
from nautilus_trader.model.orders import Order
from nautilus_trader.model.position import Position

from strategies.base import BaseStrategy
from strategies.base import BaseStrategyConfig
from strategies.base import Stratlet


# One stream of one instrument. The composite source is the spec's bar magnifier: the venue matches
# resting orders against the bars the engine is fed, so a 15-minute signal built from 1-minute
# catalog bars gets its exits checked every minute. Sessions are plain calendar days in the session
# timezone, which is what the spec's date reset means on an Eastern-keyed chart - so the overnight
# range runs from midnight, not from the Globex open.
PERCENT = 100.0
SECONDS_PER_HOUR = 3_600
SECONDS_PER_MINUTE = 60
STRATEGY_TAG = "OVERNIGHT_BIAS_ORB"


class TradeDirection(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"


class OvernightBiasORBStrategyConfig(BaseStrategyConfig, frozen=True):
    bar_type: str
    session_timezone: str = "America/New_York"
    session_start: str = "09:30"
    opening_range_end: str = "09:45"
    last_entry: str = "13:00"
    session_cutoff: str = "15:30"
    session_end: str = "16:00"
    max_trades_per_day: int = 4
    bias_long_min: float = 0.67
    bias_short_max: float = 0.33
    adx_length: int = 16
    adx_min: float = 20.0
    break_skip_multiple: float = 2.5
    bar_average_length: int = 20
    min_or_multiple: float = 0.0
    max_or_multiple: float = 2.0
    or_median_days: int = 20
    atr_multiple: float = 0.30
    atr_days: int = 15
    take_profit_rr: float = 3.0
    contracts: int = 1


def _parse_time(value: str, field_name: str) -> time:
    try:
        return time.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be an ISO 8601 time such as 09:30") from exc


def _parse_timezone(value: str) -> ZoneInfo:
    try:
        return ZoneInfo(value)
    except (KeyError, ValueError) as exc:
        raise ValueError("session_timezone must be an IANA timezone name such as America/New_York") from exc


def _seconds_since_midnight(value: time) -> int:
    return (value.hour * SECONDS_PER_HOUR) + (value.minute * SECONDS_PER_MINUTE) + value.second


def _points_to_ticks(points: float, tick_size: float) -> int:
    # Half up, so a distance landing between two ticks resolves the same way everywhere. make_price
    # rounds to the price precision rather than to the increment, so working in whole ticks is what
    # keeps every derived price on the grid.
    return math.floor((points / tick_size) + 0.5)


def _exit_prices(
    instrument: Instrument,
    direction: TradeDirection,
    close: float,
    stop_points: float,
    take_profit_rr: float,
) -> tuple[Price, Price]:
    # Both legs hang off the signal bar's close rather than off the fill, which is what the spec
    # does. The reward is scaled from the rounded stop rather than from the raw distance, so the
    # target is a whole number of ticks too and an exact multiple of the risk.
    tick_size = instrument.price_increment.as_double()
    stop_ticks = _points_to_ticks(stop_points, tick_size)
    target_ticks = math.floor((take_profit_rr * stop_ticks) + 0.5)
    if direction == TradeDirection.LONG:
        return (
            instrument.make_price(close - (stop_ticks * tick_size)),
            instrument.make_price(close + (target_ticks * tick_size)),
        )

    return (
        instrument.make_price(close + (stop_ticks * tick_size)),
        instrument.make_price(close - (target_ticks * tick_size)),
    )


class WilderAdx:
    # Nautilus ships no ADX. DirectionalMovement smooths the raw directional movement without
    # dividing it by the true range and never forms DX, so it is a different number. The spec asks
    # for the platform's native ADX and says why - a simple-average lookalike passes different bars -
    # and that native smoothing is Wilder's, which is what WilderMovingAverage is.
    def __init__(self, period: int) -> None:
        self._plus_dm = WilderMovingAverage(period)
        self._minus_dm = WilderMovingAverage(period)
        self._true_range = WilderMovingAverage(period)
        self._dx = WilderMovingAverage(period)
        self._previous_high = 0.0
        self._previous_low = 0.0
        self._previous_close = 0.0
        self._seen = False

    @property
    def initialized(self) -> bool:
        return self._dx.initialized

    @property
    def value(self) -> float:
        return self._dx.value

    def handle_bar(self, bar: Bar) -> None:
        high = bar.high.as_double()
        low = bar.low.as_double()
        close = bar.close.as_double()
        if self._seen:
            up = high - self._previous_high
            down = self._previous_low - low
            self._plus_dm.update_raw(up if up > down and up > 0 else 0.0)
            self._minus_dm.update_raw(down if down > up and down > 0 else 0.0)
            self._true_range.update_raw(
                max(high - low, abs(high - self._previous_close), abs(low - self._previous_close)),
            )

            average_true_range = self._true_range.value
            if average_true_range > 0:
                plus_di = PERCENT * self._plus_dm.value / average_true_range
                minus_di = PERCENT * self._minus_dm.value / average_true_range
            else:
                plus_di = 0.0
                minus_di = 0.0

            directional_sum = plus_di + minus_di
            self._dx.update_raw(
                PERCENT * abs(plus_di - minus_di) / directional_sum if directional_sum > 0 else 0.0,
            )

        self._previous_high = high
        self._previous_low = low
        self._previous_close = close
        self._seen = True


@dataclass
class SessionState:
    # The 0 and inf seeds are the spec's 0 and 999999 sentinels. opening_high doubles as its test for
    # whether any opening-range bar existed at all, which is why it starts at zero and not at -inf.
    overnight_high: float = 0.0
    overnight_low: float = math.inf
    bias: TradeDirection | None = None
    bias_frozen: bool = False
    opening_high: float = 0.0
    opening_low: float = math.inf
    opening_range_done: bool = False
    regime_ok: bool = True
    trades: int = 0
    rth_high: float = 0.0
    rth_low: float = math.inf
    rth_close: float = 0.0
    rth_seen: bool = False


@dataclass
class Entry(Stratlet):
    instrument: Instrument
    direction: TradeDirection
    stop_price: Price
    take_profit_price: Price
    quantity: Quantity
    flatten_ns: int
    orders: dict[str, Order] = field(default_factory=dict, init=False)
    position: Position | None = field(default=None, init=False)
    stopping: bool = field(default=False, init=False)
    stopped: bool = field(default=False, init=False)

    async def on_start(self) -> None:
        # The spec sends the entry at market on the next bar. The venue matches a bar before the
        # strategy sees it, so this order settles against the book the signal bar left behind and
        # fills at that bar's close - the same price both exits are measured from.
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

        # Both levels were fixed by the signal bar's close, so they exist before the entry is sent.
        # They go out after the fill only because the position id does not exist until then, and
        # because the exit quantity has to match what actually filled. The fill event pumps this
        # coroutine inside the same venue drain that produced it, so the exits are accepted before
        # the next source bar is processed and the entry's own interval is covered.
        exit_side = OrderSide.SELL if self.direction == TradeDirection.LONG else OrderSide.BUY
        stop_order = self.strategy.order_factory.stop_market(
            instrument_id=self.instrument.id,
            order_side=exit_side,
            quantity=closed_entry_order.filled_qty,
            trigger_price=self.stop_price,
            reduce_only=True,
            tags=[STRATEGY_TAG, "STOP_LOSS"],
        )
        take_profit_order = self.strategy.order_factory.limit(
            instrument_id=self.instrument.id,
            order_side=exit_side,
            quantity=closed_entry_order.filled_qty,
            price=self.take_profit_price,
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
            f"entry={closed_entry_order.avg_px}, tp={self.take_profit_price}, sl={self.stop_price}",
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


class OvernightBiasORBStrategy(BaseStrategy):
    def __init__(self, config: OvernightBiasORBStrategyConfig) -> None:
        super().__init__(config)

        self._bar_type = BarType.from_str(config.bar_type)
        self._timezone = _parse_timezone(config.session_timezone)
        self._session_start = _parse_time(config.session_start, "session_start")
        self._opening_range_end = _parse_time(config.opening_range_end, "opening_range_end")
        self._last_entry = _parse_time(config.last_entry, "last_entry")
        self._session_cutoff = _parse_time(config.session_cutoff, "session_cutoff")
        self._session_end = _parse_time(config.session_end, "session_end")
        self._validate_config()

        self._instrument: Instrument | None = None

        # Everything below survives the daily roll. The two counters are what make the first
        # sessions of any run behave differently from the rest: no trade at all until the true range
        # buffer fills, then trading with the opening-range band still switched off until the
        # reference has seen or_median_days of ranges.
        self._true_ranges: deque[float] = deque(maxlen=config.atr_days)
        self._previous_rth_close: float = 0.0
        self._atr_ref: float = 0.0
        self._or_median: float = 0.0
        self._or_count: int = 0
        self._bar_ranges: deque[float] = deque(maxlen=config.bar_average_length)
        self._adx = WilderAdx(period=config.adx_length)

        self._session_date: date | None = None
        self._session = SessionState()
        self._entry: Entry | None = None

    def on_start(self) -> None:
        super().on_start()

        self._instrument = self.cache.instrument(self._bar_type.instrument_id)
        if self._instrument is None:
            raise RuntimeError(f"No instrument found for {self._bar_type.instrument_id}")

        self.subscribe_bars(self._bar_type)

    def on_bar(self, bar: Bar) -> None:
        self._on_session_bar(bar)

        super().on_bar(bar)

    def on_stop(self) -> None:
        self.unsubscribe_bars(self._bar_type)

        super().on_stop()

    def _validate_config(self) -> None:
        self._validate_bar_type()

        if self._session_start >= self._opening_range_end:
            raise ValueError("session_start must be earlier than opening_range_end")
        if self._opening_range_end >= self._last_entry:
            raise ValueError("opening_range_end must be earlier than last_entry")
        if self._last_entry > self._session_cutoff:
            raise ValueError("last_entry must not be later than session_cutoff")
        if self._session_cutoff > self._session_end:
            raise ValueError("session_cutoff must not be later than session_end")
        if not 0 <= self.config.bias_short_max < self.config.bias_long_min <= 1:
            raise ValueError("bias_short_max and bias_long_min must satisfy 0 <= short < long <= 1")
        if self.config.adx_length < 1:
            raise ValueError("adx_length must be positive")
        if self.config.adx_min < 0:
            raise ValueError("adx_min must not be negative")
        if self.config.break_skip_multiple <= 0:
            raise ValueError("break_skip_multiple must be positive")
        if self.config.bar_average_length < 1:
            raise ValueError("bar_average_length must be positive")
        if self.config.min_or_multiple < 0:
            raise ValueError("min_or_multiple must not be negative")
        if self.config.max_or_multiple < self.config.min_or_multiple:
            raise ValueError("max_or_multiple must not be smaller than min_or_multiple")
        if self.config.or_median_days < 1:
            raise ValueError("or_median_days must be positive")
        if self.config.atr_multiple <= 0:
            raise ValueError("atr_multiple must be positive")
        if self.config.atr_days < 1:
            raise ValueError("atr_days must be positive")
        if self.config.take_profit_rr <= 0:
            raise ValueError("take_profit_rr must be positive")
        if self.config.max_trades_per_day < 1:
            raise ValueError("max_trades_per_day must be positive")
        if self.config.contracts < 1:
            raise ValueError("contracts must be positive")

    def _validate_bar_type(self) -> None:
        if not self._bar_type.spec.is_time_aggregated():
            raise ValueError("bar_type must be a time-aggregated bar type")
        # The spec requires a one-minute bar magnifier, and in a backtest the venue matches resting
        # orders against the bars the engine is fed rather than against the stream the strategy
        # subscribes to. The composite source is therefore the magnifier, and it has to exist.
        if not self._bar_type.is_composite():
            raise ValueError("bar_type must name a composite source, which is the bar magnifier the exits need")
        if not self._bar_type.is_internally_aggregated():
            raise ValueError("bar_type must be internally aggregated")
        if not self._bar_type.composite().is_externally_aggregated():
            raise ValueError("bar_type composite source must be externally aggregated")

        # Every window boundary below is compared against a bar's closing stamp, so one that falls
        # inside a bar moves the whole partition by up to a bar without erroring.
        interval_seconds = self._bar_type.spec.get_interval_ns() // 1_000_000_000
        for boundary, field_name in (
            (self._session_start, "session_start"),
            (self._opening_range_end, "opening_range_end"),
            (self._last_entry, "last_entry"),
            (self._session_end, "session_end"),
        ):
            if _seconds_since_midnight(boundary) % interval_seconds != 0:
                raise ValueError(f"{field_name} must fall on a {interval_seconds}s bar boundary")

    def _session_local(self, bar: Bar) -> datetime:
        return datetime.fromtimestamp(bar.ts_event / 1_000_000_000, UTC).astimezone(self._timezone)

    def _boundary_ns(self, session_date: date, boundary: time) -> int:
        return int(datetime.combine(session_date, boundary, tzinfo=self._timezone).timestamp() * 1_000_000_000)

    def _average_bar_range(self) -> float:
        # The spec reads its 20-bar average of bar ranges one bar back, so this is the mean of the
        # bars before the current one. Calling it before appending the current range is what
        # implements that offset; appending first would let an oversized bar raise its own reference.
        if len(self._bar_ranges) < self.config.bar_average_length:
            return 0.0

        return sum(self._bar_ranges) / self.config.bar_average_length

    def _roll_session(self, session_date: date) -> None:
        session = self._session
        if session.rth_seen:
            true_range = session.rth_high - session.rth_low
            if self._previous_rth_close > 0:
                true_range = max(
                    true_range,
                    abs(session.rth_high - self._previous_rth_close),
                    abs(session.rth_low - self._previous_rth_close),
                )

            # A deque bounded at atr_days is the spec's circular buffer, its write index and its fill
            # counter in one: it holds the last atr_days true ranges and nothing else.
            self._true_ranges.append(true_range)
            # Advanced after the true range is built, so each day measures against the previous
            # session's regular-hours close, and only sessions that had regular hours advance it.
            self._previous_rth_close = session.rth_close

        # Read after the append, which is what makes the atr_days-th completed session the first one
        # that can trade rather than the one after it.
        if len(self._true_ranges) == self.config.atr_days:
            self._atr_ref = sum(self._true_ranges) / self.config.atr_days
        else:
            self._atr_ref = 0.0

        self._session_date = session_date
        self._session = SessionState()

    def _on_session_bar(self, bar: Bar) -> None:
        if self._instrument is None:
            raise RuntimeError("Instrument is not initialized")

        bar_close = self._session_local(bar)
        if bar_close.date() != self._session_date:
            self._roll_session(bar_close.date())

        # Internal aggregation emits a flat, volumeless bar for every interval the venue was shut,
        # and those bars do not exist on the chart the spec was written against. Rolling before the
        # guard is harmless: a session with no regular hours contributes no true range either way.
        if bar.volume.as_double() <= 0:
            return

        session = self._session
        bar_time = bar_close.time()
        high = bar.high.as_double()
        low = bar.low.as_double()
        close = bar.close.as_double()

        # 1. The overnight range: every bar of this calendar date closing at or before the open. The
        #    spec seeds it from the date's first bar and extends from there; against the sentinels
        #    one pair of comparisons does both, for every date whose first bar is a pre-open one.
        #    A date that opens later than that - a Sunday evening, or a gap over the whole morning -
        #    is left with no range and so no bias, where the spec would seed one from a post-open
        #    bar and call it overnight.
        if bar_time <= self._session_start:
            session.overnight_high = max(session.overnight_high, high)
            session.overnight_low = min(session.overnight_low, low)

        # 2. The regular-hours extremes that become tomorrow's true range. Bars are stamped on close,
        #    so the open stamp itself is the last pre-open bar and is excluded.
        if self._session_start < bar_time <= self._session_end:
            if session.rth_seen:
                session.rth_high = max(session.rth_high, high)
                session.rth_low = min(session.rth_low, low)
            else:
                session.rth_high = high
                session.rth_low = low
                session.rth_seen = True

            session.rth_close = close

        # 3. Direction bias, frozen on the session's first opening-range bar and never revisited. It
        #    is that bar's OPEN, and an open high in the overnight range means LONG: the rule is gap
        #    continuation, not mean reversion.
        if not session.bias_frozen and self._session_start < bar_time <= self._opening_range_end:
            # The freeze is spent even when the overnight range is degenerate, which is what leaves
            # such a session with no bias rather than deferring the decision to a later bar.
            session.bias_frozen = True
            if session.overnight_high > session.overnight_low:
                position = (bar.open.as_double() - session.overnight_low) / (
                    session.overnight_high - session.overnight_low
                )
                if position >= self.config.bias_long_min:
                    session.bias = TradeDirection.LONG
                elif position <= self.config.bias_short_max:
                    session.bias = TradeDirection.SHORT

                bias = session.bias.value if session.bias is not None else "NONE"
                self.log.info(
                    f"Session {self._session_date} bias {bias}: open={bar.open} "
                    f"overnight={session.overnight_low}-{session.overnight_high} position={position:.4f} "
                    f"atr_ref={self._atr_ref:.2f}",
                )

        # 4. The opening range itself, over the same window.
        if self._session_start < bar_time <= self._opening_range_end:
            session.opening_high = max(session.opening_high, high)
            session.opening_low = min(session.opening_low, low)

        # 5. Lock the range and settle the size band on the first bar past it - which is also the
        #    first bar that may trade, so this has to run before the entry gate below.
        if not session.opening_range_done and bar_time > self._opening_range_end and session.opening_high > 0:
            session.opening_range_done = True
            width = session.opening_high - session.opening_low
            session.regime_ok = True
            # Measured against the reference as it stood before today, and only then is today folded
            # into it. Folding first would make the test partly self-referential.
            if self._or_median > 0:
                session.regime_ok = (
                    self.config.min_or_multiple * self._or_median
                    <= width
                    <= self.config.max_or_multiple * self._or_median
                )
            if self._or_median == 0:
                self._or_median = width
            else:
                self._or_median += (2 / (self.config.or_median_days + 1)) * (width - self._or_median)

            # Counted before the warm-up override, so the band starts binding on the or_median_days-th
            # opening range rather than the one after it.
            self._or_count += 1
            if self._or_count < self.config.or_median_days:
                session.regime_ok = True

        # 6. The stop distance is a session constant: the reference only moves at the roll. Zero
        #    ticks is the spec's "no true range yet, sit the session out".
        stop_points = self.config.atr_multiple * self._atr_ref
        stop_ticks = _points_to_ticks(stop_points, self._instrument.price_increment.as_double())

        # 7. The two series gates. Both advance on every bar of every session, tradable or not,
        #    because the spec evaluates them across the whole stream. The trend gate reads the bar
        #    being processed; the range filter measures against the bars before it.
        self._adx.handle_bar(bar)
        adx_ok = self._adx.initialized and self._adx.value >= self.config.adx_min

        bar_range = high - low
        average_range = self._average_bar_range()
        self._bar_ranges.append(bar_range)
        range_ok = average_range == 0 or bar_range <= self.config.break_skip_multiple * average_range

        # 8. The gate stack. One trade at a time, and the venue nets, so a break arriving while a
        #    trade is still open is skipped rather than opening a second position.
        can_work = (
            session.opening_range_done
            and session.regime_ok
            and adx_ok
            and stop_ticks >= 1
            and self._opening_range_end < bar_time <= self._last_entry
            and (self._entry is None or self._entry.completed)
            and session.trades < self.config.max_trades_per_day
        )
        if not can_work:
            return

        # 9. The first bar CLOSING beyond the range, in the biased direction. The two filters sit
        #    inside the break branch: a rejected break consumes nothing, so a later bar closing
        #    beyond the same level can still trigger, and the level itself is never spent.
        if close > session.opening_high:
            if not range_ok or session.bias != TradeDirection.LONG:
                return

            direction = TradeDirection.LONG
        elif close < session.opening_low:
            if not range_ok or session.bias != TradeDirection.SHORT:
                return

            direction = TradeDirection.SHORT
        else:
            return

        # 10. Both exits are measured from this bar's close, not from the fill, which is the reverse
        #     of the other strategies here. The trade counter moves on the decision, whether or not
        #     the order goes on to fill.
        stop_price, take_profit_price = _exit_prices(
            instrument=self._instrument,
            direction=direction,
            close=close,
            stop_points=stop_points,
            take_profit_rr=self.config.take_profit_rr,
        )
        session.trades += 1
        self.log.info("long_signal" if direction == TradeDirection.LONG else "short_signal")

        entry = Entry(
            strategy=self,
            instrument=self._instrument,
            direction=direction,
            stop_price=stop_price,
            take_profit_price=take_profit_price,
            quantity=self._instrument.make_qty(self.config.contracts),
            flatten_ns=self._boundary_ns(bar_close.date(), self._session_cutoff),
        )
        self._entry = entry
        self._add_stratlet(entry)
