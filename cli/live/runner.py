import typer


from typing import Any


from ibapi.common import MarketDataTypeEnum
from nautilus_trader.adapters.databento import DATABENTO
from nautilus_trader.adapters.databento import DatabentoDataClientConfig
from nautilus_trader.adapters.databento import DatabentoDataLoader
from nautilus_trader.adapters.interactive_brokers.common import IB
from nautilus_trader.adapters.interactive_brokers.config import InteractiveBrokersDataClientConfig
from nautilus_trader.adapters.interactive_brokers.config import InteractiveBrokersExecClientConfig
from nautilus_trader.adapters.interactive_brokers.config import InteractiveBrokersInstrumentProviderConfig
from nautilus_trader.adapters.interactive_brokers.factories import InteractiveBrokersLiveDataClientFactory
from nautilus_trader.adapters.interactive_brokers.factories import InteractiveBrokersLiveExecClientFactory
from nautilus_trader.adapters.interactive_brokers.parsing.instruments import exchange_supports_sec_type
from nautilus_trader.adapters.interactive_brokers.parsing.instruments import instrument_id_to_ib_contract
from nautilus_trader.common.config import LoggingConfig
from nautilus_trader.config import InstrumentProviderConfig
from nautilus_trader.config import LiveDataClientConfig
from nautilus_trader.config import LiveDataEngineConfig
from nautilus_trader.config import LiveExecClientConfig
from nautilus_trader.config import LiveExecEngineConfig
from nautilus_trader.config import RoutingConfig
from nautilus_trader.config import TradingNodeConfig
from nautilus_trader.live.factories import LiveDataClientFactory
from nautilus_trader.live.factories import LiveExecClientFactory
from nautilus_trader.live.node import TradingNode
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.identifiers import TraderId
from nautilus_trader.trading.strategy import Strategy


from phillip.adapters.databento import RecentDefinitionDatabentoLiveDataClientFactory
from phillip.adapters.tradovate.config import TradovateDataClientConfig
from phillip.adapters.tradovate.config import TradovateExecClientConfig
from phillip.adapters.tradovate.core import PRODUCTION_DEMO_HTTP_URL
from phillip.adapters.tradovate.core import PRODUCTION_DEMO_MARKET_DATA_WS_URL
from phillip.adapters.tradovate.core import PRODUCTION_DEMO_WS_URL
from phillip.adapters.tradovate.core import PRODUCTION_LIVE_HTTP_URL
from phillip.adapters.tradovate.core import PRODUCTION_LIVE_MARKET_DATA_WS_URL
from phillip.adapters.tradovate.core import PRODUCTION_LIVE_WS_URL
from phillip.adapters.tradovate.core import TRADOVATE
from phillip.adapters.tradovate.core import TRADOVATE_VENUE
from phillip.adapters.tradovate.factories import TradovateLiveDataClientFactory
from phillip.adapters.tradovate.factories import TradovateLiveExecClientFactory


from cli.live.settings import LiveRunSettings
from cli.live.settings import TradovateEnvironment


