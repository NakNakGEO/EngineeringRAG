"""``python -m eios_worker``: run the worker."""

from __future__ import annotations

import asyncio

from eios_core.logging import configure_logging
from eios_core.settings import get_settings
from eios_worker.worker import SERVICE_NAME, Worker, run_until_signalled


def main() -> None:
    settings = get_settings()
    configure_logging(
        service=SERVICE_NAME,
        environment=settings.environment,
        level=settings.log_level,
        json_logs=settings.log_json,
    )
    asyncio.run(run_until_signalled(Worker(settings)))


if __name__ == "__main__":
    main()
