"""``python -m eios_api``: run the API with uvicorn."""

from __future__ import annotations

import uvicorn

from eios_api.app import SERVICE_NAME, create_app
from eios_core.logging import configure_logging
from eios_core.settings import get_settings


def main() -> None:
    settings = get_settings()
    configure_logging(
        service=SERVICE_NAME,
        environment=settings.environment,
        level=settings.log_level,
        json_logs=settings.log_json,
    )
    uvicorn.run(
        create_app(settings),
        host=settings.api_host,
        port=settings.api_port,
        log_config=None,  # logging is configured by eios_core (structured JSON)
        access_log=False,  # request logs come from CorrelationIdMiddleware
    )


if __name__ == "__main__":
    main()
