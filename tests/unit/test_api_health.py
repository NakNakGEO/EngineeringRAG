from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from eios_api.app import create_app
from eios_core.health import ComponentHealth
from tests.conftest import make_settings


async def _ok() -> ComponentHealth:
    return ComponentHealth(name="database", status="ok", latency_ms=1.0)


async def _fail() -> ComponentHealth:
    return ComponentHealth(name="database", status="fail", detail="database unavailable")


def _client(*checks) -> TestClient:  # type: ignore[no-untyped-def]
    return TestClient(create_app(make_settings(), readiness_checks=list(checks)))


def test_live_never_touches_dependencies() -> None:
    with _client(_fail) as client:
        response = client.get("/health/live")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["service"] == "engineering-api"
    assert body["checks"] == []


def test_ready_ok() -> None:
    with _client(_ok) as client:
        response = client.get("/health/ready")
    assert response.status_code == 200
    assert response.json()["checks"][0]["name"] == "database"


def test_ready_returns_503_when_a_dependency_fails() -> None:
    with _client(_ok, _fail) as client:
        response = client.get("/health/ready")
    assert response.status_code == 503
    assert response.json()["status"] == "fail"


def test_correlation_id_is_generated() -> None:
    with _client(_ok) as client:
        response = client.get("/health/live")
    assert len(response.headers["x-correlation-id"]) == 36


def test_valid_inbound_correlation_id_is_echoed() -> None:
    with _client(_ok) as client:
        response = client.get("/health/live", headers={"X-Correlation-ID": "client-req-0001"})
    assert response.headers["x-correlation-id"] == "client-req-0001"


def test_invalid_inbound_correlation_id_is_replaced() -> None:
    with _client(_ok) as client:
        response = client.get("/health/live", headers={"X-Correlation-ID": "bad id!"})
    assert response.headers["x-correlation-id"] != "bad id!"
    assert len(response.headers["x-correlation-id"]) == 36


def test_request_log_carries_correlation_id(capsys: pytest.CaptureFixture[str]) -> None:
    with _client(_ok) as client:
        capsys.readouterr()
        response = client.get("/health/live", headers={"X-Correlation-ID": "client-req-0002"})
    lines = [json.loads(x) for x in capsys.readouterr().out.splitlines() if x.startswith("{")]
    request_logs = [x for x in lines if x["event"] == "http_request"]
    assert request_logs, lines
    assert request_logs[-1]["correlation_id"] == "client-req-0002"
    assert request_logs[-1]["status_code"] == response.status_code == 200
    assert request_logs[-1]["service"] == "engineering-api"


def test_unknown_route_is_404_with_correlation_header() -> None:
    with _client(_ok) as client:
        response = client.get("/nope")
    assert response.status_code == 404
    assert "x-correlation-id" in response.headers
