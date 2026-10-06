"""MCP server factory.

Phase 0 exposes exactly one tool, ``health``. The compact tool surface from the master plan
(bootstrap_project, get_context, ...) arrives in Phase 8. This server never exposes database
primitives or any external-database operation.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from sqlalchemy.ext.asyncio import AsyncEngine
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from eios_core import __version__
from eios_core.asgi import CorrelationIdMiddleware
from eios_core.health import ComponentHealth, HealthReport, build_report
from eios_core.settings import Settings
from eios_storage import build_async_engine, check_database

SERVICE_NAME = "engineering-mcp"

ReadinessCheck = Callable[[], Awaitable[ComponentHealth]]


def create_mcp_server(
    settings: Settings,
    *,
    engine: AsyncEngine | None = None,
    readiness_checks: Sequence[ReadinessCheck] | None = None,
) -> MCPServer[Any]:
    """Build the MCP server with its health tool and ``/health/*`` HTTP routes."""
    db_engine = engine or build_async_engine(settings)
    owns_engine = engine is None

    if readiness_checks is None:

        async def database_check() -> ComponentHealth:
            return await check_database(
                db_engine, timeout_seconds=settings.database_connect_timeout_seconds
            )

        checks: list[ReadinessCheck] = [database_check]
    else:
        checks = list(readiness_checks)

    async def ready_report() -> HealthReport:
        return build_report(SERVICE_NAME, [await check() for check in checks])

    @asynccontextmanager
    async def lifespan(_server: MCPServer[Any]) -> AsyncIterator[None]:
        try:
            yield
        finally:
            if owns_engine:
                await db_engine.dispose()

    mcp = MCPServer(
        name="engineering-intelligence-os",
        version=__version__,
        instructions=(
            "Engineering Intelligence OS. Phase 0: only the 'health' capability is available."
        ),
        lifespan=lifespan,
    )

    @mcp.tool(name="health", description="Report Engineering OS MCP server health.")
    async def health() -> HealthReport:
        return await ready_report()

    # The MCP SDK does not type custom_route; narrow ignore, strict mode stays on.
    @mcp.custom_route("/health/live", methods=["GET"])  # type: ignore[untyped-decorator]
    async def live(_request: Request) -> Response:
        return JSONResponse(build_report(SERVICE_NAME).model_dump())

    # The MCP SDK does not type custom_route; narrow ignore, strict mode stays on.
    @mcp.custom_route("/health/ready", methods=["GET"])  # type: ignore[untyped-decorator]
    async def ready(_request: Request) -> Response:
        report = await ready_report()
        return JSONResponse(report.model_dump(), status_code=200 if report.status == "ok" else 503)

    return mcp


def create_app(
    settings: Settings,
    *,
    engine: AsyncEngine | None = None,
    readiness_checks: Sequence[ReadinessCheck] | None = None,
) -> Starlette:
    """ASGI app serving MCP over streamable HTTP, with correlation-ID middleware."""
    mcp = create_mcp_server(settings, engine=engine, readiness_checks=readiness_checks)
    allowed_hosts = [
        f"{host}:{settings.mcp_port}" for host in ("localhost", "127.0.0.1", "[::1]", "mcp")
    ]
    app = mcp.streamable_http_app(
        host=settings.mcp_host,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=allowed_hosts,
            allowed_origins=[f"http://{host}" for host in allowed_hosts],
        ),
    )
    app.add_middleware(CorrelationIdMiddleware)
    return app
