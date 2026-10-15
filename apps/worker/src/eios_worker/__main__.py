"""``python -m eios_worker``: run the worker."""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

from eios_core.logging import configure_logging
from eios_core.settings import Settings, get_settings
from eios_governance.maintenance import MAINTAIN_JOB
from eios_governance.retention import RETENTION_JOB
from eios_jobs import Job, JobRunner
from eios_project_intelligence import SYNC_JOB
from eios_project_intelligence.watcher import ProjectWatcher
from eios_runtime import build_container
from eios_storage import build_async_engine
from eios_worker.worker import SERVICE_NAME, Worker, run_until_signalled


def main() -> None:
    settings = get_settings()
    configure_logging(
        service=SERVICE_NAME,
        environment=settings.environment,
        level=settings.log_level,
        json_logs=settings.log_json,
    )
    asyncio.run(_run(settings))


async def _run(settings: Settings) -> None:
    engine = build_async_engine(settings)
    container = build_container(settings, engine)

    async def handle_maintenance(job: Job) -> dict[str, Any]:
        raw = job.payload.get("project_id")
        async with container.recorder.run(kind="maintenance", goal="knowledge maintenance") as ctx:
            report = await container.maintenance.run(uuid.UUID(raw) if raw else None, ctx)
        return dict(report.__dict__)

    async def handle_retention(job: Job) -> dict[str, Any]:
        return dict((await container.retention.run()).deleted)

    runner = JobRunner(
        container.queue,
        {
            SYNC_JOB: container.projects.handle_sync_job,
            MAINTAIN_JOB: handle_maintenance,
            RETENTION_JOB: handle_retention,
        },
    )
    worker = Worker(settings, engine=engine, job_runner=runner)
    watcher = ProjectWatcher(
        container.projects,
        interval=settings.project_watch_interval_seconds,
        min_gap=settings.project_watch_min_gap_seconds,
    )
    watch_stop = asyncio.Event()
    watch_task = asyncio.create_task(watcher.run_forever(watch_stop))
    try:
        await run_until_signalled(worker)
    finally:
        watch_stop.set()
        await asyncio.gather(watch_task, return_exceptions=True)
        await container.background.shutdown()
        await engine.dispose()


if __name__ == "__main__":
    main()
