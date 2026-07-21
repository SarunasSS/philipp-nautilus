from typing import Any


class TradovateError(RuntimeError):
    """Base error raised by the Tradovate adapter."""


class TradovateApiError(TradovateError):
    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        details: Any = None,
    ) -> None:
        self.status = status
        self.details = details
        prefix = f"Tradovate API status {status}: " if status is not None else "Tradovate API: "
        super().__init__(prefix + message)


class TradovateProtocolError(TradovateError):
    """Raised for malformed or unsupported WebSocket protocol messages."""
