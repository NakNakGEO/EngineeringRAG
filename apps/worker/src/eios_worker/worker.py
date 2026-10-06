"""Worker lifecycle skeleton.

Phase 0 scope: start, hold a database engine, emit heartbeats, expose health, shut down cleanly on
SIGTERM/SIGINT. The job queue, leases and job handlers arrive in later phases.
"""

from __future__ import annotations

import asyncio
import signal
import time
from collections.abc import Awaitable, Callable

from sqlalchemy.ext.asyncio import AsyncEngine

from eios_core import __version__
from eios_core.health import ComponentHealth, HealthReport, build_report
from eios_core.logging import get_logger
from eios_core.settings import Settings
from eios_storage import build_async_engine, check_database
from eios_worker.health_server import HealthServer

SERVICE_NAME = "engineering-worker"

DatabaseCheck = Callable[[], Awaitable[ComponentHealth]]
_log = get_logger("eios.worker")


class Worker:
    def __init__(
        self,
        settings: Settings,
        *,
        engine: AsyncEngine | None = None,
        database_check: DatabaseCheck | None = None,
    ) -> None:
        self._settings = settings
        self._owns_engine = engine is None
        self._engine = engine or build_async_engine(settings)
        self._database_check = database_check or self._default_database_check
        self._stop = asyncio.Event()
        self._last_heartbeat = time.monotonic()
        self._health = HealthServer(
            settings.worker_health_host,
            settings.worker_health_port,
            live=self.live_report,
            ready=self.ready_report,
        )

    @property
    def health_port(self) -> int:
        return self._health.port

    async def _default_database_check(self) -> ComponentHealth:
        return await check_database(
            self._engine, timeout_seconds=self._settings.database_connect_timeout_seconds
        )

    async def live_report(self) -> HealthReport:
        return build_report(SERVICE_NAME)

    async def ready_report(self) -> HealthReport:
        interval = self._settings.worker_heartbeat_seconds
        age = time.monotonic() - self._last_heartbeat
        heartbeat = ComponentHealth(
            name="heartbeat",
            status="ok" if age <= interval * 3 else "fail",
            detail=None if age <= interval * 3 else "worker loop is not ticking",
        )
        return build_report(SERVICE_NAME, [await self._database_check(), heartbeat])

    def request_stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        """Run until :meth:`request_stop` is called (or a signal arrives via :func:`main`)."""
        await self._health.start()
        _log.info("worker_started", version=__version__, health_port=self.health_port)
        try:
            while not self._stop.is_set():
                self._last_heartbeat = time.monotonic()
                _log.debug("worker_heartbeat")
                try:
                    async with asyncio.timeout(self._settings.worker_heartbeat_seconds):
                        await self._stop.wait()
                except TimeoutError:
                    continue
        finally:
            await self._health.stop()
            if self._owns_engine:
                await self._engine.dispose()
            _log.info("worker_stopped")


async def run_until_signalled(worker: Worker) -> None:
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, worker.request_stop)
    await worker.run()
