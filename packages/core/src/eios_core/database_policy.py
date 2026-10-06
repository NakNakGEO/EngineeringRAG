"""Database target policy: Engineering OS may only talk to its own PostgreSQL.

This is the Phase 0 foundation of the Root Rule "External Database Isolation". Every database URL
used by any component is validated here *before* an engine is created. The policy is
allowlist-based on purpose: anything not explicitly recognised as the internal Engineering OS
PostgreSQL is rejected, including other PostgreSQL servers, SQL Server, MySQL, Oracle, unix
sockets, multi-host URLs and libpq parameters that could redirect the connection.

The allowlist is a module constant. It is deliberately NOT configurable through environment
variables, settings, plugins or any API, so that nothing at runtime can widen it. Changing it
requires a code change, an ADR and review.
"""

from __future__ import annotations

from urllib.parse import parse_qsl, unquote, urlsplit

ALLOWED_DRIVER = "postgresql+psycopg"

# `postgres` is the Docker Compose service name; the others are loopback for local development
# and for tests against the compose-published port.
INTERNAL_DATABASE_HOSTS: frozenset[str] = frozenset({"postgres", "localhost", "127.0.0.1", "::1"})

# Connection parameters that cannot change *where* we connect. Anything else (host, hostaddr,
# service, passfile, target_session_attrs, ...) is rejected.
ALLOWED_QUERY_PARAMETERS: frozenset[str] = frozenset(
    {"sslmode", "connect_timeout", "application_name"}
)


class ExternalDatabaseForbiddenError(ValueError):
    """Raised when a database URL does not point at the internal Engineering OS PostgreSQL."""


def assert_internal_database_url(url: str) -> str:
    """Return ``url`` unchanged if it targets the internal PostgreSQL, else raise.

    The error message never echoes the URL, because it may contain credentials.
    """
    try:
        parts = urlsplit(url)
        hostname = parts.hostname
        port = parts.port  # raises ValueError for malformed ports
    except ValueError as exc:
        raise ExternalDatabaseForbiddenError("database URL is malformed") from exc

    if parts.scheme != ALLOWED_DRIVER:
        raise ExternalDatabaseForbiddenError(
            f"database URL must use the '{ALLOWED_DRIVER}' driver; "
            "other databases and drivers are forbidden by Root Policy"
        )
    if not hostname or hostname.lower() not in INTERNAL_DATABASE_HOSTS:
        raise ExternalDatabaseForbiddenError(
            "database host is not the internal Engineering OS PostgreSQL "
            f"(allowed hosts: {', '.join(sorted(INTERNAL_DATABASE_HOSTS))}); "
            "external databases are forbidden by Root Policy"
        )
    if port is not None and not 0 < port < 65536:
        raise ExternalDatabaseForbiddenError("database port is out of range")
    if not unquote(parts.path).strip("/"):
        raise ExternalDatabaseForbiddenError("database name is required")
    if parts.fragment:
        raise ExternalDatabaseForbiddenError("database URL must not contain a fragment")

    for key, _value in parse_qsl(parts.query, keep_blank_values=True):
        if key not in ALLOWED_QUERY_PARAMETERS:
            raise ExternalDatabaseForbiddenError(f"database URL parameter '{key}' is not allowed")
    return url
