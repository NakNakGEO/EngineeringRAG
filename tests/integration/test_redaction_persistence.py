"""What reaches the database: secrets masked, telemetry (pass flags, token counts) intact."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from eios_domain.events import EventType
from eios_observability.redaction import REDACTED
from eios_runtime import Container, build_container
from tests.conftest import make_settings

pytestmark = pytest.mark.integration


@pytest.fixture
async def container(db: AsyncEngine, tmp_path: Path) -> Container:
    return build_container(make_settings(blob_dir=tmp_path / "blobs"), db)


async def test_events_keep_telemetry_and_mask_credentials(container: Container) -> None:
    run = await container.recorder.start_run(kind="redaction_check")
    await run.emit(
        EventType.CONTEXT_REQUESTED,
        "verification gate",
        data={
            "passed": True,
            "input_tokens": 120,
            "output_tokens": 45,
            "token_budget": 12_000,
            "password": "hunter2",
            "access_token": "tok-123",
            "note": "connect postgresql://eios:hunter2@db/eios",
        },
    )
    events = (await container.events.list_events(run.run_id, limit=50)).items
    data = next(e for e in events if e.summary == "verification gate").data
    assert data["passed"] is True and data["input_tokens"] == 120
    assert data["output_tokens"] == 45 and data["token_budget"] == 12_000
    assert data["password"] == REDACTED and data["access_token"] == REDACTED
    assert "hunter2" not in str(data)


async def test_job_payloads_and_results_mask_credentials_but_keep_counts(
    container: Container,
) -> None:
    job = await container.queue.enqueue(
        "noop", {"api_key": "k", "input_tokens": 9, "passed": False}
    )
    claimed = await container.queue.claim("w1", types=["noop"], lease_seconds=30)
    assert claimed is not None and claimed.id == job.id
    await container.queue.complete(job.id, "w1", {"token": "t", "output_tokens": 4, "passed": True})
    stored = await container.queue.get(job.id)
    assert stored is not None
    assert stored.payload == {"api_key": REDACTED, "input_tokens": 9, "passed": False}
    assert stored.result == {"token": REDACTED, "output_tokens": 4, "passed": True}
