from __future__ import annotations

from typing import Any

import pytest
from mcp.client import Client
from starlette.testclient import TestClient

from eios_core.health import ComponentHealth
from eios_mcp.server import create_app, create_mcp_server
from tests.conftest import make_settings


async def _ok() -> ComponentHealth:
    return ComponentHealth(name="database", status="ok")


async def _fail() -> ComponentHealth:
    return ComponentHealth(name="database", status="fail", detail="database unavailable")


EXPECTED_TOOLS = {
    "health", "bootstrap_project", "get_context", "search_knowledge", "request_capability",
    "request_specialist", "get_evidence", "report_result", "get_run_state",
}  # fmt: skip


async def test_exposes_exactly_the_compact_tool_surface() -> None:
    server = create_mcp_server(make_settings(), readiness_checks=[_ok])
    async with Client(server) as client:
        tools = await client.list_tools()
    assert {t.name for t in tools.tools} == EXPECTED_TOOLS


async def test_no_database_file_shell_or_policy_primitives_are_exposed() -> None:
    server = create_mcp_server(make_settings(), readiness_checks=[_ok])
    async with Client(server) as client:
        tools = (await client.list_tools()).tools
    forbidden = ("sql", "query_db", "execute", "shell", "run_command", "read_file", "write_file",
                 "approve", "root_policy", "set_policy", "connect")  # fmt: skip
    for tool in tools:
        assert not any(f in tool.name.lower() for f in forbidden), tool.name
        for param in (tool.input_schema or {}).get("properties", {}):
            assert not any(f in param.lower() for f in ("connection", "dsn", "password", "sql")), (
                tool.name, param,
            )  # fmt: skip


async def test_health_tool_reports_status() -> None:
    server = create_mcp_server(make_settings(), readiness_checks=[_ok])
    async with Client(server) as client:
        result = await client.call_tool("health", {})
    assert result.is_error is False
    structured: Any = result.structured_content
    assert structured["status"] == "ok"
    assert structured["service"] == "engineering-mcp"


async def test_health_tool_reports_failure() -> None:
    server = create_mcp_server(make_settings(), readiness_checks=[_fail])
    async with Client(server) as client:
        result = await client.call_tool("health", {})
    structured: Any = result.structured_content
    assert structured["status"] == "fail"


def test_http_health_routes_and_correlation() -> None:
    app = create_app(make_settings(), readiness_checks=[_ok])
    with TestClient(app, base_url="http://localhost:8082") as client:
        live = client.get("/health/live", headers={"X-Correlation-ID": "mcp-req-00001"})
        ready = client.get("/health/ready")
    assert live.status_code == ready.status_code == 200
    assert live.headers["x-correlation-id"] == "mcp-req-00001"
    assert ready.json()["service"] == "engineering-mcp"


def test_http_ready_503_on_failure() -> None:
    app = create_app(make_settings(), readiness_checks=[_fail])
    with TestClient(app, base_url="http://localhost:8082") as client:
        assert client.get("/health/ready").status_code == 503


def test_dns_rebinding_protection_rejects_unknown_hosts() -> None:
    app = create_app(make_settings(), readiness_checks=[_ok])
    with TestClient(app, base_url="http://evil.example.com:8082") as client:
        response = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
            headers={"Accept": "application/json, text/event-stream"},
        )
    assert response.status_code == 421


@pytest.mark.parametrize("host", ["mcp", "localhost", "127.0.0.1"])
def test_known_hosts_are_accepted_by_the_mcp_endpoint(host: str) -> None:
    app = create_app(make_settings(), readiness_checks=[_ok])
    with TestClient(app, base_url=f"http://{host}:8082") as client:
        response = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
            headers={"Accept": "application/json, text/event-stream"},
        )
    assert response.status_code != 421


def test_optional_bearer_token_guards_the_mcp_endpoint_but_not_health() -> None:
    app = create_app(make_settings(mcp_token="mcp-secret-token"), readiness_checks=[_ok])
    with TestClient(app, base_url="http://localhost:8082") as client:
        assert client.get("/health/live").status_code == 200
        body = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
        accept = {"Accept": "application/json, text/event-stream"}
        assert client.post("/mcp", json=body, headers=accept).status_code == 401
        wrong = {**accept, "Authorization": "Bearer nope"}
        assert client.post("/mcp", json=body, headers=wrong).status_code == 401
        right = {**accept, "Authorization": "Bearer mcp-secret-token"}
        assert client.post("/mcp", json=body, headers=right).status_code != 401
