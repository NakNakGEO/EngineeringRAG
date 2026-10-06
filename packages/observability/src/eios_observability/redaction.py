"""Secret redaction applied to every event before it is persisted or streamed."""

from __future__ import annotations

import re
from typing import Any

REDACTED = "[REDACTED]"
_SENSITIVE_KEY = re.compile(
    r"(pass(word|wd)?|secret|token|api[_-]?key|authorization|credential|private[_-]?key|cookie)",
    re.IGNORECASE,
)
# scheme://user:password@host  ->  scheme://user:[REDACTED]@host
_URL_CREDENTIALS = re.compile(r"([a-zA-Z][a-zA-Z0-9+.\-]*://[^:/\s@]+:)[^@/\s]+(@)")
_MAX_DEPTH = 12


def redact_text(value: str) -> str:
    return _URL_CREDENTIALS.sub(rf"\1{REDACTED}\2", value)


def redact(value: Any, _depth: int = 0) -> Any:
    """Return a deep copy of ``value`` with secrets masked (keys by name, URLs by pattern)."""
    if _depth > _MAX_DEPTH:
        return REDACTED
    if isinstance(value, dict):
        return {
            str(k): (REDACTED if _SENSITIVE_KEY.search(str(k)) else redact(v, _depth + 1))
            for k, v in value.items()
        }
    if isinstance(value, list | tuple | set | frozenset):
        return [redact(v, _depth + 1) for v in value]
    if isinstance(value, str):
        return redact_text(value)
    return value
