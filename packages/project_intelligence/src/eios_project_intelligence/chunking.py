"""Searchable file text: split documents, configuration and migrations into bounded chunks.

The symbol index answers "where is this function"; it cannot answer "what does the threat model
say" or "which migration creates this trigger", because documents, YAML, Dockerfiles and SQL
have no (useful) symbols. Those files are cut into chunks of roughly 40-60 lines / 2 000
characters along their natural seams (headings, top-level keys, statements, targets, stages),
each remembering its line range, so retrieval can point at exactly the lines that match.

Everything here is pure: text in, :class:`ChunkSpec` out. Large, generated and secret-bearing
files are never indexed as text (lockfiles keep a short stub so they stay findable by name).
"""

from __future__ import annotations

import posixpath
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from eios_observability.redaction import redact_text
from eios_project_intelligence.languages import TEST_PATH_PATTERN

CHUNK_VERSION = 1  # bump to re-chunk every already indexed file on the next sync

TARGET_LINES = 40  # a soft seam (blank line, sub-heading) ends a chunk once it is this long
MAX_LINES = 60
MIN_LINES = 8  # a strong seam (heading, top-level key) ends a chunk once it is this long
TARGET_CHARS = 1500
MAX_CHARS = 2000
MIN_CHARS = 400
MAX_CHUNKS_PER_FILE = 150
MAX_TEXT_CHARS = 400_000  # bigger files keep a stub only
MAX_UNKNOWN_CHARS = 64_000  # files of unknown type are only indexed when they are small
MAX_LINE_CHARS = 4_000  # a longer line means minified or generated data: stub
STUB_LINES = 12
MAX_IDENTS = 300

# Languages (see :mod:`eios_project_intelligence.languages`) whose text is indexed. Source code in
# the parsed languages is covered by symbols; its non-code neighbours are covered here.
CHUNKED_LANGUAGES = frozenset(
    {
        "markdown", "text", "yaml", "toml", "json", "dockerfile", "makefile", "shell",
        "powershell", "sql", "ini", "xml", "gradle", "protobuf", "unknown",
    }
)  # fmt: skip
# Source files whose text is indexed too: migration scripts (the SQL, constraint and trigger names
# are in string literals the symbol parser does not see) and tests (assertions, fixtures and
# messages say what the code under test is *for*). Other source code is covered by its symbols.
# One pattern for Python (``re``) and for SQL (``~``), so the two never drift apart.
CODE_TEXT_PATH_SQL = rf"(^|/)(migrations?|alembic)/(.+/)?[^/]+\.py$|{TEST_PATH_PATTERN}"
CODE_TEXT_PATH = re.compile(CODE_TEXT_PATH_SQL)

_LOCKFILES = frozenset(
    {
        "uv.lock", "poetry.lock", "pipfile.lock", "package-lock.json", "npm-shrinkwrap.json",
        "yarn.lock", "pnpm-lock.yaml", "cargo.lock", "composer.lock", "gemfile.lock", "go.sum",
        "gradle.lockfile", "packages.lock.json", "mix.lock", "flake.lock", "bun.lockb",
    }
)  # fmt: skip
_GENERATED_MARK = re.compile(r"@generated|DO NOT EDIT|Code generated|auto-?generated", re.I)
_SECRET_BASENAMES = frozenset(
    {
        ".npmrc", ".pypirc", ".netrc", "credentials", "credentials.json", "secrets.yaml",
        "secrets.yml", "secrets.json", "secrets.toml", "service-account.json",
    }
)  # fmt: skip
_SECRET_SUFFIXES = (
    ".pem", ".key", ".p12", ".pfx", ".jks", ".keystore", ".kdbx", ".tfstate", ".tfvars",
)  # fmt: skip
_ENV_EXAMPLE = frozenset({"example", "sample", "template", "dist", "defaults", "test"})


@dataclass(frozen=True)
class ChunkSpec:
    ordinal: int
    start_line: int  # 1-based, inclusive
    end_line: int
    heading: str  # where in the document this sits: "ADR 0008 > Safe git access"
    text: str
    idents: tuple[str, ...]  # whole identifiers and words mentioned, case preserved


@dataclass(frozen=True)
class ChunkResult:
    chunks: list[ChunkSpec] = field(default_factory=list)
    status: str = "chunked"  # chunked | stub | skipped
    reason: str | None = None
    truncated: bool = False


def wants_chunks(path: str, language: str) -> bool:
    """Whether the text of this file belongs in the content index."""
    return language in CHUNKED_LANGUAGES or (
        language != "binary" and bool(CODE_TEXT_PATH.search(path))
    )


