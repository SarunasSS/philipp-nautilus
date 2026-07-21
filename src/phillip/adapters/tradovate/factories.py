import asyncio
from functools import lru_cache

from nautilus_trader.cache.cache import Cache
from nautilus_trader.common.component import LiveClock
from nautilus_trader.common.component import MessageBus
from nautilus_trader.live.factories import LiveDataClientFactory

from .config import TradovateDataClientConfig
from .data import TradovateDataClient
from .http.client import TradovateHttpClient
from .providers import TradovateInstrumentProvider


@lru_cache(1)
def get_tradovate_http_client(config: TradovateDataClientConfig) -> TradovateHttpClient:
    return TradovateHttpClient(
        base_url=config.base_url,
        username=config.username,
        password=config.password,
        app_id=config.app_id,
        app_version=config.app_version,
        cid=config.cid,
        sec=config.sec,
        device_id=config.device_id,
        access_token=config.access_token,
        md_access_token=config.md_access_token,
        timeout_secs=config.request_timeout_secs,
    )


@lru_cache(1)
def get_tradovate_instrument_provider(
    client: TradovateHttpClient,
    clock: LiveClock,
    config: TradovateDataClientConfig,
) -> TradovateInstrumentProvider:
    return TradovateInstrumentProvider(client=client, clock=clock, config=config.instrument_provider)


class TradovateLiveDataClientFactory(LiveDataClientFactory):
    @staticmethod
    def create(
        loop: asyncio.AbstractEventLoop,
        name: str,
        config: TradovateDataClientConfig,
        msgbus: MessageBus,
        cache: Cache,
        clock: LiveClock,
    ) -> TradovateDataClient:
        http_client = get_tradovate_http_client(config)
        provider = get_tradovate_instrument_provider(http_client, clock, config)
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
