import asyncio
import urllib.parse
import msgspec


from datetime import datetime
from typing import Any


from nautilus_trader import NAUTILUS_USER_AGENT
from nautilus_trader.common.component import Logger
from nautilus_trader.core.nautilus_pyo3 import HttpClient
from nautilus_trader.core.nautilus_pyo3 import HttpMethod
from nautilus_trader.core.nautilus_pyo3 import HttpResponse


from ..errors import TradovateApiError


class TradovateHttpClient:
    def __init__(
        self,
        base_url: str,
        username: str | None = None,
        password: str | None = None,
        app_id: str | None = None,
        app_version: str = "1.0",
        cid: int | None = None,
        sec: str | None = None,
        device_id: str | None = None,
        access_token: str | None = None,
        md_access_token: str | None = None,
        timeout_secs: float = 15.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._username = username
        self._password = password
        self._app_id = app_id
        self._app_version = app_version
        self._cid = cid
        self._sec = sec
        self._device_id = device_id
        self._access_token = access_token
        self._md_access_token = md_access_token
        self._expiration_ns: int | None = None
        self._timeout_secs = timeout_secs
        self._client = HttpClient(keyed_quotas=[], default_quota=None)
        self._auth_lock = asyncio.Lock()
        self._log = Logger(type(self).__name__)

    async def get_access_token(self, market_data: bool = False) -> str:
        async with self._auth_lock:
            if self._tokens_expiring():
                if self._access_token is not None:
                    try:
                        await self._renew_access_token()
                    except TradovateApiError:
                        self._access_token = None
                        self._md_access_token = None
                if self._access_token is None:
                    await self._request_access_token()

            token = self._md_access_token if market_data else self._access_token
            if not token:
                token_name = "mdAccessToken" if market_data else "accessToken"
                raise TradovateApiError(f"Authentication response did not contain {token_name}")
            return token

    async def get_contract(self, name: str) -> dict[str, Any]:
        return await self._get("/contract/find", {"name": name})

    async def get_contract_maturity(self, maturity_id: int) -> dict[str, Any]:
        return await self._get("/contractMaturity/item", {"id": str(maturity_id)})

    async def get_product(self, product_id: int) -> dict[str, Any]:
        return await self._get("/product/item", {"id": str(product_id)})

    async def get_currency(self, currency_id: int) -> dict[str, Any]:
        return await self._get("/currency/item", {"id": str(currency_id)})

    async def _request_access_token(self) -> None:
        missing = [
            name
            for name, value in (
                ("username", self._username),
                ("password", self._password),
                ("app_id", self._app_id),
                ("cid", self._cid),
                ("sec", self._sec),
            )
            if value in (None, "")
        ]
        if missing:
            raise TradovateApiError(
                "Missing credentials: " + ", ".join(missing) + ". Supply credentials or both access tokens.",
            )

        payload: dict[str, Any] = {
            "name": self._username,
            "password": self._password,
            "appId": self._app_id,
            "appVersion": self._app_version,
            "cid": self._cid,
            "sec": self._sec,
        }
        if self._device_id:
            payload["deviceId"] = self._device_id

        result = await self._request(
            method=HttpMethod.POST,
            path="/auth/accesstokenrequest",
            body=msgspec.json.encode(payload),
            authenticated=False,
        )
        self._store_tokens(result)

    async def _renew_access_token(self) -> None:
        result = await self._request(
            method=HttpMethod.GET,
            path="/auth/renewaccesstoken",
            authenticated=True,
        )
        self._store_tokens(result)

    async def _get(self, path: str, params: dict[str, str]) -> dict[str, Any]:
        result = await self._request(
            method=HttpMethod.GET,
            path=path,
            params=params,
            authenticated=True,
        )
        if not isinstance(result, dict):
            raise TradovateApiError(f"Expected an object from {path}", details=result)
        return result

    async def _request(
        self,
        method: HttpMethod,
        path: str,
        params: dict[str, str] | None = None,
        body: bytes | None = None,
        authenticated: bool = True,
    ) -> Any:
        url = self._base_url + path
        if params:
            url += "?" + urllib.parse.urlencode(params)

        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": NAUTILUS_USER_AGENT,
        }
        if authenticated:
            if self._access_token is None:
                await self.get_access_token()
            headers["Authorization"] = f"Bearer {self._access_token}"

        response: HttpResponse = await self._client.request(
            method,
            url=url,
            headers=headers,
            body=body,
            timeout_secs=self._timeout_secs,
        )
        return self._decode_response(response)

    def _decode_response(self, response: HttpResponse) -> Any:
        decoded = msgspec.json.decode(response.body) if response.body else {}
        if response.status < 200 or response.status >= 300:
            message = decoded.get("errorText") if isinstance(decoded, dict) else None
            raise TradovateApiError(message or str(decoded), status=response.status, details=decoded)
        if isinstance(decoded, dict) and decoded.get("errorText"):
            raise TradovateApiError(str(decoded["errorText"]), status=response.status, details=decoded)
        return decoded

    def _store_tokens(self, response: dict[str, Any]) -> None:
        self._access_token = response.get("accessToken", self._access_token)
        self._md_access_token = response.get("mdAccessToken", self._md_access_token)
        expiration = response.get("expirationTime")
        if expiration:
            normalized = expiration[:-1] + "+00:00" if expiration.endswith("Z") else expiration
            self._expiration_ns = int(datetime.fromisoformat(normalized).timestamp() * 1_000_000_000)
        self._log.info("Tradovate access tokens acquired")

    def _tokens_expiring(self) -> bool:
        if self._access_token is None or self._md_access_token is None:
            return True
        if self._expiration_ns is None:
            return False
        now_ns = int(datetime.now().timestamp() * 1_000_000_000)
        return now_ns >= self._expiration_ns - 15 * 60 * 1_000_000_000
