from __future__ import annotations

import asyncio

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine

from eios_jobs import Job, JobRunner, PermanentJobError, PostgresJobQueue

pytestmark = pytest.mark.integration


async def test_enqueue_claim_complete(db: AsyncEngine) -> None:
    q = PostgresJobQueue(db)
    job = await q.enqueue("t", {"n": 1})
    assert job.status == "queued" and job.attempts == 0
    claimed = await q.claim("w1", types=["t"], lease_seconds=30)
    assert claimed is not None and claimed.id == job.id
    assert claimed.status == "running" and claimed.attempts == 1 and claimed.lease_owner == "w1"
    assert await q.claim("w2", types=["t"], lease_seconds=30) is None  # nothing left
    assert await q.complete(job.id, "w1", {"ok": True})
    done = await q.get(job.id)
    assert done is not None and done.status == "succeeded" and done.result == {"ok": True}
    assert done.lease_owner is None


async def test_claim_filters_by_type_and_availability(db: AsyncEngine) -> None:
    q = PostgresJobQueue(db)
    await q.enqueue("a")
    await q.enqueue("later", delay_seconds=3600)
    assert await q.claim("w", types=["b"], lease_seconds=30) is None
    assert await q.claim("w", types=["later"], lease_seconds=30) is None
    job = await q.claim("w", types=["a", "later"], lease_seconds=30)
    assert job is not None and job.type == "a"


async def test_idempotency_key_dedupes(db: AsyncEngine) -> None:
    q = PostgresJobQueue(db)
    a = await q.enqueue("sync", {"p": 1}, idempotency_key="proj-1")
    b = await q.enqueue("sync", {"p": 2}, idempotency_key="proj-1")
    c = await q.enqueue("other", idempotency_key="proj-1")  # different type: independent
    assert a.id == b.id and c.id != a.id
    assert len(await q.list_jobs()) == 2


async def test_concurrent_claims_never_hand_out_the_same_job(db: AsyncEngine) -> None:
    q = PostgresJobQueue(db)
    for i in range(20):
        await q.enqueue("t", {"i": i})
    results = await asyncio.gather(
        *(q.claim(f"w{i}", types=["t"], lease_seconds=30) for i in range(40))
    )
    claimed = [j.id for j in results if j is not None]
    assert len(claimed) == 20 and len(set(claimed)) == 20


async def test_failure_retries_then_dies(db: AsyncEngine) -> None:
    q = PostgresJobQueue(db)
    job = await q.enqueue("t", max_attempts=2)
    for attempt, expected in ((1, "queued"), (2, "dead")):
        claimed = await q.claim("w", types=["t"], lease_seconds=30)
        assert claimed is not None and claimed.attempts == attempt
        failed = await q.fail(job.id, "w", f"boom {attempt}")
        assert failed is not None and failed.status == expected
    assert await q.claim("w", types=["t"], lease_seconds=30) is None
    dead = await q.get(job.id)
    assert dead is not None and dead.error == "boom 2"


async def test_permanent_failure_skips_retries(db: AsyncEngine) -> None:
    q = PostgresJobQueue(db)
    job = await q.enqueue("t", max_attempts=5)
    await q.claim("w", types=["t"], lease_seconds=30)
    failed = await q.fail(job.id, "w", "unrecoverable", permanent=True)
    assert failed is not None and failed.status == "dead"


async def test_retry_delay_is_respected(db: AsyncEngine) -> None:
    q = PostgresJobQueue(db)
    job = await q.enqueue("t")
    await q.claim("w", types=["t"], lease_seconds=30)
    await q.fail(job.id, "w", "x", retry_delay_seconds=3600)
    assert await q.claim("w", types=["t"], lease_seconds=30) is None


