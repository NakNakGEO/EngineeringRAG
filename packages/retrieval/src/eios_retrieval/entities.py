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
# Files named without a conventional extension, and dotfiles (the leading dot is part of the name).
_KNOWN_FILE = re.compile(
    r"(?<![\w/.-])(README|CHANGELOG|CONTRIBUTING|LICENSE|Dockerfile|Makefile)(?:\.md)?(?![\w/-])"
)
_DOTFILE = re.compile(
    r"(?<![\w/.-])(\.(?:env(?:\.[\w-]+)?|gitignore|gitattributes|dockerignore|editorconfig))(?![\w/-])"
)
_BARE_TO_PATH = {
    "README": "README.md",
    "CHANGELOG": "CHANGELOG.md",
    "CONTRIBUTING": "CONTRIBUTING.md",
}
_DOTTED = re.compile(r"\b([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+)\b")
# Names the request itself introduces ("add a nullable correlation_id column", "called Foo"):
# they are not expected to exist yet, so not finding them is not a gap.
_NAMED = re.compile(r"\b(?:called|named)\s+`?([A-Za-z_]\w*)`?")
_INTRODUCED = re.compile(
    r"\b(?:add|adds|adding|new|create|creates|creating|introduce|introduces)\b[^.?!\n`]{0,48}?"
    r"`?\b([A-Za-z_]\w*)`?\s+(?:column|field|table|index|function|method|class|parameter|param|"
    r"attribute|property|endpoint|route|flag|setting|constant|variable|type|enum|event|tool|"
    r"handler|job|check|state|status|key)\b",
    re.I,
)
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
    }
)


@dataclass(frozen=True)
class Entity:
    text: str
    kind: str  # "path" | "identifier"
    explicit: bool = False  # supplied as a hint or `backticked`: the caller clearly means it
    new: bool = False  # introduced by the request itself ("add a ... foo_bar column")


def extract_entities(
    text: str, *, symbols: list[str] | None = None, paths: list[str] | None = None
) -> list[Entity]:
    """Entities named in ``text`` plus explicit hints, de-duplicated, order preserved."""
    found: dict[str, Entity] = {}
    introduced = {m.group(1) for pattern in (_NAMED, _INTRODUCED) for m in pattern.finditer(text)}

    def add(value: str, kind: str, explicit: bool = False) -> None:
        if kind == "path":  # keep the leading dot of dotfiles such as .github/ or .env.example
            value = value.strip().lstrip("([{\"'").rstrip(".,;:)]}\"'")
        else:
            value = value.strip().strip(".,;:()[]{}\"'")
        if value and value not in _STOP and len(value) <= 200:
            key = f"{kind}:{value}"
            if key not in found or (explicit and not found[key].explicit):
                found[key] = Entity(
                    value, kind, explicit, new=kind == "identifier" and value in introduced
                )

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
    for m in _KNOWN_FILE.finditer(text):
        add(_BARE_TO_PATH.get(m.group(1), m.group(1)), "path")
    for m in _DOTFILE.finditer(text):
        add(m.group(1), "path")
    for pattern in (_CAMEL, _SNAKE, _CONSTANT):
        for m in pattern.finditer(text):
            # a bare ALL-CAPS word in prose (YAML, DELETE, MCP) is an acronym or emphasis; real
            # constants carry an underscore (EVENT_TYPES). Backticks or hints stay explicit.
            if pattern is _CONSTANT and "_" not in m.group(1):
                continue
            add(m.group(1), "identifier")
    for m in _DOTTED.finditer(text):
        if not _PATH.fullmatch(m.group(1)):
            for part in m.group(1).split("."):
                if len(part) > 2:
                    add(part, "identifier")
    return list(found.values())
