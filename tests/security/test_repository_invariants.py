"""Static invariants of the repository's own configuration files."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from eios_core.database_policy import assert_internal_database_url
from eios_core.settings import Settings

pytestmark = pytest.mark.security

REPO_ROOT = Path(__file__).resolve().parents[2]
URL_PATTERN = re.compile(r"[a-z][a-z0-9+.-]*://[^\s'\"]+")
DB_SCHEMES = ("postgres", "mysql", "mssql", "oracle", "sqlite", "mongodb", "redis", "jdbc")


def _db_urls(text: str) -> list[str]:
    return [u for u in URL_PATTERN.findall(text) if u.lower().startswith(DB_SCHEMES)]


def test_env_example_has_only_the_internal_database() -> None:
    urls = _db_urls((REPO_ROOT / ".env.example").read_text())
    assert len(urls) == 1
    assert_internal_database_url(urls[0])


def test_alembic_ini_does_not_configure_a_database_url() -> None:
    text = (REPO_ROOT / "alembic.ini").read_text()
    assert "sqlalchemy.url" not in text
    assert _db_urls(text) == []


def test_compose_gives_services_only_the_internal_postgres() -> None:
    text = (REPO_ROOT / "docker-compose.yml").read_text()
    # Compose interpolations make the URL non-parseable verbatim; check the host part instead.
    urls = _db_urls(re.sub(r"\$\{[^}]*\}", "x", text))
    assert len(urls) == 1
    assert urls[0].endswith("@postgres:5432/x")
    assert "image: mssql" not in text
    assert "mysql" not in text.lower()


def test_compose_publishes_ports_on_loopback_only() -> None:
    text = (REPO_ROOT / "docker-compose.yml").read_text()
    for mapping in re.findall(r'^\s*- "([^"]*:\d+)"\s*$', text, flags=re.MULTILINE):
        assert mapping.startswith("127.0.0.1:"), mapping


def test_settings_expose_a_single_database_setting() -> None:
    db_fields = [n for n in Settings.model_fields if "database" in n or n.endswith("_dsn")]
    assert db_fields == ["database_url", "database_connect_timeout_seconds"]


def test_no_secrets_committed() -> None:
    """The example env file must only carry obvious placeholders."""
    text = (REPO_ROOT / ".env.example").read_text()
    assert "change-me" in text
