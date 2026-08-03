from datetime import UTC
from datetime import datetime


from nautilus_trader.config import StrategyConfig
from nautilus_trader.model.data import Bar
from nautilus_trader.model.data import BarType
from nautilus_trader.model.instruments import Instrument
from nautilus_trader.trading.strategy import Strategy


class SubscribeStrategyConfig(StrategyConfig, frozen=True):
    bar_type: str


class SubscribeStrategy(Strategy):
    def __init__(self, config: SubscribeStrategyConfig) -> None:
        super().__init__(config)
        self._bar_type = BarType.from_str(config.bar_type)
        self._subscribed = False

    def on_start(self) -> None:
        self.request_instrument(self._bar_type.instrument_id)

    def on_instrument(self, instrument: Instrument) -> None:
        if instrument.id != self._bar_type.instrument_id or self._subscribed:
            return

        self.subscribe_bars(self._bar_type)
        self._subscribed = True

    def on_bar(self, bar: Bar) -> None:
        ts_event = datetime.fromtimestamp(bar.ts_event / 1_000_000_000, UTC)
        self.log.info(f"Received bar: {ts_event.isoformat()} {bar}")

    def on_stop(self) -> None:
        self._subscribed = False
