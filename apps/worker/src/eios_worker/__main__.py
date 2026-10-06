"""``python -m eios_worker``: run the worker."""

from __future__ import annotations

import asyncio

from eios_core.logging import configure_logging
from eios_core.settings import Settings, get_settings
from eios_jobs import JobRunner
from eios_project_intelligence import SYNC_JOB
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
    runner = JobRunner(container.queue, {SYNC_JOB: container.projects.handle_sync_job})
    worker = Worker(settings, engine=engine, job_runner=runner)
    try:
        await run_until_signalled(worker)
    finally:
        await container.background.shutdown()
        await engine.dispose()


if __name__ == "__main__":
    main()
