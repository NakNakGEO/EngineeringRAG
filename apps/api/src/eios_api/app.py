"""FastAPI application factory."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager

from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncEngine

from eios_api.routes import health
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

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        configure_logging(
            service=SERVICE_NAME,
            environment=resolved.environment,
            level=resolved.log_level,
            json_logs=resolved.log_json,
        )
        db_engine = engine or build_async_engine(resolved)
        app.state.engine = db_engine
        if readiness_checks is None:

            async def database_check() -> ComponentHealth:
                return await check_database(
                    db_engine, timeout_seconds=resolved.database_connect_timeout_seconds
                )

            app.state.readiness_checks = [database_check]
        else:
            app.state.readiness_checks = list(readiness_checks)
        get_logger("eios.api").info("api_started", version=__version__)
        try:
            yield
        finally:
            if owns_engine:
                await db_engine.dispose()
            get_logger("eios.api").info("api_stopped")

    app = FastAPI(title="Engineering Intelligence OS", version=__version__, lifespan=lifespan)
    app.add_middleware(CorrelationIdMiddleware)
    app.include_router(health.router)
    return app
