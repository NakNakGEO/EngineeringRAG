"""Parser registry: language -> parser."""

from __future__ import annotations

from eios_project_intelligence.parsers.base import (
    ParsedCall,
    ParsedFile,
    ParsedImport,
    ParsedSymbol,
    SourceParser,
)
from eios_project_intelligence.parsers.brace_parsers import (
    CSharpParser,
    GoParser,
    JavaParser,
    MarkdownParser,
    SqlParser,
    TypeScriptParser,
)
from eios_project_intelligence.parsers.python_parser import PythonParser


class ParserRegistry:
    def __init__(self, parsers: list[SourceParser] | None = None) -> None:
        defaults: list[SourceParser] = [
            PythonParser(),
            CSharpParser(),
            JavaParser(),
            TypeScriptParser(),
            GoParser(),
            SqlParser(),
            MarkdownParser(),
        ]
        self._by_language: dict[str, SourceParser] = {p.language: p for p in parsers or defaults}
        # TypeScript and JavaScript share a parser
        if "typescript" in self._by_language:
            self._by_language.setdefault("javascript", self._by_language["typescript"])

    def get(self, language: str) -> SourceParser | None:
        return self._by_language.get(language)

    @property
    def languages(self) -> set[str]:
        return set(self._by_language)


__all__ = [
    "CSharpParser",
    "GoParser",
    "JavaParser",
    "MarkdownParser",
    "ParsedCall",
    "ParsedFile",
    "ParsedImport",
    "ParsedSymbol",
    "ParserRegistry",
    "PythonParser",
    "SourceParser",
    "SqlParser",
    "TypeScriptParser",
]
