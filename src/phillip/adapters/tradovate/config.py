from nautilus_trader.config import LiveDataClientConfig


from .core import PRODUCTION_DEMO_HTTP_URL
from .core import PRODUCTION_MARKET_DATA_WS_URL


class TradovateDataClientConfig(LiveDataClientConfig, frozen=True):
    username: str | None = None
    password: str | None = None
    app_id: str | None = None
    app_version: str = "1.0"
    cid: int | None = None
    sec: str | None = None
    device_id: str | None = None
    access_token: str | None = None
    md_access_token: str | None = None
    base_url: str = PRODUCTION_DEMO_HTTP_URL
    ws_base_url: str = PRODUCTION_MARKET_DATA_WS_URL
    request_timeout_secs: int = 15
