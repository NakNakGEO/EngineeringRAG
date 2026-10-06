"""Engine factories. Every engine in Engineering OS is created here, behind the database policy."""

from __future__ import annotations

from sqlalchemy import Engine, create_engine
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from eios_core.database_policy import assert_internal_database_url
from eios_core.settings import Settings


def _connect_args(settings: Settings) -> dict[str, int]:
    return {"connect_timeout": max(1, round(settings.database_connect_timeout_seconds))}


def build_async_engine(settings: Settings) -> AsyncEngine:
    """Async engine for the API and worker (psycopg 3 async)."""
    url = assert_internal_database_url(settings.database_url.get_secret_value())
    return create_async_engine(url, pool_pre_ping=True, connect_args=_connect_args(settings))


def build_sync_engine(settings: Settings) -> Engine:
    """Sync engine for Alembic migrations and test setup (same driver, same policy)."""
    url = assert_internal_database_url(settings.database_url.get_secret_value())
    return create_engine(url, pool_pre_ping=True, connect_args=_connect_args(settings))
