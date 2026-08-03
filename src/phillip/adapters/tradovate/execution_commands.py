import asyncio

from datetime import UTC
from datetime import datetime
from typing import Any

from nautilus_trader.execution.messages import BatchCancelOrders
from nautilus_trader.execution.messages import CancelAllOrders
from nautilus_trader.execution.messages import CancelOrder
from nautilus_trader.execution.messages import ModifyOrder
from nautilus_trader.execution.messages import QueryAccount
from nautilus_trader.execution.messages import SubmitOrder
from nautilus_trader.execution.messages import SubmitOrderList
from nautilus_trader.live.execution_client import LiveExecutionClient
from nautilus_trader.model.enums import OrderSide
from nautilus_trader.model.enums import OrderStatus
from nautilus_trader.model.enums import OrderType
from nautilus_trader.model.enums import TimeInForce
from nautilus_trader.model.identifiers import VenueOrderId
from nautilus_trader.model.orders import Order

from .core import TRADOVATE_VENUE
from .errors import TradovateApiError
from .http.client import TradovateHttpClient


_ORDER_TYPES = {
    OrderType.MARKET: "Market",
    OrderType.LIMIT: "Limit",
    OrderType.STOP_MARKET: "Stop",
    OrderType.STOP_LIMIT: "StopLimit",
}
_TIME_IN_FORCE = {
    TimeInForce.DAY: "Day",
    TimeInForce.FOK: "FOK",
    TimeInForce.GTC: "GTC",
    TimeInForce.GTD: "GTD",
    TimeInForce.IOC: "IOC",
}


class TradovateOrderCommandClient(LiveExecutionClient):
    _http_client: TradovateHttpClient
    _tradovate_account_id: int | None

    async def _update_account_state(self) -> None:
        raise NotImplementedError

    async def _submit_order(self, command: SubmitOrder) -> None:
        await self._submit_order_entity(command.order)

    async def _submit_order_list(self, command: SubmitOrderList) -> None:
        await asyncio.gather(
            *(self._submit_order_entity(order) for order in command.order_list.orders),
        )

    async def _modify_order(self, command: ModifyOrder) -> None:
        order = self._cache.order(command.client_order_id)
        if order is None:
            self._log.error(f"Cannot modify unknown order {command.client_order_id}")
            return
        venue_order_id = command.venue_order_id or order.venue_order_id
        if venue_order_id is None:
            self._log.error(f"Cannot modify {command.client_order_id} without a venue order ID")
            return
        try:
            payload = build_modify_order_payload(
                order,
                int(venue_order_id.value),
                command.quantity,
                command.price,
                command.trigger_price,
            )
            result = await self._http_client.post("/order/modifyorder", payload)
            require_command_success(result)
        except Exception as exc:
            self.generate_order_modify_rejected(
                strategy_id=command.strategy_id,
                instrument_id=command.instrument_id,
                client_order_id=command.client_order_id,
                venue_order_id=venue_order_id,
                reason=str(exc),
                ts_event=self._clock.timestamp_ns(),
            )
            return
        self.generate_order_updated(
            strategy_id=command.strategy_id,
            instrument_id=command.instrument_id,
            client_order_id=command.client_order_id,
            venue_order_id=venue_order_id,
            quantity=command.quantity or order.quantity,
            price=command.price if command.price is not None else order.price,
            trigger_price=(
                command.trigger_price if command.trigger_price is not None else order.trigger_price
            ),
            ts_event=self._clock.timestamp_ns(),
        )

    async def _cancel_order(self, command: CancelOrder) -> None:
        order = self._cache.order(command.client_order_id)
        venue_order_id = command.venue_order_id or (order.venue_order_id if order else None)
        if venue_order_id is None:
            self._log.error(f"Cannot cancel {command.client_order_id} without a venue order ID")
            return
        await self._cancel_order_entity(
            command.strategy_id,
            command.instrument_id,
            command.client_order_id,
            venue_order_id,
        )

    async def _cancel_all_orders(self, command: CancelAllOrders) -> None:
        orders = self._cache.orders_open(
            venue=TRADOVATE_VENUE,
            instrument_id=command.instrument_id,
            strategy_id=command.strategy_id,
            side=command.order_side,
        )
        for order in orders:
            if order.venue_order_id is not None:
                await self._cancel_order_entity(
                    order.strategy_id,
                    order.instrument_id,
                    order.client_order_id,
                    order.venue_order_id,
                )

    async def _batch_cancel_orders(self, command: BatchCancelOrders) -> None:
        for cancel in command.cancels:
            await self._cancel_order(cancel)

    async def _query_account(self, command: QueryAccount) -> None:
        await self._update_account_state()

    async def _submit_order_entity(self, order: Order) -> None:
        if order.status != OrderStatus.INITIALIZED:
            self.generate_order_denied(
                strategy_id=order.strategy_id,
                instrument_id=order.instrument_id,
                client_order_id=order.client_order_id,
                reason=f"Order status is {order.status_string()}, expected INITIALIZED",
                ts_event=self._clock.timestamp_ns(),
            )
            return
        if self._tradovate_account_id is None:
            raise RuntimeError("Tradovate execution client is not connected")
        try:
            payload = build_place_order_payload(order, self._tradovate_account_id)
        except ValueError as exc:
            self.generate_order_denied(
                strategy_id=order.strategy_id,
                instrument_id=order.instrument_id,
                client_order_id=order.client_order_id,
                reason=str(exc),
                ts_event=self._clock.timestamp_ns(),
            )
            return

        self.generate_order_submitted(
            strategy_id=order.strategy_id,
            instrument_id=order.instrument_id,
            client_order_id=order.client_order_id,
            ts_event=self._clock.timestamp_ns(),
        )
        try:
            result = await self._http_client.post("/order/placeorder", payload)
            require_command_success(result)
            venue_order_id = VenueOrderId(str(result["orderId"]))
        except Exception as exc:
            self.generate_order_rejected(
                strategy_id=order.strategy_id,
                instrument_id=order.instrument_id,
                client_order_id=order.client_order_id,
                reason=str(exc),
                ts_event=self._clock.timestamp_ns(),
            )
            return
        self.generate_order_accepted(
            strategy_id=order.strategy_id,
            instrument_id=order.instrument_id,
            client_order_id=order.client_order_id,
            venue_order_id=venue_order_id,
            ts_event=self._clock.timestamp_ns(),
        )

    async def _cancel_order_entity(
        self,
        strategy_id: Any,
        instrument_id: Any,
        client_order_id: Any,
        venue_order_id: VenueOrderId,
    ) -> None:
        try:
            result = await self._http_client.post(
                "/order/cancelorder",
                {"orderId": int(venue_order_id.value)},
            )
            require_command_success(result)
        except Exception as exc:
            self.generate_order_cancel_rejected(
                strategy_id=strategy_id,
                instrument_id=instrument_id,
                client_order_id=client_order_id,
                venue_order_id=venue_order_id,
                reason=str(exc),
                ts_event=self._clock.timestamp_ns(),
            )
            return
        self.generate_order_canceled(
            strategy_id=strategy_id,
            instrument_id=instrument_id,
            client_order_id=client_order_id,
            venue_order_id=venue_order_id,
            ts_event=self._clock.timestamp_ns(),
        )


