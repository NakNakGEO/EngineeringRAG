from __future__ import annotations

import pytest

from eios_core.correlation import (
    correlation_scope,
    get_correlation_id,
    new_correlation_id,
    sanitize_correlation_id,
)


def test_generated_ids_are_unique_and_valid() -> None:
    a, b = new_correlation_id(), new_correlation_id()
    assert a != b
    assert sanitize_correlation_id(a) == a


@pytest.mark.parametrize(
    "value",
    ["short", "has space in it 123", "line\nbreak-123456", "x" * 129, "ünïcode-12345678", "", None],
)
def test_untrusted_ids_are_rejected(value: str | None) -> None:
    assert sanitize_correlation_id(value) is None


def test_valid_inbound_id_is_kept() -> None:
    assert sanitize_correlation_id("req-1234.abcd_EFGH") == "req-1234.abcd_EFGH"


def test_scope_binds_and_restores() -> None:
    assert get_correlation_id() is None
    with correlation_scope("outer-12345678") as outer:
        assert get_correlation_id() == outer == "outer-12345678"
        with correlation_scope() as inner:
            assert get_correlation_id() == inner != outer
        assert get_correlation_id() == outer
    assert get_correlation_id() is None


def test_scope_replaces_invalid_id() -> None:
    with correlation_scope("bad id") as cid:
        assert cid != "bad id"
