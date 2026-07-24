import asyncio

from typing import Any

from nautilus_trader.cache.cache import Cache
from nautilus_trader.common.component import LiveClock
from nautilus_trader.common.component import MessageBus
from nautilus_trader.core.uuid import UUID4
from nautilus_trader.execution.messages import GenerateFillReports
from nautilus_trader.execution.messages import GenerateOrderStatusReport
from nautilus_trader.execution.messages import GenerateOrderStatusReports
from nautilus_trader.execution.messages import GeneratePositionStatusReports
from nautilus_trader.execution.reports import ExecutionMassStatus
from nautilus_trader.execution.reports import FillReport
from nautilus_trader.execution.reports import OrderStatusReport
from nautilus_trader.execution.reports import PositionStatusReport
from nautilus_trader.model.currencies import USD
from nautilus_trader.model.enums import AccountType
from nautilus_trader.model.enums import OmsType
from nautilus_trader.model.identifiers import AccountId
from nautilus_trader.model.identifiers import ClientId

from .config import TradovateExecClientConfig
from .core import TRADOVATE_CLIENT_ID
from .core import TRADOVATE_VENUE
from .execution_commands import TradovateOrderCommandClient
from .execution_parsing import parse_account_balances
from .execution_reports import TradovateReportProvider
from .http.client import TradovateHttpClient
from .providers import TradovateInstrumentProvider
from .websocket.client import TradovateWebSocketClient


_USER_SYNC_ENTITY_TYPES = [
    "account",
    "cashBalance",
    "command",
    "commandReport",
    "executionReport",
    "fill",
    "order",
    "position",
]


