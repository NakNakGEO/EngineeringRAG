"""Shared text utilities: identifier-aware tokenisation used by embedding and retrieval."""

from __future__ import annotations

import re

_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
_WORD = re.compile(r"[A-Za-z0-9]+")


def tokenize(text: str) -> list[str]:
    """Lower-cased tokens; camelCase and snake_case identifiers are split into their parts.

    ``GetUserById`` -> ``get user by id``; the unsplit identifier is kept as well so exact
    identifier mentions still match.
    """
    tokens: list[str] = []
    for word in _WORD.findall(text.replace("_", " ")):
        parts = [p for p in _CAMEL.split(word) if p]
        if len(parts) > 1:
            tokens.append(word.lower())
        tokens.extend(p.lower() for p in parts)
    return tokens
