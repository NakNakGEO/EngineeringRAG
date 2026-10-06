"""Alembic runs against the Engineering OS PostgreSQL (a throwaway database)."""

from __future__ import annotations

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text

from eios_core.settings import Settings
from tests.conftest import make_settings

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]


def _config(settings: Settings) -> Config:
    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    cfg.attributes["settings"] = settings
    return cfg


def _version(url: str) -> str | None:
    engine = create_engine(url)
    try:
        with engine.connect() as connection:
            if not inspect(connection).has_table("alembic_version"):
                return None
            return connection.execute(text("SELECT version_num FROM alembic_version")).scalar()
    finally:
        engine.dispose()


def test_single_head_and_linear_history() -> None:
    script = ScriptDirectory(str(REPO_ROOT / "migrations"))
    assert script.get_heads() == ["0001"]


def test_upgrade_then_downgrade_roundtrip(test_database_url: str) -> None:
    cfg = _config(make_settings(database_url=test_database_url))
    assert _version(test_database_url) is None

    command.upgrade(cfg, "head")
    assert _version(test_database_url) == "0001"

    command.upgrade(cfg, "head")  # idempotent
    assert _version(test_database_url) == "0001"

    command.downgrade(cfg, "base")
    assert _version(test_database_url) is None

    command.upgrade(cfg, "head")
    assert _version(test_database_url) == "0001"


def test_offline_sql_generation_does_not_connect(capsys: pytest.CaptureFixture[str]) -> None:
    # Port 1 on localhost: any real connection attempt would fail.
    settings = make_settings(database_url="postgresql+psycopg://u:p@localhost:1/none")
    command.upgrade(_config(settings), "head", sql=True)
    assert "alembic_version" in capsys.readouterr().out


def test_migrations_refuse_an_external_database() -> None:
    from pydantic import SecretStr

    from eios_core.database_policy import ExternalDatabaseForbiddenError

    bypassed = Settings.model_construct(
        database_url=SecretStr("mssql+pyodbc://sa:pw@sqlserver.corp.local/qa"),
        database_connect_timeout_seconds=1.0,
        environment="test",
        log_level="WARNING",
        log_json=True,
    )
    with pytest.raises(ExternalDatabaseForbiddenError):
        command.upgrade(_config(bypassed), "head")