def is_sensitive_path(path: str) -> bool:
    """Files that routinely hold credentials: never stored as text."""
    base = posixpath.basename(path).lower()
    if base in _SECRET_BASENAMES or base.endswith(_SECRET_SUFFIXES):
        return True
    if base.startswith(("id_rsa", "id_dsa", "id_ecdsa", "id_ed25519")):
        return True
    if base == ".env" or base.startswith(".env."):
        return base.rsplit(".", 1)[-1] not in _ENV_EXAMPLE
    return False


def is_lockfile(path: str) -> bool:
    base = posixpath.basename(path).lower()
    return base in _LOCKFILES or base.endswith((".min.js", ".min.css", ".map"))


# -- identifiers ----------------------------------------------------------------------------
_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*")
_FILE_SUFFIXES = frozenset(
    [
        *("py", "md", "yaml", "yml", "json", "toml", "sql", "ts", "tsx", "js", "jsx", "sh"),
        *("txt", "lock", "cfg", "ini", "env", "html", "css", "csv", "xml"),
    ]
)


def identifiers(text: str) -> tuple[str, ...]:
    """Distinct words and dotted names in ``text``, case preserved: the keys of the exact-name
    lookup (``uq_event_run_seq``, ``eios.retention``, ``RetentionService``, ``max_attempts``)."""
    found: dict[str, None] = {}
    for match in _IDENT.finditer(text):
        token = match.group(0)
        parts = token.split(".")
        if len(parts) > 1 and parts[-1].lower() in _FILE_SUFFIXES:
            parts = parts[:-1]  # "pyproject.toml": the extension is not a name
            token = ".".join(parts)
        if len(parts) > 1 and len(token) <= 120:
            found.setdefault(token, None)
        for part in parts:
            if 3 <= len(part) <= 120:
                found.setdefault(part, None)
        if len(found) >= MAX_IDENTS:
            break
    return tuple(list(found)[:MAX_IDENTS])


def is_identifier_like(text: str) -> bool:
    """A name worth looking up on its own: snake_case, CamelCase, dotted or ALL_CAPS_WITH_UNDERSCORE
    - not an ordinary lower-case word."""
    if "_" in text.strip("_") or "." in text:
        return True
    return bool(re.search(r"[a-z][A-Z]", text)) or bool(
        re.fullmatch(r"[A-Z][a-z0-9]+[A-Z]\w*", text)
    )


# -- seams ----------------------------------------------------------------------------------
# A seam describes where a document can be cut. ``strength[i]`` is 2 for a strong seam (a new
# top-level unit starts at line i), 1 for a soft one, 0 otherwise; ``heading[i]`` is the heading in
# effect at line i.


@dataclass
class _Outline:
    strength: list[int]
    heading: list[str]


_BLANK = re.compile(r"^\s*$")
_FENCE = re.compile(r"^\s*(```|~~~)")
_ATX = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
_RST_RULE = re.compile(r"^([=\-~^\"#*+`:.'_])\1{2,}\s*$")
_YAML_KEY = re.compile(r"^([A-Za-z_\"'][^:#]*?):(\s|$)")
_TOML_TABLE = re.compile(r"^\s*\[{1,2}([^\]]+)\]{1,2}\s*(#.*)?$")
_INI_SECTION = re.compile(r"^\[([^\]]+)\]")
_DOCKER_FROM = re.compile(r"^FROM\s+(\S+)(?:\s+AS\s+(\S+))?", re.I)
_DOCKER_INSTR = re.compile(
    r"^(FROM|RUN|CMD|ENTRYPOINT|COPY|ADD|ENV|ARG|WORKDIR|EXPOSE|USER|VOLUME|LABEL|HEALTHCHECK|"
    r"SHELL|ONBUILD|STOPSIGNAL)\b",
    re.I,
)
_MAKE_TARGET = re.compile(r"^([A-Za-z0-9_.%$()/ -]+?)\s*::?(?![=:])(\s|$)")
_SHELL_FUNC = re.compile(r"^(?:function\s+)?([A-Za-z_][\w-]*)\s*\(\)\s*\{?")
_SQL_STATEMENT = re.compile(
    r"^(CREATE|ALTER|DROP|INSERT|UPDATE|DELETE|GRANT|REVOKE|COMMENT|SELECT|WITH|DO|TRUNCATE|"
    r"SET|BEGIN|COMMIT)\b",
    re.I,
)
_PY_DEF = re.compile(r"^(?:async\s+)?(?:def|class)\s+(\w+)")
_PY_OP = re.compile(r"^\s{4}(op\.\w+|op\.execute|conn\w*\.execute)\(")


