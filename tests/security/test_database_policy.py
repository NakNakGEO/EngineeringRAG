"""Root Policy: external database isolation (runtime half)."""

from __future__ import annotations

import pytest

from eios_core.database_policy import ExternalDatabaseForbiddenError, assert_internal_database_url
from eios_core.settings import Settings
from eios_storage import build_async_engine, build_sync_engine
from tests.conftest import make_settings

pytestmark = pytest.mark.security

ALLOWED = [
    "postgresql+psycopg://u:p@postgres:5432/eios",
    "postgresql+psycopg://u:p@localhost:5432/eios",
    "postgresql+psycopg://u:p@127.0.0.1:55432/eios",
    "postgresql+psycopg://u:p@[::1]:5432/eios",
    "postgresql+psycopg://u:p@POSTGRES/eios",
    "postgresql+psycopg://u:p@localhost/eios?sslmode=prefer&connect_timeout=3",
]

FORBIDDEN = [
    # other database products
    "mssql+pyodbc://sa:pw@sqlserver.corp.local/qa",
    "mysql+pymysql://u:p@db.example.com/app",
    "oracle+oracledb://u:p@ora.corp.local:1521/?service_name=prod",
    "sqlite:///local.db",
    # other / unknown PostgreSQL drivers and bare scheme
    "postgresql://u:p@localhost/eios",
    "postgresql+psycopg2://u:p@localhost/eios",
    "postgresql+asyncpg://u:p@localhost/eios",
    # external PostgreSQL hosts, incl. lookalikes
    "postgresql+psycopg://u:p@prod-db.corp.local:5432/eios",
    "postgresql+psycopg://u:p@10.0.0.5:5432/eios",
    "postgresql+psycopg://u:p@localhost.evil.com/eios",
    "postgresql+psycopg://u:p@postgres.evil.com/eios",
    "postgresql+psycopg://u:p@evil-localhost/eios",
    # userinfo tricks: the real host is after the LAST '@'
    "postgresql+psycopg://u:p@localhost@evil.com/eios",
    "postgresql+psycopg://localhost:5432@evil.com/eios",
    # no host (libpq default / unix socket) and multi-host URLs
    "postgresql+psycopg:///eios",
    "postgresql+psycopg://u:p@/eios",
    "postgresql+psycopg://u:p@localhost,evil.com/eios",
    "postgresql+psycopg://u:p@localhost:5432,evil.com:5432/eios",
    # libpq parameters that redirect the connection
    "postgresql+psycopg://u:p@localhost/eios?host=evil.com",
    "postgresql+psycopg://u:p@localhost/eios?hostaddr=10.1.2.3",
    "postgresql+psycopg://u:p@localhost/eios?service=prod",
    "postgresql+psycopg://u:p@localhost/eios?sslmode=require&host=evil.com",
    "postgresql+psycopg://u:p@localhost/eios?passfile=/etc/x",
    # malformed / degenerate
    "postgresql+psycopg://u:p@localhost:notaport/eios",
    "postgresql+psycopg://u:p@localhost:99999/eios",
    "postgresql+psycopg://u:p@localhost:5432/",
    "postgresql+psycopg://u:p@localhost:5432",
    "postgresql+psycopg://u:p@localhost/eios#frag",
    "",
    "not a url",
    "localhost",
]


@pytest.mark.parametrize("url", ALLOWED)
def test_internal_urls_are_accepted(url: str) -> None:
    assert assert_internal_database_url(url) == url


@pytest.mark.parametrize("url", FORBIDDEN)
def test_everything_else_is_rejected(url: str) -> None:
    with pytest.raises(ExternalDatabaseForbiddenError):
        assert_internal_database_url(url)


@pytest.mark.parametrize("url", FORBIDDEN)
def test_settings_refuse_to_load_external_urls(url: str) -> None:
    with pytest.raises(ValueError):  # noqa: PT011 - pydantic wraps the policy error
        make_settings(database_url=url)


def test_settings_refuse_external_url_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EIOS_DATABASE_URL", "mssql+pyodbc://sa:pw@sqlserver.corp.local/qa")
    with pytest.raises(ValueError):  # noqa: PT011
        Settings(_env_file=None)  # type: ignore[call-arg]


def test_error_never_echoes_credentials() -> None:
    secret = "hunter2-very-secret"
    with pytest.raises(ExternalDatabaseForbiddenError) as info:
        assert_internal_database_url(f"mssql+pyodbc://sa:{secret}@sqlserver.corp.local/qa")
    assert secret not in str(info.value)
    assert "sqlserver.corp.local" not in str(info.value)


def test_url_does_not_leak_through_settings_repr(settings: Settings) -> None:
    assert "unit-test" not in repr(settings)
    assert "unit-test" not in str(settings.model_dump())


@pytest.mark.parametrize("builder", [build_async_engine, build_sync_engine])
def test_engine_factories_enforce_policy_even_if_settings_are_bypassed(builder) -> None:  # type: ignore[no-untyped-def]
    """Defence in depth: a Settings built without validation still cannot yield an engine."""
    from pydantic import SecretStr

    bypassed = Settings.model_construct(
        database_url=SecretStr("mssql+pyodbc://sa:pw@sqlserver.corp.local/qa"),
        database_connect_timeout_seconds=3.0,
    )
    with pytest.raises(ExternalDatabaseForbiddenError):
        builder(bypassed)