async def test_expired_lease_is_reclaimed_and_stale_worker_is_fenced(db: AsyncEngine) -> None:
    """Crash safety: a worker that disappears loses its job; it cannot later complete it."""
    q = PostgresJobQueue(db)
    job = await q.enqueue("t", max_attempts=3)
    await q.claim("crashed-worker", types=["t"], lease_seconds=30)
    assert await q.reclaim_expired() == 0  # lease still valid
    async with db.begin() as conn:  # simulate the lease running out
        await conn.execute(sa.text("UPDATE platform.job SET lease_until = now() - interval '1 s'"))
    assert await q.reclaim_expired() == 1
    again = await q.claim("healthy-worker", types=["t"], lease_seconds=30)
    assert again is not None and again.id == job.id and again.attempts == 2
    # the original worker comes back from the dead: every write is rejected
    assert not await q.complete(job.id, "crashed-worker", {"late": True})
    assert await q.fail(job.id, "crashed-worker", "late") is None
    assert not await q.heartbeat(job.id, "crashed-worker", 30)
    assert await q.complete(job.id, "healthy-worker", {"ok": True})
    final = await q.get(job.id)
    assert final is not None and final.status == "succeeded"


async def test_lost_job_dies_when_attempts_are_exhausted(db: AsyncEngine) -> None:
    q = PostgresJobQueue(db)
    job = await q.enqueue("t", max_attempts=1)
    await q.claim("w", types=["t"], lease_seconds=30)
    async with db.begin() as conn:
        await conn.execute(sa.text("UPDATE platform.job SET lease_until = now() - interval '1 s'"))
    await q.reclaim_expired()
    dead = await q.get(job.id)
    assert dead is not None and dead.status == "dead"


async def test_heartbeat_extends_lease(db: AsyncEngine) -> None:
    q = PostgresJobQueue(db)
    job = await q.enqueue("t")
    claimed = await q.claim("w", types=["t"], lease_seconds=1)
    assert claimed is not None and claimed.lease_until is not None
    assert await q.heartbeat(job.id, "w", 600)
    after = await q.get(job.id)
    assert after is not None and after.lease_until is not None
    assert after.lease_until > claimed.lease_until


async def test_secrets_in_payload_are_redacted(db: AsyncEngine) -> None:
    q = PostgresJobQueue(db)
    job = await q.enqueue("t", {"api_key": "sk-1", "url": "postgresql://u:pw@h/db"})
    assert job.payload["api_key"] == "[REDACTED]" and "pw" not in str(job.payload)


async def test_runner_executes_retries_and_dead_letters(db: AsyncEngine) -> None:
    q = PostgresJobQueue(db)
    calls: list[str] = []

    async def ok(job: Job) -> dict[str, object]:
        calls.append(f"ok:{job.payload['n']}")
        return {"n": job.payload["n"]}

    attempts = {"flaky": 0}

    async def flaky(job: Job) -> None:
        attempts["flaky"] += 1
        if attempts["flaky"] < 2:
            raise RuntimeError("transient")

    async def hopeless(job: Job) -> None:
        raise PermanentJobError("cannot ever work")

    runner = JobRunner(
        q, {"ok": ok, "flaky": flaky, "hopeless": hopeless}, lease_seconds=5, base_retry_seconds=0
    )
    good = await q.enqueue("ok", {"n": 7})
    flaky_job = await q.enqueue("flaky", max_attempts=3)
    bad = await q.enqueue("hopeless", max_attempts=5)
    for _ in range(6):
        await runner.run_once()
    assert (await q.get(good.id)).status == "succeeded"  # type: ignore[union-attr]
    assert (await q.get(flaky_job.id)).status == "succeeded" and attempts["flaky"] == 2  # type: ignore[union-attr]
    dead = await q.get(bad.id)
    assert dead is not None and dead.status == "dead" and dead.attempts == 1
    assert calls == ["ok:7"]
    assert not await runner.run_once()  # queue drained


async def test_runner_heartbeat_keeps_long_job_alive(db: AsyncEngine) -> None:
    q = PostgresJobQueue(db)

    async def slow(job: Job) -> dict[str, object]:
        await asyncio.sleep(1.2)  # longer than the 0.6s lease
        return {}

    runner = JobRunner(q, {"slow": slow}, lease_seconds=0.6)
    job = await q.enqueue("slow")
    task = asyncio.create_task(runner.run_once())
    await asyncio.sleep(0.9)
    assert await q.reclaim_expired() == 0  # heartbeat renewed the lease
    await task
    done = await q.get(job.id)
    assert done is not None and done.status == "succeeded" and done.attempts == 1
