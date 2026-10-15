"""Hand-written migrations must produce exactly the schema the table definitions describe."""

from __future__ import annotations

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import create_engine

from eios_storage import metadata

pytestmark = pytest.mark.integration


def _include_object(
    _obj: object, name: str | None, type_: str, reflected: bool, _cmp: object
) -> bool:
    return not (type_ == "table" and reflected and name == "alembic_version")


def _diff(url: str) -> list[object]:
    engine = create_engine(url)
    try:
        with engine.connect() as connection:
            context = MigrationContext.configure(
                connection,
                opts={
                    "compare_type": True,
                    "include_schemas": True,
                    "include_object": _include_object,
                },
            )
            return list(compare_metadata(context, metadata))
    finally:
        engine.dispose()


def test_migrations_match_metadata(migrated_database_url: str) -> None:
    diff = _diff(migrated_database_url)
    assert diff == [], f"migrations drifted from table definitions: {diff}"


def test_drift_detector_actually_detects_drift(fresh_database_url: str) -> None:
    from alembic import command

    from tests.conftest import alembic_config

    command.upgrade(alembic_config(fresh_database_url), "head")
    engine = create_engine(fresh_database_url)
    with engine.begin() as connection:
        connection.exec_driver_sql("ALTER TABLE platform.run ADD COLUMN sneaky text")
    engine.dispose()
    assert _diff(fresh_database_url) != []