def _run_live(
    settings: LiveRunSettings,
    strategy: Strategy,
    data_instrument_ids: list[InstrumentId],
    execution_instrument_ids: list[InstrumentId] | None = None,
) -> None:
    execution_instrument_ids = execution_instrument_ids or []
    instrument_ids = set(data_instrument_ids).union(execution_instrument_ids)
    data_clients: dict[str, LiveDataClientConfig] = {}
    data_client_factories: dict[str, type[LiveDataClientFactory]] = {}
    exec_clients: dict[str, LiveExecClientConfig] = {}
    exec_client_factories: dict[str, type[LiveExecClientFactory]] = {}
    routable_data_instrument_ids: set[InstrumentId] = set()
    routable_execution_instrument_ids: set[InstrumentId] = set()

    if settings.databento_api_key:
        databento_loader = DatabentoDataLoader()
        databento_instrument_ids: list[InstrumentId] = []
        for instrument_id in data_instrument_ids:
            try:
                databento_loader.get_dataset_for_venue(instrument_id.venue)
            except ValueError:
                continue

            databento_instrument_ids.append(instrument_id)

        data_clients[DATABENTO] = DatabentoDataClientConfig(
            api_key=settings.databento_api_key,
            instrument_ids=databento_instrument_ids or None,
            instrument_provider=InstrumentProviderConfig(
                load_ids=frozenset(databento_instrument_ids) or None,
            ),
            routing=RoutingConfig(
                venues=frozenset(instrument_id.venue.value for instrument_id in databento_instrument_ids),
            ),
            use_exchange_as_venue=False,
        )
        data_client_factories[DATABENTO] = RecentDefinitionDatabentoLiveDataClientFactory
        routable_data_instrument_ids.update(databento_instrument_ids)

    if settings.ib_account_id is not None:
        if not settings.ib_account_id.strip():
            raise ValueError("IB_ACCOUNT_ID must not be empty")
        if settings.ib_host is None:
            raise ValueError("IB_ACCOUNT_ID requires IB_HOST")

    if settings.ib_host is not None:
        if not settings.ib_host.strip():
            raise ValueError("IB_HOST must not be empty")
        if not 1 <= settings.ib_port <= 65535:
            raise ValueError("IB_PORT must be between 1 and 65535")
        if not 1 <= settings.ib_client_id <= 2147483647:
            raise ValueError("IB_CLIENT_ID must be a positive 32-bit integer, unique per process")

        ib_instrument_ids = [
            instrument_id for instrument_id in instrument_ids
            if exchange_supports_sec_type(instrument_id.venue.value, "FUT")
        ]
        for instrument_id in ib_instrument_ids:
            contract = instrument_id_to_ib_contract(instrument_id, exchange=instrument_id.venue.value)
            if contract.secType != "FUT":
                raise ValueError(f"IBKR requires a dated future, e.g. NQZ6.CME: {instrument_id}")

        ib_instrument_provider = InteractiveBrokersInstrumentProviderConfig(load_ids=frozenset(ib_instrument_ids))
        ib_routing = RoutingConfig(venues=frozenset(instrument_id.venue.value for instrument_id in ib_instrument_ids))

        data_clients[IB] = InteractiveBrokersDataClientConfig(
            ibg_host=settings.ib_host.strip(),
            ibg_port=settings.ib_port,
            ibg_client_id=settings.ib_client_id,
            instrument_provider=ib_instrument_provider,
            routing=ib_routing,
            market_data_type=MarketDataTypeEnum.REALTIME,
            use_regular_trading_hours=False,
        )
        data_client_factories[IB] = InteractiveBrokersLiveDataClientFactory
        routable_data_instrument_ids.update(ib_instrument_ids)
        if settings.ib_account_id is not None:
            exec_clients[IB] = InteractiveBrokersExecClientConfig(
                ibg_host=settings.ib_host.strip(),
                ibg_port=settings.ib_port,
                ibg_client_id=settings.ib_client_id,
                account_id=settings.ib_account_id.strip(),
                instrument_provider=ib_instrument_provider,
                routing=ib_routing,
            )
            exec_client_factories[IB] = InteractiveBrokersLiveExecClientFactory
            routable_execution_instrument_ids.update(ib_instrument_ids)

    tradovate_credentials = (
        settings.tradovate_username,
        settings.tradovate_password,
        settings.tradovate_app_id,
        settings.tradovate_cid is not None,
        settings.tradovate_sec,
    )
    if all(tradovate_credentials):
        base_url = (
            PRODUCTION_DEMO_HTTP_URL
            if settings.tradovate_environment == TradovateEnvironment.DEMO
            else PRODUCTION_LIVE_HTTP_URL
        )
        user_ws_base_url = (
            PRODUCTION_DEMO_WS_URL
            if settings.tradovate_environment == TradovateEnvironment.DEMO
            else PRODUCTION_LIVE_WS_URL
        )
        market_data_ws_base_url = (
            PRODUCTION_DEMO_MARKET_DATA_WS_URL
            if settings.tradovate_environment == TradovateEnvironment.DEMO
            else PRODUCTION_LIVE_MARKET_DATA_WS_URL
        )
        tradovate_auth: dict[str, Any] = {
            "username": settings.tradovate_username,
            "password": settings.tradovate_password,
            "app_id": settings.tradovate_app_id,
            "app_version": settings.tradovate_app_version,
            "cid": settings.tradovate_cid,
            "sec": settings.tradovate_sec,
            "device_id": settings.tradovate_device_id,
            "base_url": base_url,
        }
        tradovate_instrument_ids = frozenset(
            instrument_id for instrument_id in instrument_ids
            if instrument_id.venue == TRADOVATE_VENUE
        )
        tradovate_instrument_provider = InstrumentProviderConfig(load_ids=tradovate_instrument_ids or None)
        tradovate_routing = RoutingConfig(venues=frozenset({TRADOVATE_VENUE.value}))
        data_clients[TRADOVATE] = TradovateDataClientConfig(
            **tradovate_auth,
            instrument_provider=tradovate_instrument_provider,
            ws_base_url=market_data_ws_base_url,
            routing=tradovate_routing,
        )
        data_client_factories[TRADOVATE] = TradovateLiveDataClientFactory
        routable_data_instrument_ids.update(tradovate_instrument_ids)
        exec_clients[TRADOVATE] = TradovateExecClientConfig(
            **tradovate_auth,
            account_id=settings.tradovate_account_id,
            instrument_provider=tradovate_instrument_provider,
            ws_base_url=user_ws_base_url,
            routing=tradovate_routing,
        )
        exec_client_factories[TRADOVATE] = TradovateLiveExecClientFactory
        routable_execution_instrument_ids.update(tradovate_instrument_ids)
    elif any(tradovate_credentials):
        raise typer.BadParameter(
            "TRADOVATE_USERNAME, TRADOVATE_PASSWORD, TRADOVATE_APP_ID, TRADOVATE_CID, "
            "and TRADOVATE_SEC must be supplied together",
        )

    if not data_clients:
        raise RuntimeError("No live data provider credentials are configured")

    unroutable_data_instrument_ids = [
        instrument_id
        for instrument_id in data_instrument_ids
        if instrument_id not in routable_data_instrument_ids
    ]
    if unroutable_data_instrument_ids:
        unroutable = ", ".join(str(instrument_id) for instrument_id in unroutable_data_instrument_ids)
        raise RuntimeError(f"No live data provider is configured for: {unroutable}")

    unroutable_execution_instrument_ids = [
        instrument_id for instrument_id in execution_instrument_ids
        if instrument_id not in routable_execution_instrument_ids
    ]
    if unroutable_execution_instrument_ids:
        unroutable = ", ".join(str(instrument_id) for instrument_id in unroutable_execution_instrument_ids)
        raise RuntimeError(f"No live execution adapter is configured for: {unroutable}")

    node = TradingNode(
        config=TradingNodeConfig(
            trader_id=TraderId(settings.trader_id),
            logging=LoggingConfig(log_level=settings.log_level),
            data_clients=data_clients,
            data_engine=LiveDataEngineConfig(
                time_bars_build_with_no_updates=False,
            ),
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
