from datetime import datetime
from decimal import Decimal
from typing import Any

from nautilus_trader.core.uuid import UUID4
from nautilus_trader.execution.reports import FillReport
from nautilus_trader.execution.reports import OrderStatusReport
from nautilus_trader.execution.reports import PositionStatusReport
from nautilus_trader.model.currencies import USD
from nautilus_trader.model.enums import LiquiditySide
from nautilus_trader.model.enums import OrderSide
from nautilus_trader.model.enums import OrderStatus
from nautilus_trader.model.enums import OrderType
from nautilus_trader.model.enums import PositionSide
from nautilus_trader.model.enums import TimeInForce
from nautilus_trader.model.enums import TriggerType
from nautilus_trader.model.identifiers import AccountId
from nautilus_trader.model.identifiers import ClientOrderId
from nautilus_trader.model.identifiers import TradeId
from nautilus_trader.model.identifiers import VenueOrderId
from nautilus_trader.model.instruments import Instrument
from nautilus_trader.model.objects import AccountBalance
from nautilus_trader.model.objects import MarginBalance
from nautilus_trader.model.objects import Money

from .common import parse_timestamp_ns
from .execution_commands import parse_expire_time


CLOSED_ORDER_STATUSES = {
    OrderStatus.CANCELED,
    OrderStatus.EXPIRED,
    OrderStatus.FILLED,
    OrderStatus.REJECTED,
}
_ORDER_TYPES = {
    "Limit": OrderType.LIMIT,
    "Market": OrderType.MARKET,
    "Stop": OrderType.STOP_MARKET,
    "StopLimit": OrderType.STOP_LIMIT,
}
_TIME_IN_FORCE = {
    "Day": TimeInForce.DAY,
    "FOK": TimeInForce.FOK,
    "GTC": TimeInForce.GTC,
    "GTD": TimeInForce.GTD,
    "IOC": TimeInForce.IOC,
}


def parse_order_report(
    order: dict[str, Any],
    version: dict[str, Any],
    execution: dict[str, Any],
    instrument: Instrument,
    account_id: AccountId,
    client_order_id: ClientOrderId | None,
    ts_init: int,
) -> OrderStatusReport:
    quantity = instrument.make_qty(float(version["orderQty"]))
    filled_qty = instrument.make_qty(float(execution.get("cumQty", 0) or 0))
    status = _order_status(str(order["ordStatus"]), quantity.as_double(), filled_qty.as_double())
    order_type = _ORDER_TYPES.get(str(version["orderType"]))
    time_in_force = _TIME_IN_FORCE.get(str(version.get("timeInForce", "Day")))
    if order_type is None or time_in_force is None:
        raise ValueError(f"Unsupported Tradovate order version: {version}")

    ts_accepted = parse_timestamp_ns(order.get("timestamp"), ts_init)
    ts_last = parse_timestamp_ns(execution.get("timestamp"), ts_accepted)
    trigger_price = version.get("stopPrice")
    return OrderStatusReport(
        account_id=account_id,
        instrument_id=instrument.id,
        venue_order_id=VenueOrderId(str(order["id"])),
        order_side=_order_side(order["action"]),
        order_type=order_type,
        time_in_force=time_in_force,
        order_status=status,
        quantity=quantity,
        filled_qty=filled_qty,
        report_id=UUID4(),
        ts_accepted=ts_accepted,
        ts_last=ts_last,
        ts_init=ts_init,
        client_order_id=client_order_id,
        expire_time=parse_expire_time(version.get("expireTime")),
        price=instrument.make_price(float(version["price"])) if version.get("price") is not None else None,
        trigger_price=(
            instrument.make_price(float(trigger_price)) if trigger_price is not None else None
        ),
        trigger_type=TriggerType.DEFAULT if trigger_price is not None else TriggerType.NO_TRIGGER,
        avg_px=_decimal_or_none(execution.get("avgPx")),
        cancel_reason=execution.get("text"),
    )


def parse_fill_report(
    fill: dict[str, Any],
    execution: dict[str, Any],
    instrument: Instrument,
    account_id: AccountId,
    client_order_id: ClientOrderId | None,
    ts_init: int,
) -> FillReport:
    return FillReport(
        account_id=account_id,
        instrument_id=instrument.id,
        venue_order_id=VenueOrderId(str(fill["orderId"])),
        trade_id=TradeId(str(fill["id"])),
        order_side=_order_side(fill["action"]),
        last_qty=instrument.make_qty(float(fill["qty"])),
        last_px=instrument.make_price(float(fill["price"])),
        commission=Money(0, instrument.quote_currency),
        liquidity_side=LiquiditySide.NO_LIQUIDITY_SIDE,
        report_id=UUID4(),
        ts_event=parse_timestamp_ns(fill.get("timestamp"), ts_init),
        ts_init=ts_init,
        avg_px=_decimal_or_none(execution.get("avgPx")),
        client_order_id=client_order_id,
    )


