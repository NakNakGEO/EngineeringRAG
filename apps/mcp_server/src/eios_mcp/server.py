"""MCP server factory.

Exposes ``health`` plus the compact eight-tool surface from the master plan (see
:mod:`eios_mcp.tools`). This server never exposes database primitives, file or shell access, or any
external-database operation, and it cannot decide approvals or change Root Policy.
"""

from __future__ import annotations

import hmac
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from sqlalchemy.ext.asyncio import AsyncEngine
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send

from eios_core import __version__
from eios_core.asgi import CorrelationIdMiddleware
from eios_core.health import ComponentHealth, HealthReport, build_report
from eios_core.logging import get_logger
from eios_core.settings import Settings
from eios_mcp import tools as t
from eios_runtime import Container, build_container
from eios_storage import build_async_engine, check_database

SERVICE_NAME = "engineering-mcp"

ReadinessCheck = Callable[[], Awaitable[ComponentHealth]]
_OPEN_PATHS = ("/health/live", "/health/ready")


class BearerAuthMiddleware:
    """Optional shared-secret guard (``EIOS_MCP_TOKEN``) in front of the MCP endpoint."""

    def __init__(self, app: ASGIApp, token: str) -> None:
        self._app = app
        self._token = token.encode()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope["path"] not in _OPEN_PATHS:
            header = dict(scope["headers"]).get(b"authorization", b"")
            supplied = header[7:] if header.lower().startswith(b"bearer ") else b""
            if not hmac.compare_digest(supplied, self._token):
                response = JSONResponse({"error": "unauthorized"}, status_code=401)
                await response(scope, receive, send)
                return
        await self._app(scope, receive, send)


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

    container: Container = build_container(settings, db_engine)

    @asynccontextmanager
    async def lifespan(_server: MCPServer[Any]) -> AsyncIterator[None]:
        try:
            await container.sync_registries()
        except Exception as exc:  # stay up (and report unready) even if the DB is down
            get_logger("eios.mcp").error("registry_sync_failed", error=repr(exc))
        try:
            yield
        finally:
            await container.background.shutdown()
            if owns_engine:
                await db_engine.dispose()

    mcp = MCPServer(
        name="engineering-intelligence-os",
        version=__version__,
        instructions=(
            "Engineering Intelligence OS. Start with bootstrap_project, then get_context before "
            "acting. Use request_capability for anything executable; never improvise tools. "
            "If context is not ready_to_act, follow required_expansions. Report each workflow "
            "node with report_result and real evidence."
        ),
        lifespan=lifespan,
    )

    @mcp.tool(name="health", description="Report Engineering OS MCP server health.")
    async def health() -> HealthReport:
        return await ready_report()

    @mcp.tool(
        name="bootstrap_project",
        description="Identify a project inside an approved workspace and (optionally) queue an "
        "index sync. Returns project_id, branch, whether the index is current.",
    )
    async def bootstrap_project(path: str, sync: str = "if_needed") -> dict[str, Any]:
        return await t.bootstrap_project(container, path, sync)

    @mcp.tool(
        name="get_context",
        description="Build a budgeted context pack for a task. Reports ready_to_act, confidence, "
        "missing_context and required_expansions honestly; call before changing code.",
    )
    async def get_context(
        text: str,
        project_id: str | None = None,
        symbols: list[str] | None = None,
        paths: list[str] | None = None,
        risk: str = "medium",
        token_budget: int = 12000,
        max_level: str = "L4",
    ) -> dict[str, Any]:
        return await t.get_context(
            container, text, project_id, symbols, paths, risk, token_budget, max_level
        )

    @mcp.tool(
        name="search_knowledge",
        description="One hybrid retrieval pass over knowledge, memory, decisions and indexed code.",
    )
    async def search_knowledge(
        query: str, project_id: str | None = None, limit: int = 10
    ) -> dict[str, Any]:
        return await t.search_knowledge(container, query, project_id, limit)

    @mcp.tool(
        name="request_capability",
        description="Request a capability by name (e.g. sql_analyze, impact_analyze). The platform "
        "routes to a registered provider, applies policy and approvals, and returns the result - "
        "or an explicit gap. Unknown or forbidden capabilities are refused, never guessed.",
    )
    async def request_capability(
        capability: str,
        arguments: dict[str, Any] | None = None,
        run_id: str | None = None,
        language: str | None = None,
        extension: str | None = None,
        kind: str | None = None,
        path: str | None = None,
    ) -> dict[str, Any]:
        return await t.request_capability(
            container, capability, arguments, run_id, language, extension, kind, path
        )

    @mcp.tool(
        name="request_specialist",
        description="Get the specialist role definition(s) justified for a goal (or a named role): "
        "prompt, capabilities, forbidden actions. One writer, many reviewers.",
    )
    async def request_specialist(
        goal: str,
        role: str | None = None,
        risk: str = "medium",
        paths: list[str] | None = None,
        touches_data_layer: bool = False,
        tests_missing: bool = False,
    ) -> dict[str, Any]:
        return await t.request_specialist(
            container, goal, role, risk, paths, touches_data_layer, tests_missing
        )

    @mcp.tool(
        name="get_evidence",
        description="Fetch an evidence record (metadata and text content, size-limited).",
    )
    async def get_evidence(evidence_id: str, max_chars: int = 8000) -> dict[str, Any]:
        return await t.get_evidence(container, evidence_id, max_chars)

    @mcp.tool(
        name="report_result",
        description="Report the outcome of the active workflow node with outputs and evidence "
        "(evidence_ids, or inline evidence items {content, summary, label}). Acceptance criteria "
        "are checked by the platform; claims without evidence do not pass.",
    )
    async def report_result(
        workflow_id: str,
        node_id: str,
        reporter: str,
        outputs: dict[str, Any] | None = None,
        evidence_ids: list[str] | None = None,
        evidence: list[dict[str, str]] | None = None,
    ) -> dict[str, Any]:
        return await t.report_result(
            container, workflow_id, node_id, reporter, outputs, evidence_ids, evidence
        )

    @mcp.tool(
        name="get_run_state",
        description="Current state of a run or workflow: status, active node, criteria, "
        "approvals pending and the most recent events.",
    )
    async def get_run_state(
        run_id: str | None = None, workflow_id: str | None = None, events: int = 15
    ) -> dict[str, Any]:
        return await t.get_run_state(container, run_id, workflow_id, events)

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
    if settings.mcp_token is not None:
        app.add_middleware(BearerAuthMiddleware, token=settings.mcp_token.get_secret_value())
    return app
