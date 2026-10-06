"""Identifiers and time. UUIDs for everything, UTC timestamps only."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime


def new_id() -> uuid.UUID:
    return uuid.uuid4()


def utcnow() -> datetime:
    return datetime.now(UTC)


def ensure_utc(value: datetime) -> datetime:
    """Reject naive datetimes; normalise aware ones to UTC."""
    if value.tzinfo is None:
        raise ValueError("naive datetimes are not allowed; use timezone-aware UTC")
    return value.astimezone(UTC)
