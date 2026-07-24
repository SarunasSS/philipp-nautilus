import asyncio

from datetime import datetime
from typing import Any

from nautilus_trader.cache.cache import Cache
from nautilus_trader.common.component import LiveClock
from nautilus_trader.core.uuid import UUID4
from nautilus_trader.execution.reports import FillReport
from nautilus_trader.execution.reports import OrderStatusReport
from nautilus_trader.execution.reports import PositionStatusReport
from nautilus_trader.model.enums import PositionSide
from nautilus_trader.model.identifiers import AccountId
from nautilus_trader.model.identifiers import ClientOrderId
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.identifiers import VenueOrderId
from nautilus_trader.model.instruments import Instrument

from .common import parse_timestamp_ns
from .execution_parsing import CLOSED_ORDER_STATUSES
from .execution_parsing import client_ids_by_order
from .execution_parsing import in_time_range
from .execution_parsing import latest_by
from .execution_parsing import parse_fill_report
from .execution_parsing import parse_order_report
from .execution_parsing import parse_position_report
from .http.client import TradovateHttpClient
from .providers import TradovateInstrumentProvider


class TradovateReportProvider:
    def __init__(
        self,
        client: TradovateHttpClient,
        instrument_provider: TradovateInstrumentProvider,
        cache: Cache,
        clock: LiveClock,
        account_id: AccountId,
        tradovate_account_id: int,
    ) -> None:
        self._client = client
        self._instrument_provider = instrument_provider
        self._cache = cache
        self._clock = clock
        self._account_id = account_id
        self._tradovate_account_id = tradovate_account_id

    async def order_reports(
        self,
        instrument_id: InstrumentId | None = None,
        client_order_id: ClientOrderId | None = None,
        venue_order_id: VenueOrderId | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        open_only: bool = False,
    ) -> list[OrderStatusReport]:
        orders, versions, executions, commands = await asyncio.gather(
            self._entities("/order/list"),
            self._entities("/orderVersion/list"),
            self._entities("/executionReport/list"),
            self._entities("/command/list"),
        )
        latest_versions = latest_by(versions, "orderId")
        latest_executions = latest_by(executions, "orderId")
        client_ids = client_ids_by_order(commands)
        reports: list[OrderStatusReport] = []

        for raw_order in orders:
            if int(raw_order.get("accountId", -1)) != self._tradovate_account_id:
                continue
            raw_order_id = int(raw_order["id"])
            if venue_order_id is not None and venue_order_id.value != str(raw_order_id):
                continue

            version = latest_versions.get(raw_order_id)
            if version is None:
                continue
            instrument = await self._instrument(int(raw_order["contractId"]))
            if instrument_id is not None and instrument.id != instrument_id:
                continue

            resolved_client_order_id = self._cache.client_order_id(VenueOrderId(str(raw_order_id)))
            if resolved_client_order_id is None and client_ids.get(raw_order_id):
                resolved_client_order_id = ClientOrderId(client_ids[raw_order_id])
            if client_order_id is not None and resolved_client_order_id != client_order_id:
                continue

            execution = latest_executions.get(raw_order_id, {})
            report = parse_order_report(
                raw_order,
                version,
                execution,
                instrument,
                self._account_id,
                resolved_client_order_id,
                self._clock.timestamp_ns(),
            )
            if not in_time_range(report.ts_last, start, end):
                continue
            if open_only and report.order_status in CLOSED_ORDER_STATUSES:
                continue
            reports.append(report)
        return reports

    async def fill_reports(
        self,
        instrument_id: InstrumentId | None = None,
        venue_order_id: VenueOrderId | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> list[FillReport]:
        fills, orders, commands, executions = await asyncio.gather(
            self._entities("/fill/list"),
            self._entities("/order/list"),
            self._entities("/command/list"),
            self._entities("/executionReport/list"),
        )
        account_orders = {
            int(order["id"]): order
            for order in orders
            if int(order.get("accountId", -1)) == self._tradovate_account_id
        }
        client_ids = client_ids_by_order(commands)
        latest_executions = latest_by(executions, "orderId")
        reports: list[FillReport] = []

        for fill in fills:
            raw_order_id = int(fill["orderId"])
            order = account_orders.get(raw_order_id)
            if order is None:
                continue
            if venue_order_id is not None and venue_order_id.value != str(raw_order_id):
                continue
            ts_event = parse_timestamp_ns(fill.get("timestamp"), self._clock.timestamp_ns())
            if not in_time_range(ts_event, start, end):
                continue

            instrument = await self._instrument(int(fill["contractId"]))
            if instrument_id is not None and instrument.id != instrument_id:
                continue
            resolved_client_order_id = self._cache.client_order_id(VenueOrderId(str(raw_order_id)))
            if resolved_client_order_id is None and client_ids.get(raw_order_id):
                resolved_client_order_id = ClientOrderId(client_ids[raw_order_id])
            execution = latest_executions.get(raw_order_id, {})
            reports.append(
                parse_fill_report(
                    fill,
                    execution,
                    instrument,
                    self._account_id,
                    self._tradovate_account_id,
                    resolved_client_order_id,
                    self._clock.timestamp_ns(),
                ),
            )
        return reports

    async def position_reports(
        self,
        instrument_id: InstrumentId | None = None,
    ) -> list[PositionStatusReport]:
        positions = await self._entities("/position/list")
        reports: list[PositionStatusReport] = []
        for position in positions:
            if int(position.get("accountId", -1)) != self._tradovate_account_id:
                continue
            contract_id = int(position["contractId"])
            instrument = await self._instrument(contract_id)
            if instrument_id is not None and instrument.id != instrument_id:
                continue
            reports.append(
                parse_position_report(
                    position,
                    instrument,
                    self._account_id,
                    self._clock.timestamp_ns(),
                ),
            )

        if instrument_id is not None and not reports:
            instrument = self._cache.instrument(instrument_id)
            if instrument is None:
                await self._instrument_provider.load_async(instrument_id)
                instrument = self._instrument_provider.find(instrument_id)
            if instrument is not None:
                reports.append(
                    PositionStatusReport(
                        account_id=self._account_id,
                        instrument_id=instrument.id,
                        position_side=PositionSide.FLAT,
                        quantity=instrument.make_qty(0),
                        report_id=UUID4(),
                        ts_last=self._clock.timestamp_ns(),
                        ts_init=self._clock.timestamp_ns(),
                    ),
                )
        return reports

    async def _entities(self, path: str) -> list[dict[str, Any]]:
        result = await self._client.get(path)
        if not isinstance(result, list) or not all(isinstance(item, dict) for item in result):
            raise TypeError(f"Expected a list of objects from {path}")
        return result

    async def _instrument(self, contract_id: int) -> Instrument:
        for instrument in self._instrument_provider.list_all():
            if int(instrument.info.get("contract_id", -1)) == contract_id:
                return instrument

        instrument_id = await self._instrument_provider.load_contract_async(contract_id)
        instrument = self._instrument_provider.find(instrument_id)
        if instrument is None:
            raise RuntimeError(f"Tradovate contract {contract_id} did not produce an instrument")
        if self._cache.instrument(instrument.id) is None:
            self._cache.add_instrument(instrument)
        return instrument
