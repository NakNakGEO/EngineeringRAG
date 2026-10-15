from __future__ import annotations

import asyncio

import pytest

from eios_core.health import ComponentHealth
from eios_worker.worker import Worker
from tests.conftest import make_settings


async def _ok() -> ComponentHealth:
    return ComponentHealth(name="database", status="ok")


async def _fail() -> ComponentHealth:
    return ComponentHealth(name="database", status="fail", detail="database unavailable")


async def _http(port: int, request: str) -> tuple[int, str]:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(request.encode())
    await writer.drain()
    raw = await reader.read()
    writer.close()
    head, _, body = raw.decode().partition("\r\n\r\n")
    return int(head.split()[1]), body


def _worker(check, **overrides) -> Worker:  # type: ignore[no-untyped-def]
    settings = make_settings(worker_health_port=1, **overrides)
    worker = Worker(settings, database_check=check)
    return worker


@pytest.fixture
async def running(request: pytest.FixtureRequest):  # type: ignore[no-untyped-def]
    check = getattr(request, "param", _ok)
    settings = make_settings(worker_health_port=1, worker_heartbeat_seconds=0.05)
    worker = Worker(settings, database_check=check)
    # bind to an ephemeral port instead of the configured one
    worker._health._port = 0
    task = asyncio.create_task(worker.run())
    for _ in range(100):
        if worker.health_port != 0 and worker._health._server is not None:
            break
        await asyncio.sleep(0.01)
    yield worker
    worker.request_stop()
    await asyncio.wait_for(task, timeout=3)


async def test_live_endpoint(running: Worker) -> None:
    status, body = await _http(running.health_port, "GET /health/live HTTP/1.1\r\n\r\n")
    assert status == 200
    assert '"engineering-worker"' in body


async def test_ready_endpoint_ok(running: Worker) -> None:
    status, body = await _http(running.health_port, "GET /health/ready HTTP/1.1\r\n\r\n")
    assert status == 200
    assert '"heartbeat"' in body


@pytest.mark.parametrize("running", [_fail], indirect=True)
async def test_ready_is_503_when_database_is_down(running: Worker) -> None:
    status, _ = await _http(running.health_port, "GET /health/ready HTTP/1.1\r\n\r\n")
    assert status == 503
    live, _ = await _http(running.health_port, "GET /health/live HTTP/1.1\r\n\r\n")
    assert live == 200


async def test_unknown_path_and_method(running: Worker) -> None:
    assert (await _http(running.health_port, "GET /x HTTP/1.1\r\n\r\n"))[0] == 404
    assert (await _http(running.health_port, "POST /health/live HTTP/1.1\r\n\r\n"))[0] == 405
    assert (await _http(running.health_port, "garbage\r\n\r\n"))[0] == 400


async def test_oversized_request_line_is_rejected(running: Worker) -> None:
    status, _ = await _http(running.health_port, "GET /" + "a" * 5000 + " HTTP/1.1\r\n\r\n")
    assert status == 400


async def test_ready_fails_when_heartbeat_is_stale() -> None:
    worker = Worker(make_settings(worker_heartbeat_seconds=0.01), database_check=_ok)
    await asyncio.sleep(0.1)  # never started, so the heartbeat ages
    report = await worker.ready_report()
    assert report.status == "fail"
    assert {c.name: c.status for c in report.checks} == {"database": "ok", "heartbeat": "fail"}


async def test_graceful_shutdown_stops_the_health_server() -> None:
    worker = Worker(make_settings(worker_heartbeat_seconds=0.05), database_check=_ok)
    worker._health._port = 0
    task = asyncio.create_task(worker.run())
    for _ in range(100):
        if worker._health._server is not None:
            break
        await asyncio.sleep(0.01)
    port = worker.health_port
    worker.request_stop()
    await asyncio.wait_for(task, timeout=3)
    with pytest.raises(ConnectionRefusedError):
        await asyncio.open_connection("127.0.0.1", port)