def _clip(text: str, size: int = 100) -> str:
    text = " ".join(text.split())
    return text if len(text) <= size else text[: size - 1] + "…"


def _flat(lines: Sequence[str]) -> _Outline:
    return _Outline([0] * len(lines), [""] * len(lines))


def _blank_seams(lines: Sequence[str], out: _Outline) -> None:
    for i, line in enumerate(lines):
        if i and _BLANK.match(lines[i - 1]) and not _BLANK.match(line) and out.strength[i] == 0:
            out.strength[i] = 1


def _hoist_comments(
    lines: Sequence[str], out: _Outline, marker: str, labels: dict[int, str]
) -> None:
    """A comment block directly above a seam belongs to what follows: move the seam up."""
    for i in range(len(lines) - 1, 0, -1):
        if out.strength[i] < 2:
            continue
        j = i
        while j > 0 and lines[j - 1].lstrip().startswith(marker):
            j -= 1
        if j != i:
            out.strength[j] = max(out.strength[j], out.strength[i])
            out.strength[i] = 0
            if i in labels:
                labels[j] = labels.pop(i)


def _fill_headings(out: _Outline, labels: dict[int, str]) -> None:
    current = ""
    for i in range(len(out.heading)):
        current = labels.get(i, current)
        out.heading[i] = current


def _markdown(lines: Sequence[str]) -> _Outline:
    out = _flat(lines)
    labels: dict[int, str] = {}
    trail: list[tuple[int, str]] = []
    fenced = False
    for i, line in enumerate(lines):
        if _FENCE.match(line):
            fenced = not fenced
            continue
        if fenced:
            continue
        m = _ATX.match(line)
        if m:
            level, title = len(m.group(1)), _clip(m.group(2), 80)
            while trail and trail[-1][0] >= level:
                trail.pop()
            trail.append((level, title))
            labels[i] = " > ".join(t for _, t in trail[-3:])
            out.strength[i] = 2 if level <= 2 else 1
        elif (
            i
            and _RST_RULE.match(line)
            and lines[i - 1].strip()
            and not _RST_RULE.match(lines[i - 1])
        ):
            title = _clip(lines[i - 1], 80)  # setext / reStructuredText heading
            labels[i - 1] = title
            out.strength[i - 1] = max(out.strength[i - 1], 2)
    _blank_seams(lines, out)
    _fill_headings(out, labels)
    return out


def _yaml(lines: Sequence[str]) -> _Outline:
    out = _flat(lines)
    indents = [
        len(line) - len(line.lstrip())
        for line in lines
        if line.strip() and not line.lstrip().startswith(("#", "-"))
    ]
    nested = sorted({n for n in indents if n > 0})
    second = nested[0] if nested else 0
    labels: dict[int, str] = {}
    top = ""
    for i, line in enumerate(lines):
        indent = len(line) - len(line.lstrip())
        if indent == 0 and line.startswith("---"):
            out.strength[i] = 2
        m = _YAML_KEY.match(line.strip()) if line.strip() else None
        if m and not line.lstrip().startswith(("#", "- ")):
            if indent == 0:
                top = _clip(m.group(1).strip("\"'"), 60)
                labels[i] = top
                out.strength[i] = 2
            elif indent == second and top:
                labels[i] = f"{top} > {_clip(m.group(1).strip(chr(34) + chr(39)), 60)}"
                out.strength[i] = max(out.strength[i], 1)
    _blank_seams(lines, out)
    _hoist_comments(lines, out, "#", labels)
    _fill_headings(out, labels)
    return out


def _toml(lines: Sequence[str]) -> _Outline:
    out = _flat(lines)
    labels: dict[int, str] = {}
    for i, line in enumerate(lines):
        m = _TOML_TABLE.match(line)
        if m:
            labels[i] = _clip(m.group(1).strip(), 80)
            out.strength[i] = 2
    _blank_seams(lines, out)
    _hoist_comments(lines, out, "#", labels)
    _fill_headings(out, labels)
    return out


def _ini(lines: Sequence[str]) -> _Outline:
    out = _flat(lines)
    labels: dict[int, str] = {}
    for i, line in enumerate(lines):
        m = _INI_SECTION.match(line)
        if m:
            labels[i] = _clip(m.group(1), 80)
            out.strength[i] = 2
    _blank_seams(lines, out)
    _fill_headings(out, labels)
    return out


