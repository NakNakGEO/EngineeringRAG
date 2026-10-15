"""Database health probe. Reports availability without leaking connection details."""

from __future__ import annotations

import asyncio
import time

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from eios_core.health import ComponentHealth
from eios_core.logging import get_logger

_log = get_logger("eios.storage.health")


async def check_database(engine: AsyncEngine, *, timeout_seconds: float = 3.0) -> ComponentHealth:
    """Run ``SELECT 1`` against the Engineering OS database."""
    started = time.perf_counter()
    try:
        async with asyncio.timeout(timeout_seconds):
            async with engine.connect() as connection:
                await connection.execute(text("SELECT 1"))
    except Exception as exc:
        # Details (host, user, driver messages) go to the log only, never the response.
        _log.warning("database_check_failed", error_type=type(exc).__name__, error=str(exc))
        return ComponentHealth(name="database", status="fail", detail="database unavailable")
    latency = round((time.perf_counter() - started) * 1000, 2)
    return ComponentHealth(name="database", status="ok", latency_ms=latency)
