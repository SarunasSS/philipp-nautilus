import json


from datetime import datetime
from decimal import Decimal
from typing import Any


from nautilus_trader.model.data import Bar
from nautilus_trader.model.data import BarType
from nautilus_trader.model.enums import AssetClass
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.identifiers import Symbol
from nautilus_trader.model.instruments import FuturesContract
from nautilus_trader.model.instruments import Instrument
from nautilus_trader.model.objects import Currency
from nautilus_trader.model.objects import Price
from nautilus_trader.model.objects import Quantity


from .core import TRADOVATE_VENUE


def parse_timestamp_ns(value: str | None, fallback: int) -> int:
    if not value:
        return fallback

    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    return int(datetime.fromisoformat(normalized).timestamp() * 1_000_000_000)


def decode_sockjs_frame(raw: bytes | str) -> list[dict[str, Any]]:
    text = raw.decode("utf-8") if isinstance(raw, bytes) else raw
    if text in {"o", "h", ""}:
        return []

    payload: Any = json.loads(text[1:] if text.startswith("a[") else text)
    if not isinstance(payload, list):
        payload = [payload]

    messages: list[dict[str, Any]] = []
    for item in payload:
        if isinstance(item, str):
            item = json.loads(item)
        if isinstance(item, dict):
            messages.append(item)
    return messages


def parse_instrument(
    raw_symbol: str,
    contract: dict[str, Any],
    maturity: dict[str, Any],
    product: dict[str, Any],
    currency: dict[str, Any],
    ts_init: int,
) -> FuturesContract:
    tick_size = Decimal(str(product["tickSize"]))
    price_precision = max(0, -int(tick_size.as_tuple().exponent))
    currency_code = str(currency.get("name") or currency.get("code") or "USD").upper()
    expiration_ns = parse_timestamp_ns(maturity.get("expirationDate"), ts_init)
    product_symbol = str(product.get("name") or raw_symbol)
    root = product_symbol.upper()
    description = f"{product_symbol} {product.get('description', '')}".lower()
    index_roots = {"ES", "MES", "NQ", "MNQ", "YM", "MYM", "RTY", "M2K", "NKD"}
    fx_roots = {"6A", "6B", "6C", "6E", "6J", "6S", "M6A", "M6B", "M6E"}
    debt_roots = {"ZT", "ZF", "ZN", "TN", "ZB", "UB", "SR1", "SR3"}
    if root in index_roots or "index" in description:
        asset_class = AssetClass.INDEX
    elif root in fx_roots or "currency" in description or "foreign exchange" in description:
        asset_class = AssetClass.FX
    elif root in debt_roots or any(word in description for word in ("treasury", "bond", "interest rate")):
        asset_class = AssetClass.DEBT
    else:
        asset_class = AssetClass.COMMODITY

    return FuturesContract(
        instrument_id=InstrumentId(Symbol(raw_symbol), TRADOVATE_VENUE),
        raw_symbol=Symbol(raw_symbol),
        asset_class=asset_class,
        currency=Currency.from_str(currency_code),
        price_precision=price_precision,
        price_increment=Price.from_str(str(tick_size)),
        multiplier=Quantity.from_str(str(product.get("valuePerPoint", 1))),
        lot_size=Quantity.from_int(1),
        underlying=product_symbol,
        activation_ns=0,
        expiration_ns=expiration_ns,
        ts_event=ts_init,
        ts_init=ts_init,
        info={
            "tradovate_contract": contract,
            "tradovate_maturity": maturity,
            "tradovate_product": product,
            "tradovate_currency": currency,
            "contract_id": int(contract["id"]),
        },
    )


def parse_bar(
    raw: dict[str, Any],
    bar_type: BarType,
    instrument: Instrument,
    interval_ns: int,
    ts_init: int,
) -> Bar:
    ts_open = parse_timestamp_ns(raw.get("timestamp"), ts_init)
    volume = float(raw.get("upVolume", 0) or 0) + float(raw.get("downVolume", 0) or 0)
    return Bar(
        bar_type=bar_type,
        open=instrument.make_price(float(raw["open"])),
        high=instrument.make_price(float(raw["high"])),
        low=instrument.make_price(float(raw["low"])),
        close=instrument.make_price(float(raw["close"])),
        volume=instrument.make_qty(volume),
        ts_event=ts_open + interval_ns,
        ts_init=ts_init,
    )