def _json(lines: Sequence[str]) -> _Outline:
    out = _flat(lines)
    stripped = [line.strip() for line in lines]
    first = next((s for s in stripped if s), "")
    keyed = [
        len(line) - len(line.lstrip())
        for line, s in zip(lines, stripped, strict=True)
        if s.startswith('"') and '":' in s
    ]
    base = min(keyed, default=0)
    objects = [
        len(line) - len(line.lstrip())
        for line, s in zip(lines, stripped, strict=True)
        if s.startswith("{")
    ]
    labels: dict[int, str] = {}
    for i, line in enumerate(lines):
        indent = len(line) - len(line.lstrip())
        if first.startswith("[") and objects:
            if stripped[i].startswith("{") and indent == min(objects) and i:
                out.strength[i] = 2  # a list of records: one seam per record
        elif stripped[i].startswith('"') and '":' in stripped[i] and indent == base and base > 0:
            labels[i] = _clip(stripped[i].split('"', 2)[1], 60)
            out.strength[i] = 2
    _blank_seams(lines, out)
    _fill_headings(out, labels)
    return out


def _dockerfile(lines: Sequence[str]) -> _Outline:
    out = _flat(lines)
    labels: dict[int, str] = {}
    for i, line in enumerate(lines):
        m = _DOCKER_FROM.match(line)
        if m:
            labels[i] = f"stage {m.group(2)}" if m.group(2) else f"FROM {m.group(1)}"
            out.strength[i] = 2
        elif _DOCKER_INSTR.match(line) and not (i and lines[i - 1].rstrip().endswith("\\")):
            out.strength[i] = max(out.strength[i], 1)
    _blank_seams(lines, out)
    _hoist_comments(lines, out, "#", labels)
    _fill_headings(out, labels)
    return out


def _makefile(lines: Sequence[str]) -> _Outline:
    out = _flat(lines)
    labels: dict[int, str] = {}
    for i, line in enumerate(lines):
        if line.startswith(("\t", " ", "#")):
            continue
        m = _MAKE_TARGET.match(line)
        if m and not m.group(1).startswith("."):
            labels[i] = _clip(m.group(1).strip(), 60)
            out.strength[i] = 2
    _blank_seams(lines, out)
    _hoist_comments(lines, out, "#", labels)
    _fill_headings(out, labels)
    return out


def _shell(lines: Sequence[str]) -> _Outline:
    out = _flat(lines)
    labels: dict[int, str] = {}
    for i, line in enumerate(lines):
        m = _SHELL_FUNC.match(line)
        if m:
            labels[i] = m.group(1)
            out.strength[i] = 2
    _blank_seams(lines, out)
    _hoist_comments(lines, out, "#", labels)
    _fill_headings(out, labels)
    return out


def _sql(lines: Sequence[str]) -> _Outline:
    out = _flat(lines)
    labels: dict[int, str] = {}
    for i, line in enumerate(lines):
        if _SQL_STATEMENT.match(line):
            labels[i] = _clip(line, 100)
            out.strength[i] = 2
    _blank_seams(lines, out)
    _hoist_comments(lines, out, "--", labels)
    _fill_headings(out, labels)
    return out


def _python(lines: Sequence[str]) -> _Outline:
    out = _flat(lines)
    labels: dict[int, str] = {}
    function = ""
    for i, line in enumerate(lines):
        m = _PY_DEF.match(line)
        if m:
            function = m.group(1)
            labels[i] = f"def {function}"
            out.strength[i] = 2
        elif _PY_OP.match(line):
            labels[i] = _clip(f"{function}: {line.strip()}", 100)
            out.strength[i] = 1
    _blank_seams(lines, out)
    _fill_headings(out, labels)
    return out


def _plain(lines: Sequence[str]) -> _Outline:
    out = _flat(lines)
    _blank_seams(lines, out)
    return out


_OUTLINES: dict[str, Callable[[Sequence[str]], _Outline]] = {
    "markdown": _markdown,
    "text": _markdown,  # reStructuredText and plain text: underlined titles, blank-line seams
    "yaml": _yaml,
    "toml": _toml,
    "ini": _ini,
    "json": _json,
    "dockerfile": _dockerfile,
    "makefile": _makefile,
    "shell": _shell,
    "powershell": _shell,
    "sql": _sql,
    "python": _python,  # migrations and tests
}


# -- packing --------------------------------------------------------------------------------
@dataclass
class _Segment:
    line: int  # 1-based source line
    text: str
    strength: int
    heading: str


