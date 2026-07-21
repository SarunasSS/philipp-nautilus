import asyncio
import json


from collections.abc import Awaitable
from collections.abc import Callable
from typing import Any


from nautilus_trader.common.component import Logger
from nautilus_trader.common.enums import LogColor
from nautilus_trader.core.nautilus_pyo3 import WebSocketClient
from nautilus_trader.core.nautilus_pyo3 import WebSocketConfig


from ..common import decode_sockjs_frame
from ..errors import TradovateApiError
from ..errors import TradovateProtocolError


class TradovateWebSocketClient:
    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        base_url: str,
        handler: Callable[[dict[str, Any]], None],
        reconnect_handler: Callable[[], Awaitable[None]] | None = None,
        request_timeout_secs: float = 15.0,
    ) -> None:
        self._loop = loop
        self._base_url = base_url
        self._handler = handler
        self._reconnect_handler = reconnect_handler
        self._request_timeout_secs = request_timeout_secs
        self._log = Logger(type(self).__name__)
        self._client: WebSocketClient | None = None
        self._server_open = asyncio.Event()
        self._access_token: str | None = None
        self._request_id = 0
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._heartbeat_task: asyncio.Task[None] | None = None

    def is_connected(self) -> bool:
        return self._client is not None and self._client.is_active()

    async def connect(self, access_token: str) -> None:
        self._access_token = access_token
        self._server_open.clear()
        config = WebSocketConfig(
            url=self._base_url,
            headers=[],
            heartbeat=None,
            reconnect_timeout_ms=10_000,
        )
        self._client = await WebSocketClient.connect(
            loop_=self._loop,
            config=config,
            handler=self._handle_raw_message,
            post_reconnection=self._on_reconnected,
        )
        await asyncio.wait_for(self._server_open.wait(), timeout=self._request_timeout_secs)
        await self._authorize()
        self._start_heartbeat()
        self._log.info(f"Connected and authenticated to {self._base_url}", LogColor.BLUE)

    async def request(self, endpoint: str, body: dict[str, Any]) -> dict[str, Any]:
        return await self._request_text(endpoint, json.dumps(body, separators=(",", ":")))

    async def disconnect(self) -> None:
        if self._heartbeat_task is not None:
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass
            self._heartbeat_task = None

        if self._client is not None and not self._client.is_closed():
            await self._client.disconnect()
        self._client = None
        self._fail_pending(TradovateProtocolError("WebSocket disconnected"))

    async def _authorize(self) -> None:
        if not self._access_token:
            raise TradovateProtocolError("Cannot authorize market data WebSocket without a token")
        await self._request_text("authorize", self._access_token)

    async def _request_text(self, endpoint: str, body: str) -> dict[str, Any]:
        if not self.is_connected() or self._client is None:
            raise TradovateProtocolError("Cannot send request: WebSocket is not connected")

        self._request_id += 1
        request_id = self._request_id
        future = self._loop.create_future()
        self._pending[request_id] = future
        message = f"{endpoint}\n{request_id}\n\n{body}"
        try:
            await self._client.send_text(message.encode("utf-8"))
            response = await asyncio.wait_for(future, timeout=self._request_timeout_secs)
        finally:
            self._pending.pop(request_id, None)

        status = int(response.get("s", 0) or 0)
        if status < 200 or status >= 300:
            details = response.get("d")
            raise TradovateApiError(str(details or response), status=status, details=response)
        return response

    def _handle_raw_message(self, raw: bytes) -> None:
        text = raw.decode("utf-8")
        if text == "o":
            self._server_open.set()
            return
        if text.startswith("c["):
            self._log.error(f"Tradovate WebSocket closed by server: {text}")
            return

        try:
            messages = decode_sockjs_frame(text)
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            self._log.error(f"Invalid Tradovate WebSocket frame: {exc}")
            return

        for message in messages:
            request_id = message.get("i")
            if request_id is not None and int(request_id) in self._pending:
                future = self._pending[int(request_id)]
                if not future.done():
                    future.set_result(message)
                continue
            self._handler(message)

    def _on_reconnected(self) -> None:
        self._loop.create_task(self._restore_after_reconnect())

    async def _restore_after_reconnect(self) -> None:
        try:
            self._fail_pending(TradovateProtocolError("WebSocket reconnected during request"))
            await asyncio.sleep(0.1)
            await self._authorize()
            if self._reconnect_handler is not None:
                await self._reconnect_handler()
            self._log.info("Tradovate WebSocket reauthenticated and resubscribed")
        except Exception as exc:
            self._log.error(f"Tradovate WebSocket reconnect recovery failed: {exc}")

    def _start_heartbeat(self) -> None:
        if self._heartbeat_task is None or self._heartbeat_task.done():
            self._heartbeat_task = self._loop.create_task(self._heartbeat())

    async def _heartbeat(self) -> None:
        while self.is_connected():
            await asyncio.sleep(2.5)
            if self._client is not None and self.is_connected():
                await self._client.send_text(b"[]")

    def _fail_pending(self, exc: Exception) -> None:
        for future in self._pending.values():
            if not future.done():
                future.set_exception(exc)