def build_place_order_payload(order: Order, account_id: int) -> dict[str, Any]:
    try:
        order_type = _ORDER_TYPES[order.order_type]
        time_in_force = _TIME_IN_FORCE[order.time_in_force]
    except KeyError as exc:
        raise ValueError(f"Tradovate does not support {exc.args[0]}") from exc

    raw_quantity = order.quantity.as_double()
    quantity = int(raw_quantity)
    if raw_quantity != quantity or quantity <= 0:
        raise ValueError(f"Tradovate futures order quantity must be a positive integer, got {raw_quantity}")

    client_order_id = order.client_order_id.value
    if len(client_order_id) > 64:
        raise ValueError("Tradovate client order IDs cannot exceed 64 characters")

    payload: dict[str, Any] = {
        "accountId": account_id,
        "action": "Buy" if order.side == OrderSide.BUY else "Sell",
        "symbol": order.instrument_id.symbol.value,
        "orderQty": quantity,
        "orderType": order_type,
        "timeInForce": time_in_force,
        "clOrdId": client_order_id,
        "isAutomated": True,
    }
    if order.order_type in {OrderType.LIMIT, OrderType.STOP_LIMIT}:
        payload["price"] = order.price.as_double()
    if order.order_type in {OrderType.STOP_MARKET, OrderType.STOP_LIMIT}:
        payload["stopPrice"] = order.trigger_price.as_double()
    if order.time_in_force == TimeInForce.GTD:
        if order.expire_time is None:
            raise ValueError("Tradovate GTD orders require an expiration time")
        payload["expireTime"] = order.expire_time.astimezone(UTC).isoformat().replace("+00:00", "Z")
    return payload


def build_modify_order_payload(
    order: Order,
    venue_order_id: int,
    quantity: Any,
    price: Any,
    trigger_price: Any,
) -> dict[str, Any]:
    payload = build_place_order_payload(order, account_id=0)
    payload.pop("accountId")
    payload.pop("action")
    payload.pop("symbol")
    payload.pop("isAutomated")
    payload["orderId"] = venue_order_id

    if quantity is not None:
        raw_quantity = quantity.as_double()
        if raw_quantity != int(raw_quantity) or raw_quantity <= 0:
            raise ValueError(
                f"Tradovate futures order quantity must be a positive integer, got {raw_quantity}",
            )
        payload["orderQty"] = int(raw_quantity)
    if price is not None:
        payload["price"] = price.as_double()
    if trigger_price is not None:
        payload["stopPrice"] = trigger_price.as_double()
    return payload


def parse_expire_time(value: str | None) -> datetime | None:
    if not value:
        return None
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    return datetime.fromisoformat(normalized)


def require_command_success(result: Any) -> None:
    if not isinstance(result, dict):
        raise TradovateApiError("Tradovate command returned a non-object response", details=result)
    failure_reason = result.get("failureReason")
    if failure_reason not in (None, "", "Success"):
        raise TradovateApiError(str(result.get("failureText") or failure_reason), details=result)
