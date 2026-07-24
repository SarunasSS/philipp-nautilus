import asyncio


from enum import Enum


from nautilus_trader.model.enums import OrderSide
from nautilus_trader.model.enums import OrderStatus
from nautilus_trader.model.instruments import Instrument
from nautilus_trader.model.objects import Quantity
from nautilus_trader.model.orders import Order
from nautilus_trader.test_kit.functions import eventually


from strategies.subscribe import Subscribe
from strategies.subscribe import SubscribeConfig


class ExecutionCase(str, Enum):
    ENTRY_EXIT = "entry-exit"


class ExecuteConfig(SubscribeConfig, frozen=True):
    case: str
    quantity: float = 1.0
    timeout: float = 10.0
    delay: float = 0.0


class Execute(Subscribe):
    def __init__(self, config: ExecuteConfig) -> None:
        super().__init__(config)

        ExecutionCase(config.case)
        if config.quantity <= 0:
            raise ValueError("quantity must be greater than zero")
        if config.timeout <= 0:
            raise ValueError("timeout must be greater than zero")
        if config.delay < 0:
            raise ValueError("delay must be zero or greater")

        self._execution_loop: asyncio.AbstractEventLoop | None = None
        self._execution_task: asyncio.Task[None] | None = None
        self._starting_position = 0.0

    def on_start(self) -> None:
        self._execution_loop = asyncio.get_running_loop()
        self._starting_position = self._position_quantity()
        super().on_start()

    def on_instrument(self, instrument: Instrument) -> None:
        super().on_instrument(instrument)
        if instrument.id != self._bar_type.instrument_id or self._execution_task is not None:
            return
        if self._execution_loop is None:
            raise RuntimeError("Execute requires an active Nautilus asyncio event loop")

        self._execution_task = self._execution_loop.create_task(self._run_case(instrument))

    async def _run_case(self, instrument: Instrument) -> None:
        case = ExecutionCase(self.config.case)
        if case != ExecutionCase.ENTRY_EXIT:
            self.log.error(f"Execution case '{case.value}' is not implemented")
            return

        try:
            await self._entry_exit(instrument)
        except asyncio.CancelledError:
            self.log.warning(f"Execution case '{case.value}' was canceled")
            raise
        except Exception as exc:
            self.log.error(f"Execution case '{case.value}' failed: {exc}")

    async def _entry_exit(self, instrument: Instrument) -> None:
        starting_position = self._position_quantity()
        quantity = instrument.make_qty(self.config.quantity)

        entry_order = await self._submit_market_order(
            instrument=instrument,
            side=OrderSide.BUY,
            quantity=quantity,
            label="entry",
        )
        entered_position = self._position_quantity()
        expected_position = starting_position + entry_order.filled_qty.as_double()
        if entered_position != expected_position:
            self.log.warning(
                f"Position after entry is {entered_position}; expected {expected_position}",
            )

        if self.config.delay > 0:
            await asyncio.sleep(self.config.delay)

        await self._submit_market_order(
            instrument=instrument,
            side=OrderSide.SELL,
            quantity=entry_order.filled_qty,
            label="exit",
        )
        exited_position = self._position_quantity()
        if exited_position != starting_position:
            self.log.warning(
                f"Position after exit is {exited_position}; expected {starting_position}",
            )

        self.log.info("Execution case 'entry-exit' completed")

    async def _submit_market_order(
        self,
        instrument: Instrument,
        side: OrderSide,
        quantity: Quantity,
        label: str,
    ) -> Order:
        order = self.order_factory.market(
            instrument_id=instrument.id,
            order_side=side,
            quantity=quantity,
            tags=["EXECUTION_TEST", label.upper()],
        )
        self.submit_order(order)
        self.log.info(
            f"Submitted market {label}: side={side} quantity={quantity} "
            f"client_order_id={order.client_order_id}",
        )

        closed_statuses = {
            OrderStatus.CANCELED,
            OrderStatus.DENIED,
            OrderStatus.EXPIRED,
            OrderStatus.FILLED,
            OrderStatus.REJECTED,
        }
        await eventually(
            lambda: (
                (cached_order := self.cache.order(order.client_order_id)) is not None
                and cached_order.status in closed_statuses
            ),
            timeout=self.config.timeout,
        )

        closed_order = self.cache.order(order.client_order_id)
        if closed_order is None or closed_order.status != OrderStatus.FILLED:
            status = "missing" if closed_order is None else closed_order.status_string()
            raise RuntimeError(f"Market {label} did not fill: status={status}")

        self.log.info(
            f"Filled market {label}: venue_order_id={closed_order.venue_order_id} "
            f"quantity={closed_order.filled_qty} average_price={closed_order.avg_px}",
        )
        return closed_order

    def _position_quantity(self) -> float:
        return sum(
            float(position.signed_qty)
            for position in self.cache.positions(instrument_id=self._bar_type.instrument_id)
        )

    def on_stop(self) -> None:
        current_position = self._position_quantity()
        delta = current_position - self._starting_position
        if delta != 0:
            self.log.error(
                f"Execution test stopped with position delta {delta}; manual intervention is required",
            )

        super().on_stop()
