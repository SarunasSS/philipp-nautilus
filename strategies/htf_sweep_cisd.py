from dataclasses import dataclass


from nautilus_trader.config import StrategyConfig
from nautilus_trader.model.data import Bar
from nautilus_trader.model.data import BarType
from nautilus_trader.trading.strategy import Strategy


class HTFSweepCISDStrategyConfig(StrategyConfig, frozen=True):
    htf_bar_type: str
    ltf_bar_type: str
    comparison_tolerance: float = 0.0


@dataclass(frozen=True)
class SignalCandidate:
    swing_price: float
    cisd_level: float


@dataclass
class SignalSideState:
    candidate: SignalCandidate | None = None
    blocked: bool = False


class HTFSweepCISDStrategy(Strategy):
    def __init__(self, config: HTFSweepCISDStrategyConfig) -> None:
        super().__init__(config)

        self._ltf_bar_type = BarType.from_str(config.ltf_bar_type)
        self._htf_bar_type = BarType.from_str(config.htf_bar_type)
        self._comparison_tolerance = config.comparison_tolerance
        self._validate_bar_types()

        self._htf_bars: list[Bar] = []
        self._ltf_bars: list[Bar] = []

        self._short = SignalSideState()
        self._long = SignalSideState()

    def on_start(self) -> None:
        self.subscribe_bars(self._ltf_bar_type)
        self.subscribe_bars(self._htf_bar_type)

    def on_bar(self, bar: Bar) -> None:
        if bar.bar_type.standard() == self._htf_bar_type.standard():
            self._htf_bars.append(bar)
            del self._htf_bars[:-3]
            self._ltf_bars = [ltf_bar for ltf_bar in self._ltf_bars if ltf_bar.ts_event > bar.ts_event]
            self._short = SignalSideState()
            self._long = SignalSideState()
            return

        if bar.bar_type.standard() != self._ltf_bar_type.standard():
            return

        last_ltf_bar = bar
        self._ltf_bars.append(last_ltf_bar)
        if not self._htf_bars:
            return

        self._evaluate_signal(last_ltf_bar)

    def on_stop(self) -> None:
        self.unsubscribe_bars(self._htf_bar_type)
        self.unsubscribe_bars(self._ltf_bar_type)

    def _validate_bar_types(self) -> None:
        if self._htf_bar_type.instrument_id != self._ltf_bar_type.instrument_id:
            raise ValueError("htf_bar_type and ltf_bar_type must use the same instrument")
        if not self._htf_bar_type.is_composite():
            raise ValueError("htf_bar_type must be passed as a composite bar type")
        if not self._htf_bar_type.is_internally_aggregated():
            raise ValueError("htf_bar_type must be internally aggregated")
        if not self._ltf_bar_type.is_externally_aggregated():
            raise ValueError("ltf_bar_type must be externally aggregated")
        if not self._htf_bar_type.spec.is_time_aggregated() or not self._ltf_bar_type.spec.is_time_aggregated():
            raise ValueError("htf_bar_type and ltf_bar_type must be time-aggregated bars")

        htf_interval_ns = self._htf_bar_type.spec.get_interval_ns()
        ltf_interval_ns = self._ltf_bar_type.spec.get_interval_ns()
        if htf_interval_ns <= ltf_interval_ns:
            raise ValueError("htf_bar_type interval must be greater than ltf_bar_type interval")
        if htf_interval_ns % ltf_interval_ns != 0:
            raise ValueError("htf_bar_type interval must be an exact multiple of ltf_bar_type interval")
        if self._htf_bar_type.composite().standard() != self._ltf_bar_type.standard():
            raise ValueError("htf_bar_type composite source must match ltf_bar_type")
        if not 0 <= self._comparison_tolerance <= 1:
            raise ValueError("comparison_tolerance must be between 0 and 1")

    def _evaluate_signal(self, last_ltf_bar: Bar) -> None:
        if len(self._ltf_bars) < 3:
            return

        tolerance = self._comparison_tolerance
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

        # 3. Invalidate or confirm the pending short setup.
        if self._short.candidate is not None:
            if last_ltf_bar.high.as_double() > self._short.candidate.swing_price * (1 + tolerance):
                self._short.candidate = None
                self._short.blocked = True
            elif last_ltf_bar.close.as_double() < self._short.candidate.cisd_level * (1 - tolerance):
                self.log.info("Short_signal")
                self._short.candidate = None
                self._short.blocked = True

        # 4. Invalidate or confirm the pending long setup.
        if self._long.candidate is not None:
            if last_ltf_bar.low.as_double() < self._long.candidate.swing_price * (1 - tolerance):
                self._long.candidate = None
                self._long.blocked = True
            elif last_ltf_bar.close.as_double() > self._long.candidate.cisd_level * (1 + tolerance):
                self.log.info("long_signal")
                self._long.candidate = None
                self._long.blocked = True
