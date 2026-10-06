"""Build the graph contribution of one file: nodes, containment, import edges, symbol rows.

Edges reference nodes by key (``file:<path>``, ``symbol:<path>::<qualified>``, ``dir:<path>``,
``external:<name>``), so a file appearing, changing or disappearing never leaves a dangling id.
"""

from __future__ import annotations

import posixpath
from collections.abc import Mapping, Sequence

from eios_project_intelligence.parsers import ParsedFile, SourceParser
from eios_project_intelligence.store import (
    FileChange,
    GraphEdgeSpec,
    GraphNodeSpec,
    SymbolSpec,
)

MAX_PROVIDERS = 8  # an import resolving to more files than this is treated as ambiguous noise


def file_key(path: str) -> str:
    return f"file:{path}"


def symbol_key(path: str, qualified_name: str) -> str:
    return f"symbol:{path}::{qualified_name}"


def dir_key(path: str) -> str:
    return f"dir:{path or '.'}"


def external_name(language: str, spec: str) -> str:
    if language == "python":
        return spec.split(".")[0]
    if language in {"typescript", "javascript"}:
        parts = spec.split("/")
        return "/".join(parts[:2]) if spec.startswith("@") else parts[0]
    if language == "java":
        return ".".join(spec.split(".")[:2])
    if language == "csharp":
        return spec.split(".")[0]
    return spec


def _ancestors(path: str) -> list[str]:
    parts = path.split("/")[:-1]
    return ["/".join(parts[: i + 1]) for i in range(len(parts))]


def resolve_imports(
    path: str,
    parsed: ParsedFile,
    parser: SourceParser | None,
    key_index: Mapping[str, Sequence[str]],
) -> list[GraphEdgeSpec]:
    """Import edges from ``file:<path>`` to the files that provide the imported module keys."""
    if parser is None:
        return []
    edges: list[GraphEdgeSpec] = []
    src = file_key(path)
    for imp in parsed.imports:
        keys = parser.import_keys(path, imp)
        providers: list[str] = []
        for key in keys:
            for provider in key_index.get(key, ()):
                if provider != path and provider not in providers:
                    providers.append(provider)
        attrs = {"spec": imp.spec, "line": imp.line, "keys": keys}
        if providers and len(providers) <= MAX_PROVIDERS:
            confidence = 1.0 if len(providers) == 1 else 0.6
            edges.extend(
                GraphEdgeSpec(src, file_key(p), "imports", path, confidence=confidence, attrs=attrs)
                for p in providers
            )
        elif (
            not providers
            and imp.spec
            and not imp.level
            and (name := external_name(parsed.language, imp.spec))
        ):
            edges.append(
                GraphEdgeSpec(src, f"external:{name}", "imports", path, confidence=1.0, attrs=attrs)
            )
    return edges


def build_file_change(
    path: str,
    *,
    language: str,
    size_bytes: int,
    content_hash: str,
    parsed: ParsedFile | None,
    parser: SourceParser | None,
    key_index: Mapping[str, Sequence[str]],
    extra_metadata: dict[str, object] | None = None,
) -> FileChange:
    metadata: dict[str, object] = dict(extra_metadata or {})
    change = FileChange(
        path=path,
        language=language,
        size_bytes=size_bytes,
        line_count=parsed.line_count if parsed else 0,
        content_hash=content_hash,
        metadata=metadata,
    )
    fkey = file_key(path)
    change.nodes.append(GraphNodeSpec(fkey, "file", path, path, {"language": language}))

    # physical hierarchy: dir -> dir -> file
    ancestors = _ancestors(path)
    previous = None
    for d in ancestors:
        change.nodes.append(GraphNodeSpec(dir_key(d), "dir", posixpath.basename(d) or d, None))
        if previous is not None:
            change.edges.append(GraphEdgeSpec(dir_key(previous), dir_key(d), "contains", None))
        previous = d
    parent = dir_key(ancestors[-1] if ancestors else "")
    if not ancestors:
        change.nodes.append(GraphNodeSpec(parent, "dir", ".", None))
    change.edges.append(GraphEdgeSpec(parent, fkey, "contains", path))

    if parsed is None:
        return change
    if parsed.parse_error:
        metadata["parse_error"] = parsed.parse_error
    metadata["provides"] = parsed.provides
    metadata["symbol_count"] = len(parsed.symbols)

    by_qualified = {s.qualified_name for s in parsed.symbols}
    for s in parsed.symbols:
        change.symbols.append(
            SymbolSpec(
                s.name,
                s.qualified_name,
                s.kind,
                s.container,
                s.start_line,
                s.end_line,
                s.signature,
                s.exported,
            )
        )
        skey = symbol_key(path, s.qualified_name)
        change.nodes.append(
            GraphNodeSpec(
                skey,
                s.kind
                if s.kind
                in {
                    "class",
                    "interface",
                    "function",
                    "method",
                    "table",
                    "view",
                    "procedure",
                    "section",
                }
                else "symbol",
                s.qualified_name,
                path,
                {"kind": s.kind, "start_line": s.start_line, "end_line": s.end_line},
            )
        )
        owner_key = (
            symbol_key(path, s.container) if s.container and s.container in by_qualified else fkey
        )
        change.edges.append(GraphEdgeSpec(owner_key, skey, "contains", path))
    change.edges.extend(resolve_imports(path, parsed, parser, key_index))
    return change
