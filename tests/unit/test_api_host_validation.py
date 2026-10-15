"""DNS-rebinding protection: the API answers only to the loopback names it is configured with."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from eios_api.app import create_app
from eios_core.health import ComponentHealth
from tests.conftest import make_settings


async def _ok() -> ComponentHealth:
    return ComponentHealth(name="database", status="ok")


def _client(host: str, **settings: object) -> TestClient:
    app = create_app(make_settings(**settings), readiness_checks=[_ok])
    return TestClient(app, base_url=f"http://{host}")


@pytest.mark.parametrize(
    ("host", "status"),
    [
        ("127.0.0.1:8000", 200),
        ("127.0.0.1:8100", 200),  # a shifted host port keeps working: only the name is checked
        ("localhost:8000", 200),
        ("api:8000", 200),  # the web proxy and other containers
        ("evil.example", 400),
        ("evil.example:8000", 400),
        ("127.0.0.1.evil.example:8000", 400),
        ("localhost.evil.example", 400),
    ],
)
def test_requests_naming_other_hosts_are_refused(host: str, status: int) -> None:
    with _client(host) as client:
        assert client.get("/health/live").status_code == status


def test_the_allowed_names_come_from_settings() -> None:
    with _client("tools.internal", api_allowed_hosts=["tools.internal"]) as client:
        assert client.get("/health/live").status_code == 200
    with _client("127.0.0.1", api_allowed_hosts=["tools.internal"]) as client:
        assert client.get("/health/live").status_code == 400


def test_a_cross_origin_page_cannot_send_json_or_forms_without_a_preflight() -> None:
    """Guards the assumption the host check relies on: no route accepts a simple cross-origin
    POST (text/plain or form bodies), and no CORS headers are ever sent."""
    with _client("127.0.0.1:8000") as client:
        for content_type, body in (
            ("text/plain", '{"action": "x"}'),
            ("application/x-www-form-urlencoded", "action=x"),
        ):
            response = client.post(
                "/policy/evaluate",
                content=body,
                headers={"content-type": content_type, "origin": "http://evil.example"},
            )
            assert response.status_code == 422
            assert "access-control-allow-origin" not in response.headers
