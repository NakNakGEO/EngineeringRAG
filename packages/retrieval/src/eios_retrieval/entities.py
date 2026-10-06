"""Pull identifiers and paths out of a natural-language request."""

from __future__ import annotations

import re
from dataclasses import dataclass

_BACKTICK = re.compile(r"`([^`\n]{1,200})`")
_PATH = re.compile(
    r"(?<![\w/.-])((?:[\w.-]+/)+[\w.-]+\.[A-Za-z0-9]{1,8}|[\w-]+\.(?:py|cs|ts|tsx|js|jsx|java|go|sql|md|json|ya?ml|toml))(?![\w/-])"
)
_CAMEL = re.compile(r"\b([A-Z][a-z0-9]+(?:[A-Z][a-z0-9]*)+|[a-z]+(?:[A-Z][a-z0-9]*)+)\b")
_SNAKE = re.compile(r"\b([A-Za-z][A-Za-z0-9]*(?:_[A-Za-z0-9]+)+)\b")
_CONSTANT = re.compile(r"\b([A-Z][A-Z0-9]{2,}(?:_[A-Z0-9]+)*)\b")
_DOTTED = re.compile(r"\b([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+)\b")
_STOP = frozenset(
    {
        "The",
        "This",
        "That",
        "What",
        "When",
        "Where",
        "Which",
        "How",
        "Why",
        "SQL",
        "API",
        "HTTP",
        "JSON",
        "URL",
        "TODO",
        "README",
    }
)


@dataclass(frozen=True)
class Entity:
    text: str
    kind: str  # "path" | "identifier"
    explicit: bool = False  # supplied as a hint or `backticked`: the caller clearly means it


def extract_entities(
    text: str, *, symbols: list[str] | None = None, paths: list[str] | None = None
) -> list[Entity]:
    """Entities named in ``text`` plus explicit hints, de-duplicated, order preserved."""
    found: dict[str, Entity] = {}

    def add(value: str, kind: str, explicit: bool = False) -> None:
        value = value.strip().strip(".,;:()[]{}\"'")
        if value and value not in _STOP and len(value) <= 200:
            key = f"{kind}:{value}"
            if key not in found or (explicit and not found[key].explicit):
                found[key] = Entity(value, kind, explicit)

    for p in paths or []:
        add(p, "path", True)
    for s in symbols or []:
        add(s, "identifier", True)
    for m in _BACKTICK.finditer(text):
        token = m.group(1).strip()
        looks_like_file = "/" in token or re.search(
            r"\.(py|cs|ts|tsx|js|jsx|java|go|sql|md|json|ya?ml|toml)$", token
        )
        if looks_like_file:
            add(token, "path", True)
        else:
            add(token.split("(")[0], "identifier", True)
    for m in _PATH.finditer(text):
        add(m.group(1), "path")
    for pattern in (_CAMEL, _SNAKE, _CONSTANT):
        for m in pattern.finditer(text):
            add(m.group(1), "identifier")
    for m in _DOTTED.finditer(text):
        if not _PATH.fullmatch(m.group(1)):
            for part in m.group(1).split("."):
                if len(part) > 2:
                    add(part, "identifier")
    return list(found.values())