def _segments(lines: Sequence[str], out: _Outline) -> list[_Segment]:
    """One segment per line; a line longer than a chunk is split so no chunk can exceed the cap."""
    segments: list[_Segment] = []
    for i, line in enumerate(lines):
        text = redact_text(line.rstrip())  # before measuring: masking can lengthen a line
        if len(text) <= MAX_CHARS:
            segments.append(_Segment(i + 1, text, out.strength[i], out.heading[i]))
            continue
        first = True
        while text:
            cut = text.rfind(" ", MAX_CHARS // 2, MAX_CHARS)
            cut = cut + 1 if cut > 0 else MAX_CHARS
            segments.append(
                _Segment(i + 1, text[:cut], out.strength[i] if first else 0, out.heading[i])
            )
            text, first = text[cut:], False
    return segments


def _pack(segments: Sequence[_Segment], limit: int) -> tuple[list[tuple[int, int]], bool]:
    """Greedy cut into ``(first, last+1)`` segment ranges of at most MAX_LINES/MAX_CHARS."""
    ranges: list[tuple[int, int]] = []
    start = 0
    while start < len(segments) and len(ranges) < limit:
        chars = 0
        last_soft: int | None = None
        end = start
        while end < len(segments):
            seg = segments[end]
            n = end - start
            if n > 0:
                size_after = chars + len(seg.text) + 1
                if seg.strength >= 2 and (n >= MIN_LINES or chars >= MIN_CHARS):
                    break
                if seg.strength >= 1 and (n >= TARGET_LINES or chars >= TARGET_CHARS):
                    break
                if n >= MAX_LINES or size_after > MAX_CHARS:
                    if last_soft is not None and last_soft - start >= max(1, n // 2):
                        end = last_soft
                    break
                if seg.strength >= 1:
                    last_soft = end
            chars += len(seg.text) + 1
            end += 1
        ranges.append((start, end))
        start = end
    return ranges, start < len(segments)


# -- entry point ----------------------------------------------------------------------------
def _stub(text: str, reason: str) -> ChunkResult:
    head = "\n".join(line[:200] for line in text.split("\n")[:STUB_LINES]).strip()
    if not head:
        return ChunkResult([], "skipped", reason)
    spec = ChunkSpec(0, 1, min(STUB_LINES, text.count("\n") + 1), f"({reason})", head, ())
    return ChunkResult([spec], "stub", reason)


def chunk_text(path: str, language: str, text: str) -> ChunkResult:
    """Cut one file's text into chunks (or decide it must not be indexed as text)."""
    if is_sensitive_path(path):
        return ChunkResult([], "skipped", "sensitive")
    if is_lockfile(path):
        return _stub(text, "lockfile: index stub only")
    if language == "unknown" and len(text) > MAX_UNKNOWN_CHARS:
        return _stub(text, "large file of unknown type: index stub only")
    if len(text) > MAX_TEXT_CHARS:
        return _stub(text, "large file: index stub only")
    lines = text.split("\n")
    if any(len(line) > MAX_LINE_CHARS for line in lines):
        return _stub(text, "minified or generated: index stub only")
    if _GENERATED_MARK.search("\n".join(lines[:5])) and path.endswith((".json", ".yaml", ".yml")):
        return _stub(text, "generated: index stub only")
    outline = _OUTLINES.get(language, _plain)(lines)
    segments = _segments(lines, outline)
    ranges, truncated = _pack(segments, MAX_CHUNKS_PER_FILE)
    chunks: list[ChunkSpec] = []
    for first, end in ranges:
        group = list(segments[first:end])
        while group and not group[-1].text.strip():
            group.pop()
        while group and not group[0].text.strip():
            group.pop(0)
        if not group:
            continue
        body = "\n".join(s.text for s in group)
        chunks.append(
            ChunkSpec(
                len(chunks),
                group[0].line,
                group[-1].line,
                next((s.heading for s in group if s.heading), ""),
                body,
                identifiers(body),
            )
        )
    return ChunkResult(chunks, "chunked", None, truncated)


__all__ = [
    "CHUNKED_LANGUAGES",
    "CHUNK_VERSION",
    "CODE_TEXT_PATH_SQL",
    "MAX_CHARS",
    "MAX_CHUNKS_PER_FILE",
    "MAX_LINES",
    "ChunkResult",
    "ChunkSpec",
    "chunk_text",
    "identifiers",
    "is_identifier_like",
    "is_lockfile",
    "is_sensitive_path",
    "wants_chunks",
]
