from __future__ import annotations

import json
import logging

import pytest

from eios_core.correlation import correlation_scope
from eios_core.logging import configure_logging, get_logger


def _records(capsys: pytest.CaptureFixture[str]) -> list[dict[str, object]]:
    out = capsys.readouterr().out
    return [json.loads(line) for line in out.splitlines() if line.strip()]


def test_emits_json_with_standard_fields(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging(service="svc", environment="test")
    get_logger("t").info("something_happened", answer=42)
    (record,) = _records(capsys)
    assert record["event"] == "something_happened"
    assert record["answer"] == 42
    assert record["service"] == "svc"
    assert record["environment"] == "test"
    assert record["level"] == "info"
    assert str(record["timestamp"]).endswith("Z")


def test_includes_correlation_id_only_inside_scope(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging(service="svc", environment="test")
    log = get_logger("t")
    log.info("outside")
    with correlation_scope("abcd-1234-efgh") as cid:
        log.info("inside")
    outside, inside = _records(capsys)
    assert "correlation_id" not in outside
    assert inside["correlation_id"] == cid


def test_stdlib_loggers_are_rendered_as_json_too(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging(service="svc", environment="test")
    with correlation_scope("abcd-1234-efgh"):
        logging.getLogger("alembic.runtime.migration").info("Running upgrade")
    (record,) = _records(capsys)
    assert record["event"] == "Running upgrade"
    assert record["correlation_id"] == "abcd-1234-efgh"


def test_level_filtering(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging(service="svc", environment="test", level="WARNING")
    get_logger("t").info("hidden")
    get_logger("t").warning("shown")
    assert [r["event"] for r in _records(capsys)] == ["shown"]


def test_configure_is_idempotent(capsys: pytest.CaptureFixture[str]) -> None:
    for _ in range(3):
        configure_logging(service="svc", environment="test")
    get_logger("t").info("once")
    assert len(_records(capsys)) == 1


def test_exceptions_are_serialised(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging(service="svc", environment="test")
    try:
        raise RuntimeError("boom")
    except RuntimeError:
        get_logger("t").exception("failed")
    (record,) = _records(capsys)
    assert "RuntimeError: boom" in str(record["exception"])
