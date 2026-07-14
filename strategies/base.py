import asyncio

from collections.abc import Coroutine
from dataclasses import dataclass
from dataclasses import field


from nautilus_trader.config import StrategyConfig
from nautilus_trader.model.data import Bar
from nautilus_trader.model.events import OrderEvent
from nautilus_trader.model.events import PositionEvent
from nautilus_trader.trading.strategy import Strategy


class BaseStrategyConfig(StrategyConfig, frozen=True, kw_only=True):
    live: bool = False


@dataclass(kw_only=True)
class Stratlet:
    strategy: Strategy
    poll_delay: float = 0.0
    task: asyncio.Task[None] | None = field(default=None, init=False)
    coroutine: Coroutine[object, object, None] = field(init=False)
    completed: bool = field(default=False, init=False)

    async def start(self) -> None:
        try:
            await self.on_start()
        except asyncio.CancelledError:
            await self.on_stop()
            raise
        except Exception as exc:
            self.strategy.log.error(f"{type(self).__name__} error: {exc}")
            try:
                await self.on_stop()
            except Exception as cleanup_exc:
                self.strategy.log.error(f"{type(self).__name__} cleanup error: {cleanup_exc}")
        finally:
            self.completed = True

    async def on_start(self) -> None:
        raise NotImplementedError

    async def on_stop(self, wait_for_commands: bool = True) -> None:
        raise NotImplementedError


class BaseStrategy(Strategy):
    def __init__(self, config: BaseStrategyConfig) -> None:
        super().__init__(config)

        self._entry_loop: asyncio.AbstractEventLoop | None = None
        self._stratlets: list[Stratlet] = []
        self._updating = False
        self._update_pending = False
        self._shutdown_future: asyncio.Future[list[None | BaseException]] | None = None

    def on_start(self) -> None:
        try:
            self._entry_loop = asyncio.get_running_loop()
        except RuntimeError:
            self._entry_loop = None

        if self.config.live and self._entry_loop is None:
            raise RuntimeError("Live strategy requires the Nautilus asyncio event loop")

    def on_bar(self, bar: Bar) -> None:
        self._update()

    def on_order_event(self, event: OrderEvent) -> None:
        self._update()

    def on_position_event(self, event: PositionEvent) -> None:
        self._update()

    def on_stop(self) -> None:
        if self.config.live and self._entry_loop is not None and self._entry_loop.is_running():
            self._shutdown_future = asyncio.gather(
                *(stratlet.on_stop() for stratlet in self._stratlets),
                return_exceptions=True,
            )
            return

        # Backtest venue commands settle only after the synchronous stop callback returns.
        stop_coroutines = [stratlet.on_stop(wait_for_commands=False) for stratlet in self._stratlets]
        for coroutine in stop_coroutines:
            try:
                coroutine.send(None)
            except StopIteration:
                continue

            coroutine.close()

        for stratlet in self._stratlets:
            if stratlet.task is None:
                stratlet.coroutine.close()

        self._stratlets = []

    def _add_stratlet(self, stratlet: Stratlet) -> None:
        loop = None
        if self.config.live:
            loop = self._entry_loop
            if loop is None or loop.is_closed():
                raise RuntimeError("Live strategy requires an active Nautilus asyncio event loop")

        stratlet.poll_delay = 0.001 if self.config.live else 0.0
        stratlet.coroutine = stratlet.start()
        self._stratlets.append(stratlet)

        if loop is not None:
            stratlet.task = loop.create_task(stratlet.coroutine)

    def _update(self) -> None:
        if self.config.live:
            self._stratlets = [stratlet for stratlet in self._stratlets if not stratlet.completed]
            return

        if self._updating:
            self._update_pending = True
            return

        self._updating = True
        try:
            while True:
                self._update_pending = False
                for stratlet in self._stratlets:
                    try:
                        stratlet.coroutine.send(None)
                    except StopIteration:
                        pass

                self._stratlets = [stratlet for stratlet in self._stratlets if not stratlet.completed]
                if not self._update_pending:
                    break
        finally:
            self._updating = False
