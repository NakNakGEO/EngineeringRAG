"""Secret redaction applied to every event before it is persisted or streamed."""

from __future__ import annotations

import re
from typing import Any

REDACTED = "[REDACTED]"
# camelCase / snake_case / kebab-case keys are compared as whole words, so ``passed`` (a flag),
# ``bypass`` or ``secretary`` are not mistaken for ``pass`` or ``secret``.
_WORDS = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|\d+")
_SECRET_WORDS = frozenset(
    {
        "password", "passwd", "passphrase", "pwd", "pass", "secret", "secrets", "credential",
        "credentials", "authorization", "cookie", "cookies",
    }
)  # fmt: skip
# written as one word or two: api_key / apiKey / APIKEY, private_key, access_key
_SECRET_COMPOUNDS = ("apikey", "privatekey", "accesskey")
# a token *count* or limit is telemetry (input_tokens, token_budget), not a credential
_QUANTITY_WORDS = frozenset(
    {"budget", "count", "limit", "usage", "total", "used", "max", "min", "estimate", "ttl"}
)
# scheme://user:password@host  ->  scheme://user:[REDACTED]@host
_URL_CREDENTIALS = re.compile(r"([a-zA-Z][a-zA-Z0-9+.\-]*://[^:/\s@]+:)[^@/\s]+(@)")
_MAX_DEPTH = 12


def redact_text(value: str) -> str:
    return _URL_CREDENTIALS.sub(rf"\1{REDACTED}\2", value)


def is_sensitive_key(key: str, value: Any = None) -> bool:
    """Whether the value stored under ``key`` must be masked.

    A key naming a secret is always masked. ``token`` is a credential unless the name says it is
    a quantity (``token_budget``, ``max_tokens``); the plural ``tokens`` is telemetry when its
    value is a number (``input_tokens: 12``) and a list of credentials otherwise.
    """
    words = [w.lower() for w in _WORDS.findall(key)]
    if _SECRET_WORDS.intersection(words) or any(c in "".join(words) for c in _SECRET_COMPOUNDS):
        return True
    if "token" in words and not _QUANTITY_WORDS.intersection(words):
        return True  # access_token, token, id_token: a credential whatever its type
    if "tokens" in words and not _QUANTITY_WORDS.intersection(words):
        return not isinstance(value, bool | int | float)  # input_tokens: 12 is telemetry
    return False


def redact(value: Any, _depth: int = 0) -> Any:
    """Return a deep copy of ``value`` with secrets masked (keys by name, URLs by pattern)."""
    if _depth > _MAX_DEPTH:
        return REDACTED
    if isinstance(value, dict):
        return {
            str(k): (REDACTED if is_sensitive_key(str(k), v) else redact(v, _depth + 1))
            for k, v in value.items()
        }
    if isinstance(value, list | tuple | set | frozenset):
        return [redact(v, _depth + 1) for v in value]
    if isinstance(value, str):
        return redact_text(value)
    return value
