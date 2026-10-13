"""Resolve call/reference sites to symbols (best effort, with explicit confidence)."""

from __future__ import annotations

import builtins
import uuid
from collections import defaultdict
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass

from eios_domain.project import FileScope
from eios_project_intelligence.graph_builder import file_key, symbol_key
from eios_project_intelligence.languages import is_reexport_hub, language_family
from eios_project_intelligence.store import GraphEdgeSpec, ProjectStore, SymbolRow

SQL_KINDS = ("table", "view", "procedure", "function", "trigger")
_NAME_CHUNK = 400
_HUB_CHUNK = 400


def _words(text: str) -> frozenset[str]:
    return frozenset(text.split())


# Names that are almost always the language's own functions or the methods of its builtin types.
# They resolve only through the caller's own file or imports, never by a project-wide guess, and
# leave no ``callee:`` placeholder: a ``str(x)`` or ``items.append(y)`` is noise, not a call
# into project code.
_PY_BUILTIN_FUNCTIONS = frozenset(n for n in dir(builtins) if not n.startswith("_"))
_PY_BUILTIN_METHODS = _words(
    """append extend insert pop popitem remove clear copy sort reverse index count get items keys
    values update setdefault fromkeys add discard union intersection difference issubset issuperset
    join split rsplit splitlines strip lstrip rstrip replace format startswith endswith lower upper
    casefold title capitalize find rfind partition rpartition encode decode isdigit isalpha isalnum
    zfill ljust rjust center removeprefix removesuffix hexdigest digest"""
)
_JS_BUILTINS = _words(
    """test exec match matchAll replace replaceAll split trim trimStart trimEnd slice splice
    substring substr indexOf lastIndexOf includes startsWith endsWith toLowerCase toUpperCase
    toString toFixed padStart padEnd map filter forEach reduce reduceRight find findIndex some
    every flat flatMap concat join push pop shift unshift sort reverse fill keys values entries
    fromEntries assign freeze has get set add delete clear then catch finally resolve reject all
    allSettled race parse stringify isArray isNaN isFinite parseInt parseFloat String Number
    Boolean Array Object Date Map Set Error Promise Symbol BigInt RegExp fetch require setTimeout
    clearTimeout setInterval clearInterval addEventListener removeEventListener querySelector
    querySelectorAll getElementById createElement appendChild preventDefault stopPropagation
    getTime toISOString now random floor ceil round abs min max pow sqrt log warn error info
    debug"""
)
_BUILTINS_BY_FAMILY: Mapping[str, frozenset[str]] = {
    "python": _PY_BUILTIN_FUNCTIONS | _PY_BUILTIN_METHODS,
    "js": _JS_BUILTINS,
}


@dataclass(frozen=True)
class CallRequest:
    owner_path: str  # file that contains the call site
    caller: str | None  # qualified name of the enclosing symbol; None = module level
    callee: str
    line: int
    kind: str = "calls"  # "calls" | "references"


@dataclass(frozen=True)
class Resolution:
    targets: tuple[SymbolRow, ...]
    confidence: float  # of the best single target; split evenly when there are several


def is_builtin_callee(owner_path: str, callee: str) -> bool:
    """True when ``callee`` is a builtin of the language of ``owner_path`` (``str``, ``append``)."""
    return callee in _BUILTINS_BY_FAMILY.get(language_family(owner_path), frozenset())


def pick_targets(
    req: CallRequest, candidates: Sequence[SymbolRow], imported: Collection[str]
) -> Resolution | None:
    """Choose the definition(s) a call site refers to, or ``None`` when it is a builtin call.

    Only definitions in the caller's own language family are considered. Confidence: same file
    0.95 > imported file (re-exports followed) 0.8 > the only candidate in the project 0.3 >
    unresolved (no targets, 0.2).
    """
    family = language_family(req.owner_path)
    if req.kind == "references":
        pool = [c for c in candidates if c.kind in SQL_KINDS]
    else:
        pool = [
            c
            for c in candidates
            if c.kind not in {"section", "variable", "field"}
            # a nested def can only be called from the file that defines it
            and not (c.kind == "function" and c.container and c.path != req.owner_path)
        ]
    pool = [c for c in pool if language_family(c.path) == family]
    # a method that delegates to another object's method of the same name (``def evaluate``
    # calling ``self._engine.evaluate``) is not calling itself; direct recursion is not an edge
    # worth keeping either way, so the enclosing symbol is never its own same-file candidate
    same_file = [
        c
        for c in pool
        if c.path == req.owner_path and not (req.caller and c.qualified_name == req.caller)
    ]
    via_import = [c for c in pool if c.path in imported]
    if same_file:
        return Resolution(tuple(same_file), 0.95)
    if via_import:
        return Resolution(tuple(via_import), 0.8)
    if req.kind == "calls" and is_builtin_callee(req.owner_path, req.callee):
        return None
    if len({c.path for c in pool}) == 1 and len(pool) <= 3:
        return Resolution(tuple(pool), 0.3)
    if req.kind == "references" and pool:
        return Resolution(tuple(pool[:1]), 0.3)
    return Resolution((), 0.2)


