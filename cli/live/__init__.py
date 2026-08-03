import typer


from dataclasses import dataclass
from enum import Enum
from typing import Annotated
from typing import Any


from nautilus_trader.adapters.databento import DATABENTO
from nautilus_trader.adapters.databento import DatabentoDataClientConfig
from nautilus_trader.adapters.databento import DatabentoLiveDataClientFactory
from nautilus_trader.common.config import LoggingConfig
from nautilus_trader.config import LiveDataClientConfig
from nautilus_trader.config import LiveExecClientConfig
from nautilus_trader.config import LiveExecEngineConfig
from nautilus_trader.config import TradingNodeConfig
from nautilus_trader.live.factories import LiveDataClientFactory
from nautilus_trader.live.factories import LiveExecClientFactory
from nautilus_trader.live.node import TradingNode
from nautilus_trader.model.identifiers import TraderId
from nautilus_trader.trading.strategy import Strategy


from phillip.adapters.tradovate.config import TradovateDataClientConfig
from phillip.adapters.tradovate.config import TradovateExecClientConfig
from phillip.adapters.tradovate.core import PRODUCTION_DEMO_HTTP_URL
from phillip.adapters.tradovate.core import PRODUCTION_DEMO_MARKET_DATA_WS_URL
from phillip.adapters.tradovate.core import PRODUCTION_DEMO_WS_URL
from phillip.adapters.tradovate.core import PRODUCTION_LIVE_HTTP_URL
from phillip.adapters.tradovate.core import PRODUCTION_LIVE_MARKET_DATA_WS_URL
from phillip.adapters.tradovate.core import PRODUCTION_LIVE_WS_URL
from phillip.adapters.tradovate.core import TRADOVATE
from phillip.adapters.tradovate.factories import TradovateLiveDataClientFactory
from phillip.adapters.tradovate.factories import TradovateLiveExecClientFactory


cli = typer.Typer(help="Run Nautilus Trader live strategies")


class TradovateEnvironment(str, Enum):
    DEMO = "demo"
    LIVE = "live"


@dataclass(frozen=True)
class LiveRunSettings:
    environment: TradovateEnvironment
    username: str | None
    password: str | None
    app_id: str | None
    app_version: str
    cid: int | None
    sec: str | None
    device_id: str | None
    access_token: str | None
    md_access_token: str | None
    account_id: int | None
    databento_api_key: str | None
    trader_id: str
    log_level: str


@cli.callback()
def _configure_live(
    ctx: typer.Context,
    environment: Annotated[
        TradovateEnvironment,
        typer.Option(help="Tradovate account environment"),
    ] = TradovateEnvironment.DEMO,
    username: Annotated[
        str | None,
        typer.Option(envvar="TRADOVATE_USERNAME", help="Tradovate username"),
    ] = None,
    password: Annotated[
        str | None,
        typer.Option(envvar="TRADOVATE_PASSWORD", hide_input=True, help="Tradovate password"),
    ] = None,
    app_id: Annotated[
        str | None,
        typer.Option(envvar="TRADOVATE_APP_ID", help="API key name; use with --cid and --sec"),
    ] = None,
    app_version: Annotated[
        str,
        typer.Option(envvar="TRADOVATE_APP_VERSION", help="API application version"),
    ] = "1.0",
    cid: Annotated[
        int | None,
        typer.Option(envvar="TRADOVATE_CID", help="API key ID; use with --app-id and --sec"),
    ] = None,
    sec: Annotated[
        str | None,
        typer.Option(
            envvar="TRADOVATE_SEC",
            hide_input=True,
            help="API key secret; use with --app-id and --cid",
        ),
    ] = None,
    device_id: Annotated[
        str | None,
        typer.Option(envvar="TRADOVATE_DEVICE_ID", help="Optional stable device ID"),
    ] = None,
    access_token: Annotated[str | None, typer.Option(envvar="TRADOVATE_ACCESS_TOKEN", hidden=True)] = None,
    md_access_token: Annotated[str | None, typer.Option(envvar="TRADOVATE_MD_ACCESS_TOKEN", hidden=True)] = None,
    account_id: Annotated[
        int | None,
        typer.Option(
            envvar="TRADOVATE_ACCOUNT_ID",
            help="Tradovate account ID; required when credentials expose multiple open accounts",
        ),
    ] = None,
    databento_api_key: Annotated[str | None, typer.Option(envvar="DATABENTO_API_KEY", hidden=True)] = None,
    trader_id: Annotated[str, typer.Option(envvar="TRADER_ID")] = "PHILIPP-LIVE-001",
    log_level: Annotated[str, typer.Option(envvar="LOG_LEVEL")] = "INFO",
) -> None:
    if bool(username) != bool(password):
        raise typer.BadParameter("TRADOVATE_USERNAME and TRADOVATE_PASSWORD must be supplied together")

    api_key_fields_present = bool(app_id) or cid is not None or bool(sec)
    api_key_complete = bool(app_id) and cid is not None and bool(sec)
    if api_key_fields_present and not api_key_complete:
        raise typer.BadParameter("TRADOVATE_APP_ID, TRADOVATE_CID, and TRADOVATE_SEC must be supplied together")
    if api_key_complete and not username:
        raise typer.BadParameter("Tradovate API-key authentication also requires username and password")

    if bool(access_token) != bool(md_access_token):
        raise typer.BadParameter(
            "TRADOVATE_ACCESS_TOKEN and TRADOVATE_MD_ACCESS_TOKEN must be supplied together",
        )

    ctx.obj = LiveRunSettings(
        environment=environment,
        username=username,
        password=password,
        app_id=app_id,
        app_version=app_version,
        cid=cid,
        sec=sec,
        device_id=device_id,
        access_token=access_token,
        md_access_token=md_access_token,
        account_id=account_id,
        databento_api_key=databento_api_key,
        trader_id=trader_id,
        log_level=log_level,
    )


