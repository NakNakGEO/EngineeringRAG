"""Heuristic parsers for brace languages (C#, Java, TypeScript/JavaScript, Go), SQL and Markdown.

These are deliberately conservative regex scanners (comments and string contents are blanked out
before matching, brace depth gives each symbol an end line). They are an adapter slot: a Tree-sitter
based parser can replace any of them behind :class:`SourceParser`.
"""

from __future__ import annotations

import posixpath
import re
from typing import ClassVar

from eios_project_intelligence.parsers.base import (
    ParsedCall,
    ParsedFile,
    ParsedImport,
    ParsedSymbol,
)

_CALL = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\(")
_KEYWORDS = frozenset(
    [
        "if",
        "for",
        "while",
        "switch",
        "catch",
        "return",
        "new",
        "typeof",
        "sizeof",
        "await",
        "async",
        "function",
        "foreach",
        "using",
        "lock",
        "when",
        "and",
        "or",
        "not",
        "nameof",
        "throw",
        "else",
        "do",
        "try",
        "finally",
        "fixed",
        "checked",
        "unchecked",
        "delegate",
        "base",
        "this",
        "super",
        "in",
        "is",
        "as",
        "with",
        "select",
        "from",
        "where",
        "defer",
        "go",
        "func",
    ]
)


def _blank(text: str, *, line_comment: str = "//", block: bool = True) -> str:
    """Replace comments and string/char literal contents with spaces, preserving newlines/offsets."""
    out: list[str] = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if text.startswith(line_comment, i):
            j = text.find("\n", i)
            j = n if j == -1 else j
            out.append(" " * (j - i))
            i = j
        elif block and text.startswith("/*", i):
            j = text.find("*/", i + 2)
            j = n if j == -1 else j + 2
            out.append("".join("\n" if ch == "\n" else " " for ch in text[i:j]))
            i = j
        elif c in "\"'`":
            j = i + 1
            while j < n and text[j] != c:
                j += 2 if text[j] == "\\" else 1
                if c != "`" and j < n and text[j] == "\n":
                    break
            j = min(j + 1, n)
            out.append(c + "".join("\n" if ch == "\n" else " " for ch in text[i + 1 : j - 1]) + c)
            i = j
        else:
            out.append(c)
            i += 1
    return "".join(out)


def _line_of(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def _block_end_line(blank: str, open_brace: int) -> int:
    depth = 0
    for i in range(open_brace, len(blank)):
        ch = blank[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return _line_of(blank, i)
    return _line_of(blank, len(blank) - 1)


def _body_brace(blank: str, pos: int) -> int:
    """Offset of the ``{`` that opens the body starting after ``pos``, or -1 if the member has
    no block body (``;`` abstract/interface member, or an expression body ``=>``)."""
    for i in range(pos, len(blank)):
        ch = blank[i]
        if ch == "{":
            return i
        if ch == ";" or blank.startswith("=>", i):
            return -1
    return -1


def _line_count(text: str) -> int:
    return text.count("\n") + (0 if text.endswith("\n") or not text else 1)


def _calls_in_bodies(blank: str, symbols: list[ParsedSymbol], out: ParsedFile) -> None:
    lines = blank.split("\n")
    for sym in symbols:
        if sym.kind not in {"function", "method"}:
            continue
        seen: set[tuple[str, int]] = set()
        for lineno in range(sym.start_line, min(sym.end_line, len(lines)) + 1):
            for m in _CALL.finditer(lines[lineno - 1]):
                name = m.group(1)
                if name in _KEYWORDS or name == sym.name or (name, lineno) in seen:
                    continue
                seen.add((name, lineno))
                out.calls.append(ParsedCall(sym.qualified_name, name, lineno))


# --------------------------------------------------------------------------------------------
class CSharpParser:
    language = "csharp"
    _NAMESPACE = re.compile(r"^\s*namespace\s+([\w.]+)", re.M)
    _USING = re.compile(
        r"^\s*(?:global\s+)?using\s+(?:static\s+)?(?:\w+\s*=\s*)?([\w.]+)\s*;", re.M
    )
    _TYPE = re.compile(
        r"^[ \t]*(?:\[[^\]\n]*\][ \t]*)*(?:(?:public|private|protected|internal|static|abstract|sealed|"
        r"partial|readonly|unsafe|new)\s+)*(class|interface|struct|enum|record)\s+(\w+)",
        re.M,
    )
    _METHOD = re.compile(
        r"^[ \t]*(?:(public|private|protected|internal|static|virtual|override|abstract|async|"
        r"sealed|extern|unsafe|new)\s+)+[\w<>\[\],.?\s]+?\s+(\w+)\s*(?:<[^>\n]*>)?\s*\(([^)\n]*)\)"
        r"[^;{=\n]*\{?",
        re.M,
    )

    def parse(self, path: str, text: str) -> ParsedFile:
        blank = _blank(text)
        out = ParsedFile(language="csharp", line_count=_line_count(text))
        for m in self._NAMESPACE.finditer(blank):
            out.provides.append(f"ns:{m.group(1)}")
        for m in self._USING.finditer(blank):
            out.imports.append(ParsedImport(m.group(1), _line_of(blank, m.start())))
        types: list[tuple[int, int, str]] = []
        for m in self._TYPE.finditer(blank):
            brace = blank.find("{", m.end())
            start = _line_of(blank, m.start(2))
            end = _block_end_line(blank, brace) if brace != -1 else start
            kind = {"record": "class"}.get(m.group(1), m.group(1))
            out.symbols.append(
                ParsedSymbol(
                    m.group(2), m.group(2), kind, start, end, signature=f"{m.group(1)} {m.group(2)}"
                )
            )
            types.append((start, end, m.group(2)))
            out.provides.append(f"type:{m.group(2)}")
        for m in self._METHOD.finditer(blank):
            name = m.group(2)
            if name in _KEYWORDS:
                continue
            start = _line_of(blank, m.start(2))
            brace = _body_brace(blank, m.end() - 1 if m.group(0).endswith("{") else m.end())
            end = _block_end_line(blank, brace) if brace != -1 else start
            container = next((t for s, e, t in reversed(types) if s <= start <= e), None)
            qualified = f"{container}.{name}" if container else name
            out.symbols.append(
                ParsedSymbol(
                    name,
                    qualified,
                    "method",
                    start,
                    end,
                    container,
                    f"{name}({m.group(3).strip()})",
                )
            )
        _calls_in_bodies(blank, out.symbols, out)
        return out

    def import_keys(self, path: str, imp: ParsedImport) -> list[str]:
        return [f"ns:{imp.spec}"]


class JavaParser:
    language = "java"
    _PACKAGE = re.compile(r"^\s*package\s+([\w.]+)\s*;", re.M)
    _IMPORT = re.compile(r"^\s*import\s+(?:static\s+)?([\w.]+)(?:\.\*)?\s*;", re.M)
    _TYPE = re.compile(
        r"^[ \t]*(?:@\w+(?:\([^)]*\))?\s*)*(?:(?:public|private|protected|static|abstract|final|sealed)\s+)*"
        r"(class|interface|enum|record)\s+(\w+)",
        re.M,
    )
    _METHOD = re.compile(
        r"^[ \t]*(?:@\w+(?:\([^)]*\))?\s*)*(?:(public|private|protected|static|final|abstract|"
        r"synchronized|native|default)\s+)+[\w<>\[\],.?\s]+?\s+(\w+)\s*\(([^)\n]*)\)[^;{\n]*\{",
        re.M,
    )

    def parse(self, path: str, text: str) -> ParsedFile:
        blank = _blank(text)
        out = ParsedFile(language="java", line_count=_line_count(text))
        pkg = self._PACKAGE.search(blank)
        package = pkg.group(1) if pkg else ""
        if package:
            out.provides.append(f"pkg:{package}")
        for m in self._IMPORT.finditer(blank):
            out.imports.append(ParsedImport(m.group(1), _line_of(blank, m.start())))
        types: list[tuple[int, int, str]] = []
        for m in self._TYPE.finditer(blank):
            brace = blank.find("{", m.end())
            start = _line_of(blank, m.start(2))
            end = _block_end_line(blank, brace) if brace != -1 else start
            kind = "class" if m.group(1) == "record" else m.group(1)
            out.symbols.append(
                ParsedSymbol(
                    m.group(2), m.group(2), kind, start, end, signature=f"{m.group(1)} {m.group(2)}"
                )
            )
            types.append((start, end, m.group(2)))
            out.provides.append(f"type:{package + '.' if package else ''}{m.group(2)}")
        for m in self._METHOD.finditer(blank):
            name = m.group(2)
            if name in _KEYWORDS:
                continue
            start = _line_of(blank, m.start(2))
            end = _block_end_line(blank, m.end() - 1)
            container = next((t for s, e, t in reversed(types) if s <= start <= e), None)
            out.symbols.append(
                ParsedSymbol(
                    name,
                    f"{container}.{name}" if container else name,
                    "method",
                    start,
                    end,
                    container,
                    f"{name}({m.group(3).strip()})",
                )
            )
        _calls_in_bodies(blank, out.symbols, out)
        return out

    def import_keys(self, path: str, imp: ParsedImport) -> list[str]:
        keys = [f"type:{imp.spec}"]
        if "." in imp.spec:
            keys.append(f"pkg:{imp.spec.rsplit('.', 1)[0]}")
        return keys


class TypeScriptParser:
    """TypeScript and JavaScript."""

    language = "typescript"
    _IMPORT = re.compile(
        r"""(?:import\s+(?:[\w*{}\s,]+\s+from\s+)?|export\s+[\w*{}\s,]+\s+from\s+|require\()\s*['"]([^'"]+)['"]"""
    )
    _FUNC = re.compile(
        r"^[ \t]*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s*\*?\s*(\w+)\s*(?:<[^>]*>)?\s*\(([^)]*)\)",
        re.M,
    )
    _ARROW = re.compile(
        r"^[ \t]*(?:export\s+)?(?:const|let|var)\s+(\w+)\s*(?::[^=\n]+)?=\s*(?:async\s*)?(?:\([^)]*\)|\w+)\s*(?::[^=\n]+)?=>",
        re.M,
    )
    _CLASS = re.compile(
        r"^[ \t]*(?:export\s+)?(?:default\s+)?(?:abstract\s+)?(class|interface|enum)\s+(\w+)", re.M
    )
    _TYPE = re.compile(r"^[ \t]*(?:export\s+)?type\s+(\w+)\s*(?:<[^>]*>)?\s*=", re.M)
    _METHOD = re.compile(
        r"^[ \t]+(?:(?:public|private|protected|static|readonly|async|override|get|set)\s+)*(\w+)\s*(?:<[^>]*>)?\s*\(([^)]*)\)\s*(?::\s*[^{;\n]+)?\s*\{",
        re.M,
    )

    def parse(self, path: str, text: str) -> ParsedFile:
        language = "typescript" if path.endswith((".ts", ".tsx", ".mts", ".cts")) else "javascript"
        blank = _blank(text)
        out = ParsedFile(language=language, line_count=_line_count(text))
        stem = posixpath.splitext(path)[0]
        out.provides = [f"file:{stem}"]
        if stem.endswith("/index"):
            out.provides.append(f"file:{stem[: -len('/index')]}")
        for m in self._IMPORT.finditer(text):  # specs live inside strings: scan the raw text
            out.imports.append(ParsedImport(m.group(1), _line_of(text, m.start())))
        classes: list[tuple[int, int, str]] = []
        for m in self._CLASS.finditer(blank):
            brace = blank.find("{", m.end())
            start = _line_of(blank, m.start(2))
            end = _block_end_line(blank, brace) if brace != -1 else start
            out.symbols.append(
                ParsedSymbol(
                    m.group(2),
                    m.group(2),
                    m.group(1),
                    start,
                    end,
                    signature=f"{m.group(1)} {m.group(2)}",
                )
            )
            classes.append((start, end, m.group(2)))
        for m in self._TYPE.finditer(blank):
            line = _line_of(blank, m.start(1))
            out.symbols.append(
                ParsedSymbol(
                    m.group(1), m.group(1), "type", line, line, signature=f"type {m.group(1)}"
                )
            )
        for m in self._FUNC.finditer(blank):
            brace = blank.find("{", m.end())
            start = _line_of(blank, m.start(1))
            end = _block_end_line(blank, brace) if brace != -1 else start
            out.symbols.append(
                ParsedSymbol(
                    m.group(1),
                    m.group(1),
                    "function",
                    start,
                    end,
                    signature=f"function {m.group(1)}({m.group(2).strip()})",
                )
            )
        for m in self._ARROW.finditer(blank):
            start = _line_of(blank, m.start(1))
            brace = blank.find("{", m.end())
            nl = blank.find("\n", m.end())
            end = (
                _block_end_line(blank, brace) if brace != -1 and (nl == -1 or brace < nl) else start
            )
            out.symbols.append(
                ParsedSymbol(
                    m.group(1),
                    m.group(1),
                    "function",
                    start,
                    end,
                    signature=f"const {m.group(1)} = () =>",
                )
            )
        for m in self._METHOD.finditer(blank):
            name = m.group(1)
            if name in _KEYWORDS or (name == "constructor" and not classes):
                continue
            start = _line_of(blank, m.start(1))
            container = next((c for s, e, c in reversed(classes) if s < start <= e), None)
            if container is None:
                continue
            end = _block_end_line(blank, m.end() - 1)
            out.symbols.append(
                ParsedSymbol(
                    name,
                    f"{container}.{name}",
                    "method",
                    start,
                    end,
                    container,
                    f"{name}({m.group(2).strip()})",
                )
            )
        _calls_in_bodies(blank, out.symbols, out)
        return out

    def import_keys(self, path: str, imp: ParsedImport) -> list[str]:
        if not imp.spec.startswith("."):
            return []  # package import: external
        resolved = posixpath.normpath(posixpath.join(posixpath.dirname(path), imp.spec))
        stem = (
            posixpath.splitext(resolved)[0]
            if posixpath.splitext(resolved)[1] in {".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"}
            else resolved
        )
        return [f"file:{stem}", f"file:{stem}/index"]


class GoParser:
    language = "go"
    _PACKAGE = re.compile(r"^\s*package\s+(\w+)", re.M)
    _IMPORT_LINE = re.compile(r'^\s*(?:import\s+)?(?:[\w.]+\s+)?"([^"]+)"\s*$', re.M)
    _FUNC = re.compile(
        r"^func\s+(?:\((\w+)\s+\*?(\w+)(?:\[[^\]]*\])?\)\s+)?(\w+)\s*(?:\[[^\]]*\])?\s*\(([^)]*)\)",
        re.M,
    )
    _TYPE = re.compile(r"^type\s+(\w+)\s+(struct|interface)\b", re.M)

    def parse(self, path: str, text: str) -> ParsedFile:
        blank = _blank(text)
        out = ParsedFile(language="go", line_count=_line_count(text))
        directory = posixpath.dirname(path)
        out.provides = [f"gopkg:{directory}"]
        import_block = re.search(r"^import\s*\((.*?)^\)", text, re.M | re.S)
        specs = []
        if import_block:
            for m in re.finditer(r'"([^"]+)"', import_block.group(1)):
                specs.append((m.group(1), _line_of(text, import_block.start(1) + m.start())))
        for m in re.finditer(r'^import\s+(?:[\w.]+\s+)?"([^"]+)"', text, re.M):
            specs.append((m.group(1), _line_of(text, m.start())))
        out.imports = [ParsedImport(s, ln) for s, ln in specs]
        for m in self._TYPE.finditer(blank):
            brace = blank.find("{", m.end())
            start = _line_of(blank, m.start(1))
            end = _block_end_line(blank, brace) if brace != -1 else start
            out.symbols.append(
                ParsedSymbol(
                    m.group(1),
                    m.group(1),
                    "struct" if m.group(2) == "struct" else "interface",
                    start,
                    end,
                    exported=m.group(1)[0].isupper(),
                    signature=f"type {m.group(1)} {m.group(2)}",
                )
            )
        for m in self._FUNC.finditer(blank):
            name, recv = m.group(3), m.group(2)
            brace = blank.find("{", m.end())
            start = _line_of(blank, m.start())
            end = _block_end_line(blank, brace) if brace != -1 else start
            out.symbols.append(
                ParsedSymbol(
                    name,
                    f"{recv}.{name}" if recv else name,
                    "method" if recv else "function",
                    start,
                    end,
                    recv,
                    f"func {name}({m.group(4).strip()})",
                    exported=name[0].isupper(),
                )
            )
        _calls_in_bodies(blank, out.symbols, out)
        return out

    def import_keys(self, path: str, imp: ParsedImport) -> list[str]:
        # an import path ends with the package directory: match any "<...>/<dir>" suffix
        parts = imp.spec.split("/")
        return [f"gopkg:{'/'.join(parts[i:])}" for i in range(len(parts))]


class SqlParser:
    """SQL scripts, analysed as text only. Nothing here ever connects to a database."""

    language = "sql"
    _CREATE = re.compile(
        r"\bCREATE\s+(?:OR\s+(?:ALTER|REPLACE)\s+)?(?:UNIQUE\s+)?(?:CLUSTERED\s+|NONCLUSTERED\s+)?"
        r"(TABLE|VIEW|PROCEDURE|PROC|FUNCTION|TRIGGER)\s+(?:IF\s+NOT\s+EXISTS\s+)?"
        r"((?:[\[\"`]?[\w$]+[\]\"`]?\.)*[\[\"`]?[\w$]+[\]\"`]?)",
        re.I,
    )
    _REF = re.compile(
        r"\b(FROM|JOIN|INTO|UPDATE|EXEC(?:UTE)?|REFERENCES)\s+"
        r"((?:[\[\"`]?[\w$]+[\]\"`]?\.)*[\[\"`]?[\w$]+[\]\"`]?)",
        re.I,
    )
    _KIND: ClassVar[dict[str, str]] = {
        "table": "table",
        "view": "view",
        "procedure": "procedure",
        "proc": "procedure",
        "function": "function",
        "trigger": "trigger",
    }
    _NOISE = frozenset(
        {"select", "values", "set", "where", "the", "dbo", "as", "tmp", "inserted", "deleted"}
    )

    @staticmethod
    def normalise(name: str) -> str:
        return re.sub(r"[\[\]\"`]", "", name).split(".")[-1].lower()

    def parse(self, path: str, text: str) -> ParsedFile:
        blank = _blank(text, line_comment="--")
        out = ParsedFile(language="sql", line_count=_line_count(text))
        creates = list(self._CREATE.finditer(blank))
        spans: list[tuple[int, int, str]] = []
        for i, m in enumerate(creates):
            name = self.normalise(m.group(2))
            start = _line_of(blank, m.start())
            nxt = creates[i + 1].start() if i + 1 < len(creates) else len(blank)
            end = _line_of(blank, max(m.start(), nxt - 1))
            kind = self._KIND[m.group(1).lower()]
            out.symbols.append(
                ParsedSymbol(name, name, kind, start, end, signature=f"{m.group(1).upper()} {name}")
            )
            out.provides.append(f"sql:{name}")
            spans.append((m.start(), nxt, name))
        for m in self._REF.finditer(blank):
            target = self.normalise(m.group(2))
            if target in self._NOISE or not target:
                continue
            owner = next((n for s, e, n in spans if s <= m.start() < e), None)
            if owner == target:
                continue
            out.calls.append(
                ParsedCall(owner, target, _line_of(blank, m.start()), kind="references")
            )
        return out

    def import_keys(self, path: str, imp: ParsedImport) -> list[str]:
        return []


class MarkdownParser:
    language = "markdown"
    _HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$", re.M)

    def parse(self, path: str, text: str) -> ParsedFile:
        out = ParsedFile(language="markdown", line_count=_line_count(text))
        in_fence = False
        offsets: list[tuple[int, int, str]] = []
        pos = 0
        for lineno, line in enumerate(text.split("\n"), start=1):
            if line.lstrip().startswith("```"):
                in_fence = not in_fence
            elif not in_fence:
                m = self._HEADING.match(line)
                if m:
                    offsets.append((lineno, len(m.group(1)), m.group(2).strip()))
            pos += len(line) + 1
        total = out.line_count
        for i, (lineno, level, title) in enumerate(offsets):
            end = next((ln - 1 for ln, lv, _ in offsets[i + 1 :] if lv <= level), total)
            out.symbols.append(
                ParsedSymbol(
                    title[:200],
                    title[:200],
                    "section",
                    lineno,
                    max(end, lineno),
                    signature="#" * level + " " + title[:100],
                )
            )
        return out

    def import_keys(self, path: str, imp: ParsedImport) -> list[str]:
        return []