async def expand_reexports(
    store: ProjectStore,
    project_id: uuid.UUID,
    branch: str,
    imported_paths: Mapping[str, set[str]],
    owners: set[str],
    *,
    scopes: Sequence[FileScope] = (FileScope.COMMITTED,),
    hidden_committed_paths: frozenset[str] = frozenset(),
) -> dict[str, set[str]]:
    """Follow imports of ``__init__.py`` / ``index.ts`` one hop to the files they re-export.

    ``from pkg import X`` resolves to the package entry point, not to the module that defines
    ``X``; without this the definition would never count as imported.
    """
    hubs = sorted({p for o in owners for p in imported_paths.get(o, ()) if is_reexport_hub(p)})
    behind: dict[str, set[str]] = defaultdict(set)
    for i in range(0, len(hubs), _HUB_CHUNK):
        edges = await store.edges_from(
            project_id,
            branch,
            [file_key(h) for h in hubs[i : i + _HUB_CHUNK]],
            kinds=["imports"],
            scopes=scopes,
            limit=200_000,
        )
        for e in edges:
            if (
                e["scope"] == FileScope.COMMITTED.value
                and e["owner_path"] in hidden_committed_paths
            ):
                continue  # that hub is overridden by its overlay version
            if str(e["dst_key"]).startswith("file:"):
                behind[str(e["src_key"]).removeprefix("file:")].add(
                    str(e["dst_key"]).removeprefix("file:")
                )
    expanded = {o: set(imported_paths.get(o, ())) for o in owners}
    for owner, direct in list(expanded.items()):
        for p in list(direct):
            expanded[owner] |= behind.get(p, set())
    return expanded


async def resolve_calls(
    store: ProjectStore,
    project_id: uuid.UUID,
    branch: str,
    requests: Sequence[CallRequest],
    *,
    imported_paths: dict[str, set[str]],
    scopes: Sequence[FileScope] = (FileScope.COMMITTED,),
    hidden_committed_paths: frozenset[str] = frozenset(),
    scope_label: FileScope = FileScope.COMMITTED,
) -> list[GraphEdgeSpec]:
    """Turn call sites into edges.

    Confidence: same file 0.95 > imported file 0.8 (a package's re-exports count as imported) >
    the only candidate in the project 0.3 > unresolved ``callee:<name>`` placeholder 0.2 (kept so
    a later-added definition can resolve it). Candidates are limited to the caller's language
    family, and calls to language builtins produce no edge at all (see :func:`pick_targets`).
    """
    names = sorted({r.callee.lower() for r in requests})
    by_name: dict[str, list[SymbolRow]] = defaultdict(list)
    for i in range(0, len(names), _NAME_CHUNK):
        rows = await store.find_symbols(
            project_id, branch, names[i : i + _NAME_CHUNK], scopes=scopes, limit=20000
        )
        for row in rows:
            if row.scope == FileScope.COMMITTED.value and row.path in hidden_committed_paths:
                continue  # overridden by the overlay version of the same file
            by_name[row.name.lower()].append(row)
    imported = await expand_reexports(
        store,
        project_id,
        branch,
        imported_paths,
        {r.owner_path for r in requests},
        scopes=scopes,
        hidden_committed_paths=hidden_committed_paths,
    )

    best: dict[tuple[str, str, str], GraphEdgeSpec] = {}
    for req in requests:
        src = symbol_key(req.owner_path, req.caller) if req.caller else file_key(req.owner_path)
        resolution = pick_targets(
            req, by_name.get(req.callee.lower(), []), imported.get(req.owner_path, set())
        )
        if resolution is None:
            continue
        targets, confidence = resolution.targets, resolution.confidence
        attrs = {"callee": req.callee, "line": req.line}
        if not targets:
            specs = [
                GraphEdgeSpec(
                    src,
                    f"callee:{req.callee}",
                    req.kind,
                    req.owner_path,
                    confidence=confidence,
                    attrs=attrs,
                )
            ]
        else:
            specs = [
                GraphEdgeSpec(
                    src,
                    symbol_key(t.path, t.qualified_name),
                    req.kind,
                    req.owner_path,
                    confidence=confidence / len(targets) if len(targets) > 1 else confidence,
                    attrs=attrs,
                )
                for t in targets[:5]
            ]
        for spec in specs:
            if spec.src_key == spec.dst_key:
                continue
            k = (spec.src_key, spec.dst_key, spec.kind)
            if k not in best or spec.confidence > best[k].confidence:
                best[k] = spec
    return list(best.values())