def parse_position_report(
    position: dict[str, Any],
    instrument: Instrument,
    account_id: AccountId,
    ts_init: int,
) -> PositionStatusReport:
    net_position = int(position.get("netPos", 0) or 0)
    side = PositionSide.FLAT
    if net_position > 0:
        side = PositionSide.LONG
    elif net_position < 0:
        side = PositionSide.SHORT
    return PositionStatusReport(
        account_id=account_id,
        instrument_id=instrument.id,
        position_side=side,
        quantity=instrument.make_qty(abs(net_position)),
        report_id=UUID4(),
        ts_last=parse_timestamp_ns(position.get("timestamp"), ts_init),
        ts_init=ts_init,
        avg_px_open=_decimal_or_none(position.get("netPrice")) if net_position else None,
    )


def parse_account_balances(
    snapshot: dict[str, Any],
) -> tuple[list[AccountBalance], list[MarginBalance]]:
    total = Decimal(str(snapshot.get("netLiq", snapshot.get("totalCashValue", 0)) or 0))
    initial = Decimal(str(snapshot.get("initialMargin", 0) or 0))
    maintenance = Decimal(str(snapshot.get("maintenanceMargin", 0) or 0))
    return (
        [
            AccountBalance(
                total=Money(total, USD),
                locked=Money(initial, USD),
                free=Money(total - initial, USD),
            ),
        ],
        [
            MarginBalance(
                initial=Money(initial, USD),
                maintenance=Money(maintenance, USD),
            ),
        ],
    )


def latest_by(entities: list[dict[str, Any]], key: str) -> dict[int, dict[str, Any]]:
    result: dict[int, dict[str, Any]] = {}
    for entity in entities:
        entity_key = entity.get(key)
        if entity_key is None:
            continue
        current = result.get(int(entity_key))
        if current is None or int(entity.get("id", 0)) > int(current.get("id", 0)):
            result[int(entity_key)] = entity
    return result


def client_ids_by_order(commands: list[dict[str, Any]]) -> dict[int, str]:
    result: dict[int, str] = {}
    for command in sorted(commands, key=lambda item: int(item.get("id", 0))):
        if command.get("commandType") != "New" or not command.get("clOrdId"):
            continue
        result.setdefault(int(command["orderId"]), str(command["clOrdId"]))
    return result


def in_time_range(timestamp_ns: int, start: datetime | None, end: datetime | None) -> bool:
    if start is not None and timestamp_ns < int(start.timestamp() * 1_000_000_000):
        return False
    if end is not None and timestamp_ns > int(end.timestamp() * 1_000_000_000):
        return False
    return True


def _order_status(value: str, quantity: float, filled_qty: float) -> OrderStatus:
    statuses = {
        "Canceled": OrderStatus.CANCELED,
        "Expired": OrderStatus.EXPIRED,
        "Filled": OrderStatus.FILLED,
        "PendingCancel": OrderStatus.PENDING_CANCEL,
        "PendingNew": OrderStatus.SUBMITTED,
        "PendingReplace": OrderStatus.PENDING_UPDATE,
        "Rejected": OrderStatus.REJECTED,
        "Suspended": OrderStatus.ACCEPTED,
        "Unknown": OrderStatus.ACCEPTED,
        "Working": OrderStatus.PARTIALLY_FILLED if filled_qty else OrderStatus.ACCEPTED,
    }
    if value == "Completed":
        return OrderStatus.FILLED if filled_qty >= quantity else OrderStatus.CANCELED
    try:
        return statuses[value]
    except KeyError as exc:
        raise ValueError(f"Unsupported Tradovate order status: {value}") from exc


def _order_side(value: str) -> OrderSide:
    if value == "Buy":
        return OrderSide.BUY
    if value == "Sell":
        return OrderSide.SELL
    raise ValueError(f"Unsupported Tradovate order action: {value}")


def _decimal_or_none(value: Any) -> Decimal | None:
    return Decimal(str(value)) if value is not None else None
