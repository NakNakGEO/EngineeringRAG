"""Minimal HTTP health endpoint for processes that are not web apps (the worker).

Deliberately tiny and dependency-free: it serves only ``GET /health/live`` and
``GET /health/ready`` and closes the connection after each response.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable

from eios_core.health import HealthReport
from eios_core.logging import get_logger

HealthProvider = Callable[[], Awaitable[HealthReport]]

_MAX_REQUEST_LINE = 2048
_READ_TIMEOUT_SECONDS = 5.0
_log = get_logger("eios.worker.health")

_REASONS = {
    200: "OK",
    404: "Not Found",
    405: "Method Not Allowed",
    400: "Bad Request",
    503: "Service Unavailable",
}


def _response(status: int, body: str) -> bytes:
    payload = body.encode()
    head = (
        f"HTTP/1.1 {status} {_REASONS[status]}\r\n"
        "Content-Type: application/json\r\n"
        f"Content-Length: {len(payload)}\r\n"
        "Connection: close\r\n\r\n"
    )
    return head.encode() + payload


class HealthServer:
    def __init__(
        self, host: str, port: int, *, live: HealthProvider, ready: HealthProvider
    ) -> None:
        self._host = host
        self._port = port
        self._live = live
        self._ready = ready
        self._server: asyncio.Server | None = None

    @property
    def port(self) -> int:
        """Actual bound port (useful when started with port 0 in tests)."""
        if self._server is None or not self._server.sockets:
            return self._port
        port: int = self._server.sockets[0].getsockname()[1]
        return port

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, self._host, self._port)

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            try:
                async with asyncio.timeout(_READ_TIMEOUT_SECONDS):
                    line = await reader.readline()
            except TimeoutError:
                return
            if len(line) > _MAX_REQUEST_LINE:
                writer.write(_response(400, '{"detail":"request too large"}'))
                return
            parts = line.decode("latin-1").split()
            if len(parts) < 2:
                writer.write(_response(400, '{"detail":"bad request"}'))
                return
            method, target = parts[0], parts[1].split("?", 1)[0]
            if method != "GET":
                writer.write(_response(405, '{"detail":"method not allowed"}'))
            elif target == "/health/live":
                writer.write(_response(200, (await self._live()).model_dump_json()))
            elif target == "/health/ready":
                report = await self._ready()
                writer.write(
                    _response(200 if report.status == "ok" else 503, report.model_dump_json())
                )
            else:
                writer.write(_response(404, '{"detail":"not found"}'))
            await writer.drain()
        except Exception as exc:  # a broken probe must never take the worker down
            _log.warning("health_request_failed", error_type=type(exc).__name__)
        finally:
            writer.close()
            with contextlib.suppress(ConnectionError):
                await writer.wait_closed()
