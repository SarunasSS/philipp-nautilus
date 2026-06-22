from datetime import UTC
from datetime import datetime


from nautilus_trader.config import StrategyConfig
from nautilus_trader.model.data import Bar
from nautilus_trader.model.data import BarType
from nautilus_trader.trading.strategy import Strategy


class SubscribeStrategyConfig(StrategyConfig, frozen=True):
    bar_type: str


class SubscribeStrategy(Strategy):
    def __init__(self, config: SubscribeStrategyConfig) -> None:
        super().__init__(config)
        self._bar_type = BarType.from_str(config.bar_type)

    def on_start(self) -> None:
        self.subscribe_bars(self._bar_type)

    def on_bar(self, bar: Bar) -> None:
        ts_event = datetime.fromtimestamp(bar.ts_event / 1_000_000_000, UTC)
        self.log.info(f"Received bar: {ts_event.isoformat()} {bar}")

    def on_stop(self) -> None:
        self.unsubscribe_bars(self._bar_type)