def _get_live_settings(ctx: typer.Context) -> LiveRunSettings:
    settings = ctx.find_object(LiveRunSettings)
    if settings is None:
        raise RuntimeError("Live settings were not configured")

    return settings


def _run_live(
    settings: LiveRunSettings,
    strategy: Strategy,
) -> None:
    data_clients: dict[str, LiveDataClientConfig] = {}
    data_client_factories: dict[str, type[LiveDataClientFactory]] = {}
    exec_clients: dict[str, LiveExecClientConfig] = {}
    exec_client_factories: dict[str, type[LiveExecClientFactory]] = {}

    if settings.databento_api_key:
        data_clients[DATABENTO] = DatabentoDataClientConfig(
            api_key=settings.databento_api_key,
            use_exchange_as_venue=False,
        )
        data_client_factories[DATABENTO] = DatabentoLiveDataClientFactory

    if (settings.username and settings.password) or (settings.access_token and settings.md_access_token):
        base_url = (
            PRODUCTION_DEMO_HTTP_URL
            if settings.environment == TradovateEnvironment.DEMO
            else PRODUCTION_LIVE_HTTP_URL
        )
        user_ws_base_url = (
            PRODUCTION_DEMO_WS_URL
            if settings.environment == TradovateEnvironment.DEMO
            else PRODUCTION_LIVE_WS_URL
        )
        market_data_ws_base_url = (
            PRODUCTION_DEMO_MARKET_DATA_WS_URL
            if settings.environment == TradovateEnvironment.DEMO
            else PRODUCTION_LIVE_MARKET_DATA_WS_URL
        )
        tradovate_auth: dict[str, Any] = {
            "username": settings.username,
            "password": settings.password,
            "app_id": settings.app_id,
            "app_version": settings.app_version,
            "cid": settings.cid,
            "sec": settings.sec,
            "device_id": settings.device_id,
            "access_token": settings.access_token,
            "md_access_token": settings.md_access_token,
            "base_url": base_url,
        }
        data_clients[TRADOVATE] = TradovateDataClientConfig(
            **tradovate_auth,
            ws_base_url=market_data_ws_base_url,
        )
        exec_clients[TRADOVATE] = TradovateExecClientConfig(
            **tradovate_auth,
            account_id=settings.account_id,
            ws_base_url=user_ws_base_url,
        )
        data_client_factories[TRADOVATE] = TradovateLiveDataClientFactory
        exec_client_factories[TRADOVATE] = TradovateLiveExecClientFactory

    if not data_clients:
        raise RuntimeError("No live data provider credentials are configured")

    node = TradingNode(
        config=TradingNodeConfig(
            trader_id=TraderId(settings.trader_id),
            logging=LoggingConfig(log_level=settings.log_level),
            data_clients=data_clients,
            exec_clients=exec_clients,
            exec_engine=LiveExecEngineConfig(
                reconciliation=True,
                filter_unclaimed_external_orders=False,
                generate_missing_orders=True,
                open_check_interval_secs=5.0,
                position_check_interval_secs=5.0,
                reconciliation_startup_delay_secs=2.0,
            ),
        ),
    )
    for name, data_factory in data_client_factories.items():
        node.add_data_client_factory(name, data_factory)
    for name, exec_factory in exec_client_factories.items():
        node.add_exec_client_factory(name, exec_factory)

    try:
        node.build()
        node.trader.add_strategy(strategy)
        node.run(raise_exception=True)
    finally:
        node.dispose()


from cli.live.subscribe import cli as subscribe_cli
from cli.live.execute import cli as execute_cli


cli.add_typer(subscribe_cli, name="subscribe")
cli.add_typer(execute_cli, name="execute")
