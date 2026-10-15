from __future__ import annotations

import pytest
from pydantic import ValidationError

from eios_core.settings import Settings
from tests.conftest import UNIT_DATABASE_URL, make_settings


def test_defaults() -> None:
    s = make_settings()
    assert s.environment == "development"
    assert s.log_level == "INFO"
    assert s.api_port == 8000
    assert s.database_url.get_secret_value() == UNIT_DATABASE_URL


def test_reads_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EIOS_DATABASE_URL", UNIT_DATABASE_URL)
    monkeypatch.setenv("EIOS_LOG_LEVEL", "DEBUG")
    monkeypatch.setenv("EIOS_API_PORT", "9001")
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert (s.log_level, s.api_port) == ("DEBUG", 9001)


def test_database_url_is_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EIOS_DATABASE_URL", raising=False)
    with pytest.raises(ValidationError):
        Settings(_env_file=None)  # type: ignore[call-arg]


@pytest.mark.parametrize(
    ("field", "value"),
    [("api_port", 0), ("api_port", 70000), ("log_level", "LOUD"), ("environment", "qa-prod")],
)
def test_invalid_values_are_rejected(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        make_settings(**{field: value})


def test_settings_are_immutable() -> None:
    s = make_settings()
    with pytest.raises(ValidationError):
        s.log_level = "DEBUG"


def test_unknown_environment_variables_are_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EIOS_SOMETHING_ELSE", "x")
    make_settings()
