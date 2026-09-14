from dataclasses import dataclass
from enum import Enum


class TradovateEnvironment(str, Enum):
    DEMO = "demo"
    LIVE = "live"


@dataclass(frozen=True)
class LiveRunSettings:
    tradovate_environment: TradovateEnvironment
    tradovate_username: str | None
    tradovate_password: str | None
    tradovate_app_id: str | None
    tradovate_app_version: str
    tradovate_cid: int | None
    tradovate_sec: str | None
    tradovate_device_id: str | None
    tradovate_account_id: int | None
    databento_api_key: str | None
    trader_id: str
    log_level: str
    ib_host: str | None = None
    ib_port: int = 7497
    ib_client_id: int = 101
    ib_account_id: str | None = None
