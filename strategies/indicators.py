from nautilus_trader.indicators.averages import WilderMovingAverage
from nautilus_trader.model.data import Bar


PERCENT = 100.0


class WilderAdx:
    # Nautilus ships no ADX. DirectionalMovement smooths the raw directional movement without
    # dividing it by the true range and never forms DX, so it is a different number. The specs that
    # use this ask for the platform's native ADX and say why - a simple-average lookalike passes
    # different bars - and that native smoothing is Wilder's, which is what WilderMovingAverage is.
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
