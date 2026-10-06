"""Pure-ASGI middleware shared by the HTTP surfaces (API and MCP over HTTP)."""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any

from eios_core.correlation import (
    CORRELATION_ID_HEADER,
    correlation_scope,
    sanitize_correlation_id,
)
from eios_core.logging import get_logger

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

_HEADER_NAME = CORRELATION_ID_HEADER.lower().encode("latin-1")
_log = get_logger("eios.http")


class CorrelationIdMiddleware:
    """Bind a correlation ID per request, echo it in the response, and log the request.

    A valid inbound ``X-Correlation-ID`` is honoured; anything else is replaced by a fresh UUID.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        inbound: str | None = None
        for key, value in scope.get("headers", []):
            if key == _HEADER_NAME:
                inbound = sanitize_correlation_id(value.decode("latin-1"))
                break

        with correlation_scope(inbound) as correlation_id:
            started = time.perf_counter()
            status_code = 500

            async def send_with_header(message: Message) -> None:
                nonlocal status_code
                if message["type"] == "http.response.start":
                    status_code = int(message["status"])
                    headers = [
                        (k, v) for k, v in message.get("headers", []) if k.lower() != _HEADER_NAME
                    ]
                    headers.append((_HEADER_NAME, correlation_id.encode("latin-1")))
                    message["headers"] = headers
                await send(message)

            try:
                await self.app(scope, receive, send_with_header)
            finally:
                _log.info(
                    "http_request",
                    method=scope.get("method"),
                    path=scope.get("path"),
                    status_code=status_code,
                    duration_ms=round((time.perf_counter() - started) * 1000, 2),
                )
