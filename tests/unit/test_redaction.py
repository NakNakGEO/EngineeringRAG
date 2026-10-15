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


def test_telemetry_and_flags_are_not_mistaken_for_secrets() -> None:
    usage = {
        "passed": True,
        "bypass": 1,
        "input_tokens": 120,
        "output_tokens": 45,
        "tokens": 7,
        "token_budget": 12_000,
        "max_tokens": 512,
        "tokens_used": 3,
        "tokenizer": "hashing-v1",
        "secretary": "Ada",
    }
    assert redact(usage) == usage  # nothing here is a credential


def test_real_credentials_are_masked_whatever_their_spelling() -> None:
    secrets = {
        "password": "p",
        "user_password": "p",
        "passwd": "p",
        "passphrase": "p",
        "client_secret": "s",
        "SECRET": "s",
        "api_key": "k",
        "apiKey": "k",
        "X-API-KEY": "k",
        "private_key": "k",
        "access_key": "k",
        "Authorization": "Bearer x",
        "cookie": "c",
        "credentials": {"user": "u"},
        "token": "abc",
        "access_token": "abc",
        "refreshToken": "abc",
        "id_token": 123456,
        "tokens": ["abc", "def"],
    }
    out = redact(secrets)
    assert all(v == REDACTED for v in out.values()), out
    # nested, inside lists, under telemetry-looking parents
    nested = redact({"usage": {"input_tokens": 5, "auth": {"access_token": "x"}}})
    assert nested == {"usage": {"input_tokens": 5, "auth": {"access_token": REDACTED}}}
