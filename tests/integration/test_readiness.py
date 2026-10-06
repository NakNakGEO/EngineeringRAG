"""Readiness against a real database and against an unreachable one."""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from eios_api.app import create_app
from eios_storage import build_async_engine, check_database
from eios_worker.worker import Worker
from tests.conftest import make_settings

pytestmark = pytest.mark.integration

DEAD_URL = "postgresql+psycopg://u:p@localhost:1/none"


async def test_check_database_ok(test_database_url: str) -> None:
    engine = build_async_engine(make_settings(database_url=test_database_url))
    try:
        result = await check_database(engine)
    finally:
        await engine.dispose()
    assert result.status == "ok"
    assert result.latency_ms is not None


async def test_check_database_fails_without_leaking_details() -> None:
    engine = build_async_engine(make_settings(database_url=DEAD_URL))
    try:
        result = await check_database(engine, timeout_seconds=2)
    finally:
        await engine.dispose()
    assert result.status == "fail"
    assert result.detail == "database unavailable"
    assert "localhost" not in (result.detail or "")


def test_api_ready_with_real_database(test_database_url: str) -> None:
    app = create_app(make_settings(database_url=test_database_url))
    with TestClient(app) as client:
        response = client.get("/health/ready")
    assert response.status_code == 200
    assert response.json()["checks"][0] == {
        "name": "database",
        "status": "ok",
        "detail": None,
        "latency_ms": pytest.approx(response.json()["checks"][0]["latency_ms"]),
    }


def test_api_ready_is_503_when_database_is_unreachable() -> None:
    app = create_app(make_settings(database_url=DEAD_URL, database_connect_timeout_seconds=1))
    with TestClient(app) as client:
        ready = client.get("/health/ready")
        live = client.get("/health/live")
    assert ready.status_code == 503
    assert live.status_code == 200
    assert "localhost" not in ready.text
    assert "u:p" not in ready.text


async def test_worker_ready_with_real_database(test_database_url: str) -> None:
    worker = Worker(make_settings(database_url=test_database_url, worker_heartbeat_seconds=0.05))
    worker._health._port = 0
    task = asyncio.create_task(worker.run())
    try:
        for _ in range(100):
            if worker._health._server is not None:
                break
            await asyncio.sleep(0.01)
        report = await worker.ready_report()
        assert report.status == "ok"
    finally:
        worker.request_stop()
        await asyncio.wait_for(task, timeout=5)
