"""Parser contracts and result models. Tree-sitter (or any other engine) plugs in behind
:class:`SourceParser` without touching the indexer."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

SYMBOL_KINDS = frozenset(
    {
        "module", "class", "interface", "struct", "enum", "function", "method", "variable",
        "table", "view", "procedure", "trigger", "section", "namespace", "type",
    }
)  # fmt: skip


@dataclass(frozen=True)
class ParsedSymbol:
    name: str
    qualified_name: str
    kind: str
    start_line: int
    end_line: int
    container: str | None = None
    signature: str = ""
    exported: bool = True


@dataclass(frozen=True)
class ParsedImport:
    spec: str  # module / namespace / relative path as written
    line: int
    names: tuple[str, ...] = ()
    level: int = 0  # python relative-import depth


@dataclass(frozen=True)
class ParsedCall:
    caller: str | None  # qualified name of the enclosing symbol; None = module level
    callee: str
    line: int
    kind: str = "calls"  # "calls" | "references"


@dataclass
class ParsedFile:
    language: str
    line_count: int
    symbols: list[ParsedSymbol] = field(default_factory=list)
    imports: list[ParsedImport] = field(default_factory=list)
    calls: list[ParsedCall] = field(default_factory=list)
    provides: list[str] = field(default_factory=list)  # module keys this file defines
    parse_error: str | None = None


class SourceParser(Protocol):
    language: str

    def parse(self, path: str, text: str) -> ParsedFile: ...

    def import_keys(self, path: str, imp: ParsedImport) -> list[str]:
        """Candidate module keys an import refers to, to be matched against ``provides``."""
        ...
