import asyncio
import math


from collections import deque
from dataclasses import dataclass
from dataclasses import field
from datetime import UTC
from datetime import date
from datetime import datetime
from datetime import time
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


# One stream of one instrument, long only. The composite source is the spec's bar magnifier: the
# venue matches resting orders against the bars the engine is fed, so a 30-minute signal built from
# 1-minute catalog bars gets its exits checked every minute - which is what lets a trade open and
# close inside one signal bar, as the source's own trade list does. Sessions are plain calendar days
# in the session timezone, which is what the spec's date reset means; replaying the rules over this
# catalog reproduces 42 of the source's 43 reference trades on an Eastern-keyed day against 39 on a
# Central-keyed one, so Eastern is the reading implemented here.
SECONDS_PER_HOUR = 3_600
SECONDS_PER_MINUTE = 60
STRATEGY_TAG = "VAULT_BREAK"


class VaultBreakStrategyConfig(BaseStrategyConfig, frozen=True):
    bar_type: str
    session_timezone: str = "America/New_York"
    earliest_entry: str = "11:00"
    flatten_time: str = "15:30"
    noise_multiple: float = 0.3
    boundary_atr_days: int = 15
    max_trades_per_day: int = 3
    vwap_exit_bars: int = 7
    take_profit_points: float = 40.0
    stop_loss_points: float = 75.0
    contracts: int = 1


def _parse_time(value: str, field_name: str) -> time:
    try:
        return time.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be an ISO 8601 time such as 11:00") from exc


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


def _exit_price(instrument: Instrument, entry_price: float, points: float, above: bool) -> Price:
    # Both legs are fixed distances from the fill rather than from the signal bar, which is what the
    # source's reference trade list shows: every target lands exactly 40 points above the entry price
    # and every stop exactly 75 below it. Neither level exists until the market entry has filled.
    tick_size = instrument.price_increment.as_double()
    offset = _points_to_ticks(points, tick_size) * tick_size
    if above:
        return instrument.make_price(entry_price + offset)

    return instrument.make_price(entry_price - offset)


@dataclass
class SessionState:
    # The 0 and inf seeds are the spec's 0 and 999999 sentinels. seen is what separates a session that
    # has had a traded bar from one that has not, and it guards every field beside it.
    open_price: float = 0.0
    high: float = 0.0
    low: float = math.inf
    seen: bool = False
    noise_up: float = 0.0
    vwap_price_volume: float = 0.0
    vwap_volume: float = 0.0
    vwap: float = 0.0
    bars_below_vwap: int = 0
    trades: int = 0


