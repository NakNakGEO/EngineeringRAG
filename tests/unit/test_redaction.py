from __future__ import annotations

from eios_observability.redaction import REDACTED, redact, redact_text


def test_sensitive_keys_are_masked_at_any_depth() -> None:
    data = {
        "ok": 1,
        "password": "p",
        "nested": {"API_KEY": "k", "list": [{"Authorization": "Bearer x", "fine": "y"}]},
        "db_secret": "s",
    }
    out = redact(data)
    assert out["password"] == REDACTED
    assert out["nested"]["API_KEY"] == REDACTED
    assert out["nested"]["list"][0] == {"Authorization": REDACTED, "fine": "y"}
    assert out["db_secret"] == REDACTED
    assert out["ok"] == 1


def test_url_credentials_are_masked_in_strings() -> None:
    text = "connect postgresql+psycopg://eios:hunter2@postgres:5432/eios now"
    assert "hunter2" not in redact_text(text)
    assert "eios:" in redact_text(text) and "@postgres" in redact_text(text)


def test_input_is_not_mutated() -> None:
    original = {"password": "p", "x": ["mssql://u:pw@h/db"]}
    redact(original)
    assert original == {"password": "p", "x": ["mssql://u:pw@h/db"]}


def test_excessive_depth_is_cut_off() -> None:
    deep: dict[str, object] = {}
    cur = deep
    for _ in range(40):
        nxt: dict[str, object] = {}
        cur["a"] = nxt
        cur = nxt
    assert REDACTED in str(redact(deep))
