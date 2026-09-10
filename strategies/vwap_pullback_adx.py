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
from strategies.indicators import WilderAdx


# One 1-minute stream of one instrument over the whole 24-hour session. The structure comes from
# time windows rather than timeframes: an opening range that fixes the day's bias, a session VWAP
# that is the reference price, an entry window that says when the trigger may fire. Sessions are
# plain calendar days in the session timezone, which is what the spec's 23:00 CT reset means on an
# Exchange-time chart: midnight New York. The pivots and the ADX are market structure and survive
# the roll; everything else is wiped.
SECONDS_PER_HOUR = 3_600
SECONDS_PER_MINUTE = 60
STRATEGY_TAG = "VWAP_PULLBACK_ADX"


class TradeDirection(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"


class RetestMode(str, Enum):
    # A wick into the VWAP latches; a close back across it is the signal.
    TOUCH = "TOUCH"
    # A close through the VWAP latches; a close back across it is the signal. Deeper pullback,
    # fewer trades. The spec's research option.
    CLOSE_THROUGH = "CLOSE_THROUGH"


class StopMode(str, Enum):
    # The last confirmed swing of stop_swing_length strength, wherever on the chart it was.
    PIVOT = "PIVOT"
    # The extreme printed since the retest latch was set. Tighter, more stops. The research option.
    PULLBACK_EXTREME = "PULLBACK_EXTREME"


class VwapPullbackAdxStrategyConfig(BaseStrategyConfig, frozen=True):
    bar_type: str
    session_timezone: str = "America/New_York"
    # All three windows select bars by their closing stamp, after the start and up to and including
    # the end, which is the one convention the spec uses for all of them.
    opening_range_window: str = "09:30-10:00"
    vwap_window: str = "09:30-18:00"
    entry_window: str = "10:00-18:00"
    eod_flat: bool = True
    eod_flat_time: str = "16:55"
    retest_mode: str = RetestMode.TOUCH.value
    stop_mode: str = StopMode.PIVOT.value
    stop_swing_length: int = 20
    target_swing_length: int = 5
    stop_buffer_points: float = 0.0
    max_trades_per_day: int = 1
    trade_longs: bool = True
    trade_shorts: bool = False
    min_rr: float = 0.0
    max_stop_points: float = 0.0
    use_adx_elevation: bool = True
    adx_elevation_threshold: float = 20.0
    use_adx_decay: bool = True
    adx_decay_lookback: int = 1
    adx_length: int = 14
    contracts: int = 1


def _parse_time(value: str, field_name: str) -> time:
    try:
        return time.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be an ISO 8601 time such as 16:55") from exc


def _parse_time_range(value: str, field_name: str) -> tuple[time, time]:
    parts = value.split("-")
    if len(parts) != 2:
        raise ValueError(f"{field_name} must be an ISO 8601 time range such as 09:30-10:00")

    return _parse_time(parts[0].strip(), field_name), _parse_time(parts[1].strip(), field_name)


def _parse_timezone(value: str) -> ZoneInfo:
    try:
        return ZoneInfo(value)
    except (KeyError, ValueError) as exc:
        raise ValueError("session_timezone must be an IANA timezone name such as America/New_York") from exc


def _seconds_since_midnight(value: time) -> int:
    return (value.hour * SECONDS_PER_HOUR) + (value.minute * SECONDS_PER_MINUTE) + value.second


def _typical_price(bar: Bar) -> float:
    return (bar.high.as_double() + bar.low.as_double() + bar.close.as_double()) / 3


def _points_to_ticks(points: float, tick_size: float) -> int:
    # Half up, so a distance landing between two ticks resolves the same way everywhere. make_price
    # rounds to the price precision rather than to the increment, so working in whole ticks is what
    # keeps every derived price on the grid.
    return math.floor((points / tick_size) + 0.5)


class PivotDetector:
    # The spec's four High[i] / Low[i] scans, one detector per strength. A pivot high of strength N
    # is a bar whose high is strictly above the highs of the N bars on each side, confirmed N bars
    # after it forms; ties disqualify. Each detector remembers only the most recent confirmed pivot,
    # and the has_ flags never reset: once a pivot has ever existed, a most recent one always does.
    def __init__(self, strength: int) -> None:
        self._strength = strength
        self._window: deque[tuple[float, float]] = deque(maxlen=(2 * strength) + 1)
        self.last_high = 0.0
        self.last_low = 0.0
        self.has_high = False
        self.has_low = False

    def handle_bar(self, bar: Bar) -> None:
        self._window.append((bar.high.as_double(), bar.low.as_double()))
        if len(self._window) < self._window.maxlen:
            return

        centre_high, centre_low = self._window[self._strength]
        others = [pair for index, pair in enumerate(self._window) if index != self._strength]
        if all(high < centre_high for high, _ in others):
            self.last_high = centre_high
            self.has_high = True
        if all(low > centre_low for _, low in others):
            self.last_low = centre_low
            self.has_low = True


@dataclass
class SessionState:
    # The 0 and inf seeds are the spec's sentinels. orb_seen and vwap_seen replace its
    # inX and inX[1] = false transition tests, which on a chart with a bar at every minute are the
    # same thing as "the first traded bar of this session inside the window".
    trades: int = 0
    orb_high: float = 0.0
    orb_low: float = math.inf
    orb_seen: bool = False
    orb_ready: bool = False
    vwap_price_volume: float = 0.0
    vwap_volume: float = 0.0
    vwap: float = 0.0
    vwap_seen: bool = False
    # The six latches. Two one-way switches set by a close beyond the range, and four retest
    # latches, one pair per retest mode. Cleared only by an entry or by the roll.
    long_broken: bool = False
    short_broken: bool = False
    long_touched: bool = False
    short_touched: bool = False
    long_closed_through: bool = False
    short_closed_through: bool = False
    # The pullback extreme since the latch was set, read by StopMode.PULLBACK_EXTREME only. The
    # previous-bar latch state is what seeds it on the transition, as the spec's longLatched[1] does.
    pullback_low: float = 0.0
    pullback_high: float = 0.0
    long_latched_previous: bool = False
    short_latched_previous: bool = False


@dataclass
class Entry(Stratlet):
    instrument: Instrument
    direction: TradeDirection
    stop_price: Price
    take_profit_price: Price
    quantity: Quantity
    # None when the spec's UseEODFlat is off, and the bracket then rests until it resolves.
    flatten_ns: int | None
    orders: dict[str, Order] = field(default_factory=dict, init=False)
    position: Position | None = field(default=None, init=False)
    stopping: bool = field(default=False, init=False)
    stopped: bool = field(default=False, init=False)

    async def on_start(self) -> None:
        # The spec sends the entry at market on the next bar. The venue matches a bar before the
        # strategy sees it, so this order settles against the book the signal bar left behind and
        # fills at that bar's close.
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
            if self._past_flatten():
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

        # Both levels were frozen on the signal bar - the spec's activeStop and activeTgt - so they
        # exist before the entry is sent. They go out after the fill only because the position id
        # does not exist until then, and because the exit quantity has to match what actually
        # filled. The fill event pumps this coroutine inside the same venue drain that produced it,
        # so the exits are accepted before the next source bar is processed.
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
            if self._past_flatten():
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

    def _past_flatten(self) -> bool:
        # The flatten reads the clock rather than a bar's closing stamp, so it fires on the first
        # pump at or after the boundary even when the venue has gone quiet.
        if self.flatten_ns is None:
            return False

        return self.strategy.clock.timestamp_ns() >= self.flatten_ns

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


class VwapPullbackAdxStrategy(BaseStrategy):
    def __init__(self, config: VwapPullbackAdxStrategyConfig) -> None:
        super().__init__(config)

        self._bar_type = BarType.from_str(config.bar_type)
        self._timezone = _parse_timezone(config.session_timezone)
        self._orb_start, self._orb_end = _parse_time_range(config.opening_range_window, "opening_range_window")
        self._vwap_start, self._vwap_end = _parse_time_range(config.vwap_window, "vwap_window")
        self._entry_start, self._entry_end = _parse_time_range(config.entry_window, "entry_window")
        self._eod_flat_time = _parse_time(config.eod_flat_time, "eod_flat_time")
        self._retest_mode = self._parse_enum(RetestMode, config.retest_mode, "retest_mode")
        self._stop_mode = self._parse_enum(StopMode, config.stop_mode, "stop_mode")
        self._validate_config()

        self._instrument: Instrument | None = None

        # Market structure: the only state that survives the daily roll. The ADX history holds the
        # values the decay test compares, the current one last.
        self._adx = WilderAdx(period=config.adx_length)
        self._adx_history: deque[float] = deque(maxlen=config.adx_decay_lookback + 1)
        self._stop_pivot = PivotDetector(strength=config.stop_swing_length)
        self._target_pivot = PivotDetector(strength=config.target_swing_length)

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

    @staticmethod
    def _parse_enum[E: Enum](enum_type: type[E], value: str, field_name: str) -> E:
        try:
            return enum_type(value.upper())
        except ValueError as exc:
            allowed = ", ".join(member.value for member in enum_type)
            raise ValueError(f"{field_name} must be one of: {allowed}") from exc

    def _validate_config(self) -> None:
        self._validate_bar_type()

        for start, end, field_name in (
            (self._orb_start, self._orb_end, "opening_range_window"),
            (self._vwap_start, self._vwap_end, "vwap_window"),
            (self._entry_start, self._entry_end, "entry_window"),
        ):
            if start >= end:
                raise ValueError(f"{field_name} start must be earlier than its end")
        if self._eod_flat_time <= self._entry_start:
            raise ValueError("eod_flat_time must be later than entry_window starts")
        if self.config.stop_swing_length < 1:
            raise ValueError("stop_swing_length must be positive")
        if self.config.target_swing_length < 1:
            raise ValueError("target_swing_length must be positive")
        if self.config.stop_buffer_points < 0:
            raise ValueError("stop_buffer_points must not be negative")
        if self.config.max_trades_per_day < 1:
            raise ValueError("max_trades_per_day must be positive")
        if not self.config.trade_longs and not self.config.trade_shorts:
            raise ValueError("trade_longs and trade_shorts must not both be disabled")
        if self.config.min_rr < 0:
            raise ValueError("min_rr must not be negative")
        if self.config.max_stop_points < 0:
            raise ValueError("max_stop_points must not be negative")
        if self.config.adx_elevation_threshold < 0:
            raise ValueError("adx_elevation_threshold must not be negative")
        if self.config.adx_decay_lookback < 1:
            raise ValueError("adx_decay_lookback must be positive")
        if self.config.adx_length < 1:
            raise ValueError("adx_length must be positive")
        if self.config.contracts < 1:
            raise ValueError("contracts must be positive")

    def _validate_bar_type(self) -> None:
        if not self._bar_type.spec.is_time_aggregated():
            raise ValueError("bar_type must be a time-aggregated bar type")
        # A plain external bar type is matched against its own bars, which is what the live client
        # delivers. A composite is aggregated from a finer catalog source, and that source is then
        # the bar magnifier the venue checks the bracket against.
        if self._bar_type.is_composite():
            if not self._bar_type.is_internally_aggregated():
                raise ValueError("bar_type must be internally aggregated when it names a composite source")
            if not self._bar_type.composite().is_externally_aggregated():
                raise ValueError("bar_type composite source must be externally aggregated")
        elif not self._bar_type.is_externally_aggregated():
            raise ValueError("bar_type must be externally aggregated when it names no composite source")

        # Every window boundary is compared against a bar's closing stamp, and the flatten deadline
        # is the entry gate's exclusive end as well as what the clock is read against, so one that
        # falls inside a bar moves the partition by up to a bar without erroring.
        interval_seconds = self._bar_type.spec.get_interval_ns() // 1_000_000_000
        for boundary, field_name in (
            (self._orb_start, "opening_range_window"),
            (self._orb_end, "opening_range_window"),
            (self._vwap_start, "vwap_window"),
            (self._vwap_end, "vwap_window"),
            (self._entry_start, "entry_window"),
            (self._entry_end, "entry_window"),
            (self._eod_flat_time, "eod_flat_time"),
        ):
            if _seconds_since_midnight(boundary) % interval_seconds != 0:
                raise ValueError(f"{field_name} must fall on a {interval_seconds}s bar boundary")

    def _session_local(self, bar: Bar) -> datetime:
        return datetime.fromtimestamp(bar.ts_event / 1_000_000_000, UTC).astimezone(self._timezone)

    def _boundary_ns(self, session_date: date, boundary: time) -> int:
        return int(datetime.combine(session_date, boundary, tzinfo=self._timezone).timestamp() * 1_000_000_000)

    def _stop_price(self, level: float, direction: TradeDirection) -> Price:
        # The level is a bar extreme and already sits on the grid; the buffer is what may not, so it
        # is applied in whole ticks.
        if self._instrument is None:
            raise RuntimeError("Instrument is not initialized")

        tick_size = self._instrument.price_increment.as_double()
        offset = _points_to_ticks(self.config.stop_buffer_points, tick_size) * tick_size
        if direction == TradeDirection.LONG:
            return self._instrument.make_price(level - offset)

        return self._instrument.make_price(level + offset)

    def _roll_session(self, session_date: date) -> None:
        # The spec's 23:00 CT transition: the trade counter, the range flags and all six latches go;
        # the pivots and the ADX stay, and so does an open position, which is why the flatten is a
        # separate rule read from the clock inside the entry.
        self._session_date = session_date
        self._session = SessionState()

    def _adx_ok(self) -> bool:
        # Both switches short-circuit to true when off. Elevation says the last adx_length bars had
        # directional character; decay says that pressure is not accelerating on this bar.
        if len(self._adx_history) < self._adx_history.maxlen:
            return False

        value = self._adx_history[-1]
        elevation_ok = not self.config.use_adx_elevation or value >= self.config.adx_elevation_threshold
        decay_ok = not self.config.use_adx_decay or value <= self._adx_history[0]

        return elevation_ok and decay_ok

    def _on_session_bar(self, bar: Bar) -> None:
        if self._instrument is None:
            raise RuntimeError("Instrument is not initialized")

        bar_close = self._session_local(bar)
        if bar_close.date() != self._session_date:
            self._roll_session(bar_close.date())

        # Internal aggregation emits a flat, volumeless bar for every interval the venue was shut,
        # and those bars do not exist on the chart the spec was written against.
        if bar.volume.as_double() <= 0:
            return

        session = self._session
        bar_time = bar_close.time()
        high = bar.high.as_double()
        low = bar.low.as_double()
        close = bar.close.as_double()
        volume = bar.volume.as_double()

        # 1. The gate, computed on every bar so that it is always the state of the current bar when
        #    the entry block reads it.
        self._adx.handle_bar(bar)
        if self._adx.initialized:
            self._adx_history.append(self._adx.value)

        # 2. The three windows, one convention: a bar belongs if its closing stamp is after the
        #    start and at or before the end.
        in_orb = self._orb_start < bar_time <= self._orb_end
        in_vwap = self._vwap_start < bar_time <= self._vwap_end
        in_entry = self._entry_start < bar_time <= self._entry_end

        # 3. The opening range: seed on the first bar inside, extend on every bar inside, mark
        #    final on the first bar outside. Not ready between the roll and the window's end, so no
        #    stale range from yesterday can be broken this morning.
        if in_orb:
            if session.orb_seen:
                session.orb_high = max(session.orb_high, high)
                session.orb_low = min(session.orb_low, low)
            else:
                session.orb_high = high
                session.orb_low = low
                session.orb_seen = True
                session.orb_ready = False
        elif session.orb_seen:
            session.orb_ready = True

        # 4. The session VWAP, re-anchored on the first bar of its window and invalid outside it -
        #    which is what silences the entry logic overnight without an extra time check.
        if in_vwap:
            if session.vwap_seen:
                session.vwap_price_volume += _typical_price(bar) * volume
                session.vwap_volume += volume
            else:
                session.vwap_price_volume = _typical_price(bar) * volume
                session.vwap_volume = volume
                session.vwap_seen = True
        vwap_valid = in_vwap and session.vwap_volume > 0
        if vwap_valid:
            session.vwap = session.vwap_price_volume / session.vwap_volume

        # 5. Pivots, on every bar of the 24-hour stream.
        self._stop_pivot.handle_bar(bar)
        self._target_pivot.handle_bar(bar)

        # 6. Break latching: two one-way switches set by a close, cleared only by an entry or the
        #    roll. A day that closes back inside the range keeps its bias.
        can_track_break = session.orb_ready and not in_orb
        if can_track_break and close > session.orb_high:
            session.long_broken = True
        if can_track_break and close < session.orb_low:
            session.short_broken = True

        # 7. Retest and signal. The latches are sticky; the signals are states rebuilt every bar.
        #    A signal that meets a closed gate is not lost - it is still there on the next bar.
        can_look_for_entry = can_track_break and in_vwap and in_entry and vwap_valid
        long_signal = False
        short_signal = False
        if can_look_for_entry and session.long_broken:
            if self._retest_mode == RetestMode.TOUCH:
                if low <= session.vwap:
                    session.long_touched = True
                long_signal = session.long_touched and close > session.vwap
            else:
                if close < session.vwap:
                    session.long_closed_through = True
                long_signal = session.long_closed_through and close > session.vwap
        if can_look_for_entry and session.short_broken:
            if self._retest_mode == RetestMode.TOUCH:
                if high >= session.vwap:
                    session.short_touched = True
                short_signal = session.short_touched and close < session.vwap
            else:
                if close > session.vwap:
                    session.short_closed_through = True
                short_signal = session.short_closed_through and close < session.vwap

        # 8. The pullback extreme since the latch was set, seeded on the transition and extended
        #    while the latch holds. Tracked in both stop modes, read in one.
        long_latched = session.long_touched or session.long_closed_through
        short_latched = session.short_touched or session.short_closed_through
        if long_latched and not session.long_latched_previous:
            session.pullback_low = low
        elif long_latched and low < session.pullback_low:
            session.pullback_low = low
        if short_latched and not session.short_latched_previous:
            session.pullback_high = high
        elif short_latched and high > session.pullback_high:
            session.pullback_high = high
        session.long_latched_previous = long_latched
        session.short_latched_previous = short_latched

        # 9. Levels, recomputed every bar from the freshest pivots but frozen into the bracket only
        #    on the bar the entry fires. The stop level here is the raw extreme; the buffer is
        #    applied when the price is built.
        if self._stop_mode == StopMode.PULLBACK_EXTREME:
            long_stop_level = session.pullback_low
            short_stop_level = session.pullback_high
        else:
            long_stop_level = self._stop_pivot.last_low
            short_stop_level = self._stop_pivot.last_high
        buffer = self.config.stop_buffer_points
        long_stop = long_stop_level - buffer
        short_stop = short_stop_level + buffer
        long_target = self._target_pivot.last_high
        short_target = self._target_pivot.last_low

        # 10. Validity: both pivots must exist, the stop below price and the target above it, and
        #     the two research filters short-circuit to true at zero.
        pivot_stop = self._stop_mode == StopMode.PIVOT
        max_stop = self.config.max_stop_points
        min_rr = self.config.min_rr
        valid_long = (
            (not pivot_stop or self._stop_pivot.has_low)
            and self._target_pivot.has_high
            and (pivot_stop or long_latched)
            and long_stop < close < long_target
            and (max_stop == 0 or close - long_stop <= max_stop)
            and (min_rr == 0 or (long_target - close) / (close - long_stop) >= min_rr)
        )
        valid_short = (
            (not pivot_stop or self._stop_pivot.has_high)
            and self._target_pivot.has_low
            and (pivot_stop or short_latched)
            and short_target < close < short_stop
            and (max_stop == 0 or short_stop - close <= max_stop)
            and (min_rr == 0 or (close - short_target) / (short_stop - close) >= min_rr)
        )

        # 11. The gate stack: flat, budget, then side by side. One trade at a time and the venue
        #     nets, so a signal arriving while a trade is still open is delayed, not spent. The
        #     one departure from the spec is the flatten term: with the flatten on, a signal
        #     closing at or after the flatten time is refused rather than entered and flattened a
        #     minute later.
        if self._entry is not None and not self._entry.completed:
            return
        if session.trades >= self.config.max_trades_per_day:
            return
        if self.config.eod_flat and bar_time >= self._eod_flat_time:
            return
        if not self._adx_ok():
            return

        if long_signal and valid_long and self.config.trade_longs:
            direction = TradeDirection.LONG
            stop_price = self._stop_price(long_stop_level, direction)
            take_profit_price = self._instrument.make_price(long_target)
            stop_distance = close - long_stop
        elif short_signal and valid_short and self.config.trade_shorts:
            direction = TradeDirection.SHORT
            stop_price = self._stop_price(short_stop_level, direction)
            take_profit_price = self._instrument.make_price(short_target)
            stop_distance = short_stop - close
        else:
            return

        # 12. In the same breath the bracket is frozen, the diagnostic line goes out, the counter
        #     ticks and every latch is cleared: the setup is spent whether or not the order fills.
        session.trades += 1
        session.long_broken = False
        session.short_broken = False
        session.long_touched = False
        session.short_touched = False
        session.long_closed_through = False
        session.short_closed_through = False
        self.log.info("long_signal" if direction == TradeDirection.LONG else "short_signal")
        self.log.info(
            f"{bar_close:%Y-%m-%d %H:%M} {direction.value} qty={self.config.contracts} close={close:.2f} "
            f"stop={stop_price} tgt={take_profit_price} stopDist={stop_distance:.2f} "
            f"adx={self._adx_history[-1]:.2f}",
        )

        entry = Entry(
            strategy=self,
            instrument=self._instrument,
            direction=direction,
            stop_price=stop_price,
            take_profit_price=take_profit_price,
            quantity=self._instrument.make_qty(self.config.contracts),
            flatten_ns=self._boundary_ns(bar_close.date(), self._eod_flat_time) if self.config.eod_flat else None,
        )
        self._entry = entry
        self._add_stratlet(entry)
