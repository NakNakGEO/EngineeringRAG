"""Alembic environment for the Engineering OS database.

All connections go through ``eios_storage.build_sync_engine``, which enforces the external-database
policy. Tests may inject settings via ``config.attributes["settings"]``.
"""

from __future__ import annotations

from alembic import context

from eios_core.logging import configure_logging
from eios_core.settings import Settings, get_settings
from eios_storage import build_sync_engine, metadata

config = context.config
settings: Settings = config.attributes.get("settings") or get_settings()

configure_logging(
    service="engineering-migrations",
    environment=settings.environment,
    level=settings.log_level,
    json_logs=settings.log_json,
)

target_metadata = metadata


def _include_object(
    _object: object, name: str | None, type_: str, reflected: bool, _compare_to: object
) -> bool:
    # alembic_version lives in the default schema and is not part of our metadata.
    return not (type_ == "table" and reflected and name == "alembic_version")


def run_migrations_offline() -> None:
    """Emit SQL to stdout without connecting (useful for reviewing a migration)."""
    context.configure(
        url=settings.database_url.get_secret_value(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        include_schemas=True,
        include_object=_include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = build_sync_engine(settings)
    with engine.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            include_schemas=True,
            include_object=_include_object,
        )
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
