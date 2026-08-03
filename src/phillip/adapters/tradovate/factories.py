import asyncio


from functools import lru_cache


from nautilus_trader.cache.cache import Cache
from nautilus_trader.common.component import LiveClock
from nautilus_trader.common.component import MessageBus
from nautilus_trader.config import InstrumentProviderConfig
from nautilus_trader.config import LiveDataClientConfig
from nautilus_trader.config import LiveExecClientConfig
from nautilus_trader.live.factories import LiveDataClientFactory
from nautilus_trader.live.factories import LiveExecClientFactory

from .config import TradovateDataClientConfig
from .config import TradovateExecClientConfig
from .data import TradovateDataClient
from .execution import TradovateExecutionClient
from .http.client import TradovateHttpClient
from .providers import TradovateInstrumentProvider


def get_tradovate_http_client(
    config: TradovateDataClientConfig | TradovateExecClientConfig,
) -> TradovateHttpClient:
    return _cached_tradovate_http_client(
        config.base_url,
        config.username,
        config.password,
        config.app_id,
        config.app_version,
        config.cid,
        config.sec,
        config.device_id,
        config.access_token,
        config.md_access_token,
        config.request_timeout_secs,
    )


@lru_cache(2)
def _cached_tradovate_http_client(
    base_url: str,
    username: str | None,
    password: str | None,
    app_id: str | None,
    app_version: str,
    cid: int | None,
    sec: str | None,
    device_id: str | None,
    access_token: str | None,
    md_access_token: str | None,
    request_timeout_secs: int,
) -> TradovateHttpClient:
    return TradovateHttpClient(
        base_url=base_url,
        username=username,
        password=password,
        app_id=app_id,
        app_version=app_version,
        cid=cid,
        sec=sec,
        device_id=device_id,
        access_token=access_token,
        md_access_token=md_access_token,
        timeout_secs=request_timeout_secs,
    )


@lru_cache(2)
def get_tradovate_instrument_provider(
    client: TradovateHttpClient,
    clock: LiveClock,
    config: InstrumentProviderConfig,
) -> TradovateInstrumentProvider:
    return TradovateInstrumentProvider(client=client, clock=clock, config=config)


class TradovateLiveDataClientFactory(LiveDataClientFactory):
    @staticmethod
    def create(
        loop: asyncio.AbstractEventLoop,
        name: str,
        config: LiveDataClientConfig,
        msgbus: MessageBus,
        cache: Cache,
        clock: LiveClock,
    ) -> TradovateDataClient:
        if not isinstance(config, TradovateDataClientConfig):
            raise TypeError(f"Expected TradovateDataClientConfig, got {type(config).__name__}")
        http_client = get_tradovate_http_client(config)
        provider = get_tradovate_instrument_provider(http_client, clock, config.instrument_provider)
        return TradovateDataClient(
            loop=loop,
            http_client=http_client,
            msgbus=msgbus,
            cache=cache,
            clock=clock,
            instrument_provider=provider,
            config=config,
            name=name,
        )


class TradovateLiveExecClientFactory(LiveExecClientFactory):
    @staticmethod
    def create(
        loop: asyncio.AbstractEventLoop,
        name: str,
        config: LiveExecClientConfig,
        msgbus: MessageBus,
        cache: Cache,
        clock: LiveClock,
    ) -> TradovateExecutionClient:
        if not isinstance(config, TradovateExecClientConfig):
            raise TypeError(f"Expected TradovateExecClientConfig, got {type(config).__name__}")
        http_client = get_tradovate_http_client(config)
        provider = get_tradovate_instrument_provider(http_client, clock, config.instrument_provider)
        return TradovateExecutionClient(
            loop=loop,
            http_client=http_client,
            msgbus=msgbus,
            cache=cache,
            clock=clock,
            instrument_provider=provider,
            config=config,
            name=name,
        )