@dataclass
class Entry(Stratlet):
    instrument: Instrument
    stop_points: float
    take_profit_points: float
    quantity: Quantity
    flatten_ns: int
    # Set by the strategy when the spec's VWAP exit condition comes true. A stratlet may await nothing
    # but its own poll delay, so a rule decided outside the trade has to be polled rather than pushed,
    # exactly like the flatten deadline beside it.
    exit_requested: bool = field(default=False, init=False)
    orders: dict[str, Order] = field(default_factory=dict, init=False)
    position: Position | None = field(default=None, init=False)
    stopping: bool = field(default=False, init=False)
    stopped: bool = field(default=False, init=False)

    async def on_start(self) -> None:
        # The spec sends the entry at market on the next bar. The venue matches a bar before the
        # strategy sees it, so this order settles against the book the signal bar left behind and
        # fills at the open of the first magnifier bar after it - the price the source's own trade
        # list records.
        entry_order = self.strategy.order_factory.market(
            instrument_id=self.instrument.id,
            order_side=OrderSide.BUY,
            quantity=self.quantity,
            tags=[STRATEGY_TAG, "ENTRY"],
        )

        self.orders["entry"] = entry_order
        self.strategy.submit_order(entry_order)
        self.strategy.log.info(f"Submitted LONG MARKET entry {entry_order.client_order_id} qty={self.quantity}")

        while not self.stopping and not self.strategy.cache.is_order_closed(entry_order.client_order_id):
            exit_reason = self._exit_reason()
            if exit_reason is not None:
                self.strategy.log.info(f"{exit_reason}; canceling entry {entry_order.client_order_id}")
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

        entry_price = closed_entry_order.avg_px
        stop_price = _exit_price(self.instrument, entry_price, self.stop_points, above=False)
        take_profit_price = _exit_price(self.instrument, entry_price, self.take_profit_points, above=True)
        stop_order = self.strategy.order_factory.stop_market(
            instrument_id=self.instrument.id,
            order_side=OrderSide.SELL,
            quantity=closed_entry_order.filled_qty,
            trigger_price=stop_price,
            reduce_only=True,
            tags=[STRATEGY_TAG, "STOP_LOSS"],
        )
        take_profit_order = self.strategy.order_factory.limit(
            instrument_id=self.instrument.id,
            order_side=OrderSide.SELL,
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
            # Tested before the two polled rules, so a bracket leg that filled inside the signal bar
            # wins over an exit armed by that same bar, which is what the magnifier does in the spec.
            if filled_exit_order is not None:
                break
            if closed_orders:
                raise RuntimeError(f"Exit order {closed_orders[0].client_order_id} closed without fill")

            exit_reason = self._exit_reason()
            if exit_reason is not None:
                self.strategy.log.info(f"{exit_reason}; flattening position {self.position.id}")
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

    def _exit_reason(self) -> str | None:
        # Both of the spec's market exits, in the order it writes them. The flatten reads the clock
        # rather than a bar's closing stamp, so it fires on the first pump at or after the boundary
        # even when the venue has gone quiet.
        if self.exit_requested:
            return "VWAP exit requested"
        if self.strategy.clock.timestamp_ns() >= self.flatten_ns:
            return "Flat time reached"

        return None

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


class VaultBreakStrategy(BaseStrategy):
    def __init__(self, config: VaultBreakStrategyConfig) -> None:
        super().__init__(config)

        self._bar_type = BarType.from_str(config.bar_type)
        self._timezone = _parse_timezone(config.session_timezone)
        self._earliest_entry = _parse_time(config.earliest_entry, "earliest_entry")
        self._flatten_time = _parse_time(config.flatten_time, "flatten_time")
        self._validate_config()

        self._instrument: Instrument | None = None

        # The only state that survives the daily roll, and what makes the first sessions of any run
        # behave differently from the rest: nothing trades until the window holds boundary_atr_days
        # completed session ranges.
        self._session_ranges: deque[float] = deque(maxlen=config.boundary_atr_days)
        self._boundary_atr: float = 0.0

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

        if self._earliest_entry >= self._flatten_time:
            raise ValueError("earliest_entry must be earlier than flatten_time")
        if self.config.noise_multiple < 0:
            raise ValueError("noise_multiple must not be negative")
        if self.config.boundary_atr_days < 1:
            raise ValueError("boundary_atr_days must be positive")
        if self.config.max_trades_per_day < 1:
            raise ValueError("max_trades_per_day must be positive")
        if self.config.vwap_exit_bars < 1:
            raise ValueError("vwap_exit_bars must be positive")
        if self.config.take_profit_points <= 0:
            raise ValueError("take_profit_points must be positive")
        if self.config.stop_loss_points <= 0:
            raise ValueError("stop_loss_points must be positive")
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

        # Both boundaries partition bars by their closing stamp - flatten_time is the entry window's
        # exclusive end as well as the deadline the clock is read against - so one falling inside a
        # bar would move that partition by up to a bar without erroring.
        interval_seconds = self._bar_type.spec.get_interval_ns() // 1_000_000_000
        for boundary, field_name in (
            (self._earliest_entry, "earliest_entry"),
            (self._flatten_time, "flatten_time"),
        ):
            if _seconds_since_midnight(boundary) % interval_seconds != 0:
                raise ValueError(f"{field_name} must fall on a {interval_seconds}s bar boundary")

    def _session_local(self, bar: Bar) -> datetime:
        return datetime.fromtimestamp(bar.ts_event / 1_000_000_000, UTC).astimezone(self._timezone)

    def _boundary_ns(self, session_date: date, boundary: time) -> int:
        return int(datetime.combine(session_date, boundary, tzinfo=self._timezone).timestamp() * 1_000_000_000)

    def _roll_session(self, session_date: date) -> None:
        # The spec also sells any position still open here. That guard is unreachable in this engine:
        # the flatten deadline is read from the clock rather than from the bar stream, so it has
        # already fired and settled before any bar carrying the next date arrives.
        session = self._session
        if session.seen:
            session_range = session.high - session.low
            # The spec folds a session in only when its range is positive, and a bounded deque is its
            # ring buffer, write index and fill counter in one.
            if session_range > 0:
                self._session_ranges.append(session_range)

        # Read after the append, which is what makes the boundary_atr_days-th completed session the
        # one that arms the boundary rather than the one after it: at the shipped 15, the sixteenth
        # session of a run is the first that can trade.
        if len(self._session_ranges) == self.config.boundary_atr_days:
            self._boundary_atr = sum(self._session_ranges) / self.config.boundary_atr_days
        else:
            self._boundary_atr = 0.0

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
        # guard is harmless: a session with no traded bars contributes no range either way.
        if bar.volume.as_double() <= 0:
            return

        session = self._session
        bar_time = bar_close.time()
        close = bar.close.as_double()

        # 1. The session extremes, extended on every traded bar of the date rather than only inside
        #    the entry window, because their difference is what feeds tomorrow's boundary. The open
        #    and the boundary itself settle on the first traded bar rather than in _roll_session,
        #    since the bar that rolls the date can be one of the volumeless ones skipped above.
        if session.seen:
            session.high = max(session.high, bar.high.as_double())
            session.low = min(session.low, bar.low.as_double())
        else:
            session.open_price = bar.open.as_double()
            session.high = bar.high.as_double()
            session.low = bar.low.as_double()
            session.seen = True
            session.noise_up = session.open_price + (self.config.noise_multiple * self._boundary_atr)
            self.log.info(
                f"Session {self._session_date} open={session.open_price} "
                f"boundary_atr={self._boundary_atr:.2f} noise_up={session.noise_up:.2f}",
            )

        # 2. The session VWAP, anchored on the same date change and given no window at all by the
        #    spec. The guard above is what makes its divide-by-zero fallback to the close unreachable.
        session.vwap_price_volume += _typical_price(bar) * bar.volume.as_double()
        session.vwap_volume += bar.volume.as_double()
        session.vwap = session.vwap_price_volume / session.vwap_volume

        # 3. Consecutive closes below the anchor, reset by any close at or above it. An entry needs a
        #    close above the anchor, so the bar that opens a trade always leaves this at zero, which
        #    is what keeps the entry and the exit below mutually exclusive on any one bar.
        session.bars_below_vwap = session.bars_below_vwap + 1 if close < session.vwap else 0

        # 4. The entry. One position at a time and the venue nets, so a qualifying bar arriving while
        #    a trade is still open is skipped rather than opening a second position. The counter moves
        #    on the decision, whether or not the order goes on to fill.
        if (
            close > session.noise_up
            and close > session.vwap
            and self._earliest_entry <= bar_time < self._flatten_time
            and session.trades < self.config.max_trades_per_day
            and (self._entry is None or self._entry.completed)
            and len(self._session_ranges) == self.config.boundary_atr_days
            and self._boundary_atr > 0
        ):
            session.trades += 1
            self.log.info("long_signal")

            entry = Entry(
                strategy=self,
                instrument=self._instrument,
                stop_points=self.config.stop_loss_points,
                take_profit_points=self.config.take_profit_points,
                quantity=self._instrument.make_qty(self.config.contracts),
                flatten_ns=self._boundary_ns(bar_close.date(), self._flatten_time),
            )
            self._entry = entry
            self._add_stratlet(entry)

        # 5. The VWAP exit, after the entry so the file reads in the spec's own order. It has never
        #    fired on NQ at these settings - not once across the source's 43 reference trades, and not
        #    in a replay of this catalog, where the counter never passed 1 while a position was open.
        #    Seven consecutive closes below the anchor is three and a half hours inside a four and a
        #    half hour window, on a trade that opened above the anchor and resolves in a bar or two.
        if (
            self._entry is not None
            and not self._entry.completed
            and session.bars_below_vwap >= self.config.vwap_exit_bars
        ):
            self.log.info(f"VWAP exit armed after {session.bars_below_vwap} closes below {session.vwap:.2f}")
            self._entry.exit_requested = True
