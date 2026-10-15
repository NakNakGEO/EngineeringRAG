"""Correlation IDs: one identifier that follows a unit of work through logs and responses."""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

CORRELATION_ID_HEADER = "X-Correlation-ID"

# Inbound IDs are untrusted: restrict the alphabet and length so they cannot inject log lines,
# headers or oversized values.
_VALID_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-]{7,127}$")

_correlation_id: ContextVar[str | None] = ContextVar("eios_correlation_id", default=None)


def new_correlation_id() -> str:
    return str(uuid.uuid4())


def sanitize_correlation_id(value: str | None) -> str | None:
    """Return ``value`` if it is an acceptable correlation ID, otherwise ``None``."""
    if value is not None and _VALID_ID.fullmatch(value):
        return value
    return None


def get_correlation_id() -> str | None:
    return _correlation_id.get()


@contextmanager
def correlation_scope(correlation_id: str | None = None) -> Iterator[str]:
    """Bind a correlation ID for the duration of the ``with`` block (generated if not given)."""
    resolved = sanitize_correlation_id(correlation_id) or new_correlation_id()
    token = _correlation_id.set(resolved)
    try:
        yield resolved
    finally:
        _correlation_id.reset(token)
