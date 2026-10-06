"""Integration fixtures: an API served by a real uvicorn instance (needed for SSE)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import httpx
import pytest
import uvicorn
from sqlalchemy.ext.asyncio import AsyncEngine

from eios_api.app import create_app
from tests.conftest import make_settings


@pytest.fixture
async def live_api(db: AsyncEngine, migrated_database_url: str) -> AsyncIterator[str]:
    """Base URL of the real API bound to an ephemeral loopback port."""
    settings = make_settings(
        database_url=migrated_database_url,
        sse_poll_interval_seconds=0.05,
        sse_keepalive_seconds=0.3,
    )
    app = create_app(settings)
    config = uvicorn.Config(
        app, host="127.0.0.1", port=0, log_config=None, access_log=False, lifespan="on"
    )
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    for _ in range(200):
        if server.started:
            break
        await asyncio.sleep(0.01)
    else:  # pragma: no cover
        raise RuntimeError("API did not start")
    port = server.servers[0].sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, timeout=10)


@pytest.fixture
async def client(live_api: str) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(base_url=live_api, timeout=15) as http:
        yield http
