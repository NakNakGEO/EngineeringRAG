"""FastAPI application factory."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager

from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncEngine

from eios_api.container import build_container
from eios_api.routes import health, runs
from eios_core import __version__
from eios_core.asgi import CorrelationIdMiddleware
from eios_core.health import ComponentHealth
from eios_core.logging import configure_logging, get_logger
from eios_core.settings import Settings, get_settings
from eios_storage import build_async_engine, check_database

SERVICE_NAME = "engineering-api"

ReadinessCheck = Callable[[], Awaitable[ComponentHealth]]


def create_app(
    settings: Settings | None = None,
    *,
    engine: AsyncEngine | None = None,
    readiness_checks: Sequence[ReadinessCheck] | None = None,
) -> FastAPI:
    """Build the API.

    ``engine`` and ``readiness_checks`` are injection points (explicit dependency injection, no
    globals): production uses the defaults, tests pass their own.
    """
    resolved = settings or get_settings()
    owns_engine = engine is None
    # Engines connect lazily, so building one here is cheap and keeps the app fully wired even if
    # the ASGI server does not run the lifespan (readiness must never depend on it).
    db_engine = engine or build_async_engine(resolved)

    async def database_check() -> ComponentHealth:
        return await check_database(
            db_engine, timeout_seconds=resolved.database_connect_timeout_seconds
        )

    container = build_container(resolved, db_engine)
    checks: list[ReadinessCheck] = (
        [database_check] if readiness_checks is None else list(readiness_checks)
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        configure_logging(
            service=SERVICE_NAME,
            environment=resolved.environment,
            level=resolved.log_level,
            json_logs=resolved.log_json,
        )
        get_logger("eios.api").info("api_started", version=__version__)
        try:
            yield
        finally:
            await container.background.shutdown()
            if owns_engine:
                await db_engine.dispose()
            get_logger("eios.api").info("api_stopped")

    app = FastAPI(title="Engineering Intelligence OS", version=__version__, lifespan=lifespan)
    app.state.engine = db_engine
    app.state.container = container
    app.state.readiness_checks = checks
    app.add_middleware(CorrelationIdMiddleware)
    app.include_router(health.router)
    app.include_router(runs.router)
    return app
