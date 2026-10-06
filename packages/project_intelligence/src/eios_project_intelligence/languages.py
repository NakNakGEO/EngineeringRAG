"""File classification: language by name/extension, test-file heuristics, binary detection."""

from __future__ import annotations

import posixpath
import re

_BY_EXTENSION: dict[str, str] = {
    ".py": "python", ".pyi": "python",
    ".ts": "typescript", ".tsx": "typescript", ".mts": "typescript", ".cts": "typescript",
    ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript", ".cjs": "javascript",
    ".cs": "csharp", ".java": "java", ".go": "go", ".rs": "rust", ".rb": "ruby", ".php": "php",
    ".kt": "kotlin", ".swift": "swift", ".c": "c", ".h": "c", ".cpp": "cpp", ".hpp": "cpp",
    ".sql": "sql", ".md": "markdown", ".markdown": "markdown", ".rst": "text", ".txt": "text",
    ".json": "json", ".yml": "yaml", ".yaml": "yaml", ".toml": "toml", ".xml": "xml",
    ".csproj": "xml", ".sln": "text", ".sh": "shell", ".ps1": "powershell", ".html": "html",
    ".css": "css", ".ini": "ini", ".cfg": "ini", ".gradle": "gradle", ".proto": "protobuf",
}  # fmt: skip
_BY_NAME = {"dockerfile": "dockerfile", "makefile": "makefile"}

# Languages that have a parser (symbols/imports/calls); everything else is inventory only.
PARSED_LANGUAGES = frozenset(
    {"python", "typescript", "javascript", "csharp", "java", "go", "sql", "markdown"}
)

_TEST_PATH = re.compile(
    r"(^|/)(tests?|__tests__|specs?|e2e)(/|$)|(^|/)test_[^/]+\.py$|_test\.(py|go)$"
    r"|\.(test|spec)\.[jt]sx?$|(Tests?|Spec)\.(cs|java|kt)$"
)


def detect_language(path: str) -> str:
    base = posixpath.basename(path).lower()
    if base in _BY_NAME:
        return _BY_NAME[base]
    return _BY_EXTENSION.get(posixpath.splitext(base)[1], "unknown")


def is_test_path(path: str) -> bool:
    return bool(_TEST_PATH.search(path))


def looks_binary(data: bytes) -> bool:
    return b"\0" in data[:8192]
