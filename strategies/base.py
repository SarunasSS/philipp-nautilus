import asyncio

from collections.abc import Coroutine
from dataclasses import dataclass
from dataclasses import field


from nautilus_trader.config import StrategyConfig
from nautilus_trader.model.data import Bar
from nautilus_trader.model.data import TradeTick
from nautilus_trader.model.events import OrderEvent
from nautilus_trader.model.events import PositionEvent
from nautilus_trader.trading.strategy import Strategy


_BACKTEST_SHUTDOWN_STEPS = 64


class BaseStrategyConfig(StrategyConfig, frozen=True, kw_only=True):
    live: bool = False


@dataclass(kw_only=True)
class Stratlet:
    strategy: Strategy
    poll_delay: float = 0.0
    task: asyncio.Task[None] | None = field(default=None, init=False)
    coroutine: Coroutine[object, object, None] = field(init=False)
    completed: bool = field(default=False, init=False)
    stopping: bool = field(default=False, init=False)
    stopped: bool = field(default=False, init=False)
    _stop_in_progress: bool = field(default=False, init=False)
    _stop_failed: bool = field(default=False, init=False)
    _close_attempted: bool = field(default=False, init=False)

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
        if self.stopped:
            return
        if self._stop_in_progress:
            while not self.stopped:
                await asyncio.sleep(self.poll_delay)
            return

        self.stopping = True
        self._stop_in_progress = True
        try:
            await self._on_stop(wait_for_commands)
        except (asyncio.CancelledError, Exception) as exc:
            self._stop_failed = True
            self.strategy.log.error(f"{type(self).__name__} stop error: {exc}")
            try:
                await self._close_once(wait_for_commands)
            except Exception as close_exc:
                self.strategy.log.error(f"{type(self).__name__} close error: {close_exc}")
            raise
        finally:
            self.stopped = True
            self._stop_in_progress = False

    async def _on_stop(self, wait_for_commands: bool = True) -> None:
        raise NotImplementedError

    async def on_close(self, wait_for_commands: bool = True) -> None:
        pass

    async def _close_once(self, wait_for_commands: bool = True) -> None:
        if self._close_attempted:
            return

        self._close_attempted = True
        await self.on_close(wait_for_commands)


class BaseStrategy(Strategy):
    def __init__(self, config: BaseStrategyConfig) -> None:
        super().__init__(config)

        self._entry_loop: asyncio.AbstractEventLoop | None = None
        self._stratlets: list[Stratlet] = []
        self._updating = False
        self._update_pending = False
        self._shutdown_future: asyncio.Task[None] | None = None

    def on_start(self) -> None:
        try:
            self._entry_loop = asyncio.get_running_loop()
        except RuntimeError:
            self._entry_loop = None

        if self.config.live and self._entry_loop is None:
            raise RuntimeError("Live strategy requires the Nautilus asyncio event loop")

    def on_bar(self, bar: Bar) -> None:
        self._update()

    def on_trade_tick(self, tick: TradeTick) -> None:
        self._update()

    def on_order_event(self, event: OrderEvent) -> None:
        self._update()

    def on_position_event(self, event: PositionEvent) -> None:
        self._update()

    def on_stop(self) -> None:
        self._shutdown_stratlets()

    def on_dispose(self) -> None:
        self._shutdown_stratlets()

    def _shutdown_stratlets(self) -> None:
        if not self._stratlets or (self._shutdown_future is not None and not self._shutdown_future.done()):
            return

        stratlets = tuple(self._stratlets)
        if self.config.live and self._entry_loop is not None and self._entry_loop.is_running():
            self._shutdown_future = self._entry_loop.create_task(self._shutdown_live(stratlets))
            return

        # Backtest venue commands settle only after the synchronous callback returns.
        self._dispatch_backtest(stratlets, stop=True)
        self._dispatch_backtest(stratlets, stop=False)

        for stratlet in stratlets:
            if stratlet.task is None:
                try:
                    stratlet.coroutine.close()
                except Exception as exc:
                    self.log.error(f"{type(stratlet).__name__} task close error: {exc}")

        self._stratlets = []

    def _dispatch_backtest(self, stratlets: tuple[Stratlet, ...], stop: bool) -> None:
        for stratlet in stratlets:
            coroutine = (
                stratlet.on_stop(wait_for_commands=False)
                if stop
                else stratlet._close_once(wait_for_commands=False)
            )
            phase = "stop" if stop else "close"
            completed = False
            try:
                for _ in range(_BACKTEST_SHUTDOWN_STEPS):
                    try:
                        yielded = coroutine.send(None)
                    except StopIteration:
                        completed = True
                        break
                    if yielded is not None:
                        self.log.error(
                            f"{type(stratlet).__name__} {phase} needs an event loop "
                            f"({type(yielded).__name__})",
                        )
                        break
                else:
                    self.log.error(
                        f"{type(stratlet).__name__} {phase} exceeded "
                        f"{_BACKTEST_SHUTDOWN_STEPS} immediate steps",
                    )
            except (asyncio.CancelledError, Exception) as exc:
                self.log.error(f"{type(stratlet).__name__} {phase} error: {exc}")
            finally:
                if stop and not completed:
                    stratlet._stop_failed = True
                try:
                    coroutine.close()
                except Exception as exc:
                    self.log.error(f"{type(stratlet).__name__} task close error: {exc}")

    async def _shutdown_live(self, stratlets: tuple[Stratlet, ...]) -> None:
        results = await asyncio.gather(*(stratlet.on_stop() for stratlet in stratlets), return_exceptions=True)
        for stratlet, result in zip(stratlets, results, strict=True):
            if isinstance(result, BaseException):
                self.log.error(f"{type(stratlet).__name__} stop error: {result}")

        results = await asyncio.gather(*(stratlet._close_once() for stratlet in stratlets), return_exceptions=True)
        for stratlet, result in zip(stratlets, results, strict=True):
            if isinstance(result, BaseException):
                self.log.error(f"{type(stratlet).__name__} close error: {result}")

        tasks = [stratlet.task for stratlet in stratlets if stratlet.task is not None]
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

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