class TradovateExecutionClient(TradovateOrderCommandClient):
    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        http_client: TradovateHttpClient,
        msgbus: MessageBus,
        cache: Cache,
        clock: LiveClock,
        instrument_provider: TradovateInstrumentProvider,
        config: TradovateExecClientConfig,
        name: str | None,
    ) -> None:
        super().__init__(
            loop=loop,
            client_id=ClientId(name) if name else TRADOVATE_CLIENT_ID,
            venue=TRADOVATE_VENUE,
            oms_type=OmsType.NETTING,
            account_type=AccountType.MARGIN,
            base_currency=USD,
            instrument_provider=instrument_provider,
            msgbus=msgbus,
            cache=cache,
            clock=clock,
            config=config,
        )
        self._http_client = http_client
        self._tradovate_instrument_provider = instrument_provider
        self._config = config
        self._ws_client = TradovateWebSocketClient(
            loop=loop,
            base_url=config.ws_base_url,
            handler=self._handle_ws_message,
            reconnect_handler=self._restore_user_sync,
            request_timeout_secs=config.request_timeout_secs,
        )
        self._tradovate_account_id: int | None = None
        self._report_provider: TradovateReportProvider | None = None
        self._refresh_task: asyncio.Task[None] | None = None
        self._account_refresh_requested = False
        self._execution_refresh_requested = False
        self._seen_fill_ids: set[str] = set()

    async def _connect(self) -> None:
        access_token = await self._http_client.get_access_token()
        accounts = await self._http_client.get("/account/list")
        if not isinstance(accounts, list):
            raise TypeError("Expected a list of objects from /account/list")
        open_accounts = [account for account in accounts if not account.get("closed", False)]
        if self._config.account_id is not None:
            selected = next(
                (account for account in open_accounts if int(account["id"]) == self._config.account_id),
                None,
            )
            if selected is None:
                raise ValueError(f"Tradovate account {self._config.account_id} was not found or is closed")
        elif len(open_accounts) == 1:
            selected = open_accounts[0]
        elif not open_accounts:
            raise ValueError("No open Tradovate accounts were returned")
        else:
            account_ids = ", ".join(str(account["id"]) for account in open_accounts)
            raise ValueError(
                f"Multiple Tradovate accounts are available ({account_ids}); set TRADOVATE_ACCOUNT_ID",
            )

        self._tradovate_account_id = int(selected["id"])
        self._set_account_id(AccountId(f"TRADOVATE-{self._tradovate_account_id}"))
        self._report_provider = TradovateReportProvider(
            client=self._http_client,
            instrument_provider=self._tradovate_instrument_provider,
            cache=self._cache,
            clock=self._clock,
            account_id=self.account_id,
            tradovate_account_id=self._tradovate_account_id,
        )
        await self._update_account_state()
        await self._ws_client.connect(access_token)
        await self._subscribe_user_sync()
        self._log.info(f"Connected Tradovate execution account {self.account_id}")

    async def _disconnect(self) -> None:
        if self._refresh_task is not None:
            self._refresh_task.cancel()
            try:
                await self._refresh_task
            except asyncio.CancelledError:
                pass
            self._refresh_task = None
        await self._ws_client.disconnect()

    async def generate_order_status_report(
        self,
        command: GenerateOrderStatusReport,
    ) -> OrderStatusReport | None:
        reports = await self._require_reports().order_reports(
            instrument_id=command.instrument_id,
            client_order_id=command.client_order_id,
            venue_order_id=command.venue_order_id,
        )
        return reports[0] if reports else None

    async def generate_order_status_reports(
        self,
        command: GenerateOrderStatusReports,
    ) -> list[OrderStatusReport]:
        reports = await self._require_reports().order_reports(
            instrument_id=command.instrument_id,
            start=command.start,
            end=command.end,
            open_only=command.open_only,
        )
        self._log.info(f"Retrieved {len(reports)} Tradovate order reports")
        return reports

    async def generate_fill_reports(self, command: GenerateFillReports) -> list[FillReport]:
        reports = await self._require_reports().fill_reports(
            instrument_id=command.instrument_id,
            venue_order_id=command.venue_order_id,
            start=command.start,
            end=command.end,
        )
        self._seen_fill_ids.update(report.trade_id.value for report in reports)
        return reports

    async def generate_position_status_reports(
        self,
        command: GeneratePositionStatusReports,
    ) -> list[PositionStatusReport]:
        reports = await self._require_reports().position_reports(command.instrument_id)
        self._log.info(f"Retrieved {len(reports)} Tradovate position reports")
        return reports

    async def generate_mass_status(self, lookback_mins: int | None = None) -> ExecutionMassStatus:
        reports = self._require_reports()
        order_reports, fill_reports, position_reports = await asyncio.gather(
            reports.order_reports(),
            reports.fill_reports(),
            reports.position_reports(),
        )
        self._seen_fill_ids.update(report.trade_id.value for report in fill_reports)
        mass_status = ExecutionMassStatus(
            client_id=self.id,
            account_id=self.account_id,
            venue=TRADOVATE_VENUE,
            report_id=UUID4(),
            ts_init=self._clock.timestamp_ns(),
        )
        mass_status.add_order_reports(order_reports)
        mass_status.add_fill_reports(fill_reports)
        mass_status.add_position_reports(position_reports)
        self._log.info(
            "Retrieved Tradovate execution state: "
            f"orders={len(order_reports)}, fills={len(fill_reports)}, positions={len(position_reports)}",
        )
        return mass_status

    async def _update_account_state(self) -> None:
        if self._tradovate_account_id is None:
            raise RuntimeError("Tradovate account has not been selected")
        snapshot = await self._http_client.post(
            "/cashBalance/getcashbalancesnapshot",
            {"accountId": self._tradovate_account_id},
        )
        if not isinstance(snapshot, dict):
            raise TypeError("Expected an object from /cashBalance/getcashbalancesnapshot")
        balances, margins = parse_account_balances(snapshot)
        self.generate_account_state(
            balances=balances,
            margins=margins,
            reported=True,
            ts_event=self._clock.timestamp_ns(),
            info=snapshot,
        )
        self._log.info(f"Retrieved Tradovate account state for {self.account_id}")

    async def _subscribe_user_sync(self) -> None:
        await self._ws_client.request(
            "user/syncrequest",
            {"splitResponses": True, "entityTypes": _USER_SYNC_ENTITY_TYPES},
        )

    async def _restore_user_sync(self) -> None:
        await self._subscribe_user_sync()
        self._schedule_refresh(account=True, execution=True)

    def _handle_ws_message(self, message: dict[str, Any]) -> None:
        if message.get("e") == "shutdown":
            self._log.error(f"Tradovate requested WebSocket shutdown: {message.get('d')}")
            return
        if message.get("e") == "props":
            updates = message.get("d")
            if not isinstance(updates, list):
                self._schedule_refresh(account=True, execution=True)
                return
            entity_types = {
                str(update.get("entityType", "")).lower()
                for update in updates
                if isinstance(update, dict)
            }
            self._schedule_refresh(
                account=bool(entity_types & {"account", "cashbalance"}),
                execution=bool(
                    entity_types
                    & {"command", "commandreport", "executionreport", "fill", "order", "position"}
                ),
            )

    def _schedule_refresh(self, account: bool, execution: bool) -> None:
        if not account and not execution:
            return
        self._account_refresh_requested |= account
        self._execution_refresh_requested |= execution
        if self._refresh_task is None or self._refresh_task.done():
            self._refresh_task = self._loop.create_task(self._refresh_execution_state())

    async def _refresh_execution_state(self) -> None:
        try:
            while self._account_refresh_requested or self._execution_refresh_requested:
                await asyncio.sleep(0.05)
                refresh_account = self._account_refresh_requested
                refresh_execution = self._execution_refresh_requested
                self._account_refresh_requested = False
                self._execution_refresh_requested = False

                if refresh_account:
                    await self._update_account_state()
                if refresh_execution:
                    report_provider = self._require_reports()
                    order_reports, fill_reports, position_reports = await asyncio.gather(
                        report_provider.order_reports(),
                        report_provider.fill_reports(),
                        report_provider.position_reports(),
                    )
                    for report in order_reports:
                        self._send_order_status_report(report)
                    for report in fill_reports:
                        if report.trade_id.value not in self._seen_fill_ids:
                            self._seen_fill_ids.add(report.trade_id.value)
                            self._send_fill_report(report)
                    for report in position_reports:
                        self._send_position_status_report(report)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._log.error(f"Tradovate user-state refresh failed: {exc}")

    def _require_reports(self) -> TradovateReportProvider:
        if self._report_provider is None:
            raise RuntimeError("Tradovate execution client is not connected")
        return self._report_provider
