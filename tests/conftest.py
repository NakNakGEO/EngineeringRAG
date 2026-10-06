"""Shared fixtures.

Unit tests never need a database. Integration tests use throwaway databases created on the
Engineering OS PostgreSQL named by ``EIOS_TEST_DATABASE_URL`` (an admin URL, normally the
``postgres`` maintenance database of the compose service). That URL is subject to the same
external-database policy as production code.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from eios_core.database_policy import assert_internal_database_url
from eios_core.settings import Settings

UNIT_DATABASE_URL = "postgresql+psycopg://eios:unit-test@localhost:5432/eios_unit"


def make_settings(**overrides: Any) -> Settings:
    """Settings that ignore the developer's .env and ambient EIOS_* variables."""
    values: dict[str, Any] = {"database_url": UNIT_DATABASE_URL, "log_json": True}
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[call-arg]  # pydantic-settings init arg


@pytest.fixture(autouse=True)
def _isolate_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in list(os.environ):
        if key.startswith("EIOS_") and key != "EIOS_TEST_DATABASE_URL":
            monkeypatch.delenv(key)


@pytest.fixture
def settings() -> Settings:
    return make_settings()


@pytest.fixture(scope="session")
def test_database_url() -> Iterator[str]:
    """URL of a fresh, empty database on the Engineering OS PostgreSQL (dropped afterwards)."""
    admin_url = os.environ.get("EIOS_TEST_DATABASE_URL")
    if not admin_url:
        if os.environ.get("EIOS_REQUIRE_DB_TESTS") == "1":
            pytest.fail("EIOS_REQUIRE_DB_TESTS=1 but EIOS_TEST_DATABASE_URL is not set")
        pytest.skip("EIOS_TEST_DATABASE_URL not set; run `make test` to start the compose DB")
    assert_internal_database_url(admin_url)

    name = f"eios_test_{uuid.uuid4().hex[:12]}"
    admin = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as connection:
        connection.execute(text(f'CREATE DATABASE "{name}"'))
    url = make_url(admin_url).set(database=name).render_as_string(hide_password=False)
    try:
        yield url
    finally:
        with admin.connect() as connection:
            connection.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()
