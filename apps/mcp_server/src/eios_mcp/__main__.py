"""``python -m eios_mcp``: serve MCP over streamable HTTP."""

from __future__ import annotations

import uvicorn

from eios_core.logging import configure_logging
from eios_core.settings import get_settings
from eios_mcp.server import SERVICE_NAME, create_app


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
        host=settings.mcp_host,
        port=settings.mcp_port,
        log_config=None,
        access_log=False,
    )


if __name__ == "__main__":
    main()
