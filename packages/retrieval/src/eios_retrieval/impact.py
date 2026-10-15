"""Impact Analyzer (master plan 6.19): what could a change to these targets affect?"""

from __future__ import annotations

import fnmatch
import posixpath
import re
import uuid
from collections.abc import Collection, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, Field

from eios_core.settings import (
    DEFAULT_IMPACT_CONTRACT_PATHS,
    DEFAULT_IMPACT_ROUTE_PATHS,
    DEFAULT_IMPACT_SCHEMA_PATHS,
    DEFAULT_IMPACT_SCHEMA_TEST_MARKERS,
    DEFAULT_IMPACT_SECURITY_CRITICAL_PATHS,
    Settings,
)
from eios_domain.policy import Risk
from eios_domain.project import FileScope
from eios_knowledge import KnowledgeService, SearchScope
from eios_knowledge.decision_repo import DecisionRepository
from eios_knowledge.text import or_query
from eios_project_intelligence import ProjectService
from eios_project_intelligence.languages import (
    PARSED_LANGUAGES,
    detect_language,
    is_reexport_hub,
    is_test_path,
    language_family,
)
from eios_project_intelligence.store import FileRow, SymbolRow
from eios_retrieval.graph import WalkNode, unresolved_callers, walk

_FIX_WORDS = re.compile(r"\b(fix|bug|issue|regress|hotfix|revert|crash|incident)\w*", re.I)
_RULE_KINDS = ("rule", "business_rule", "requirement")
_PATTERN_KINDS = ("pattern", "component")
MIN_IMPACT_CONFIDENCE = 0.3  # impact analysis is conservative: include weaker edges, flag them
LOW_CONFIDENCE = 0.5  # entries below this are flagged ``low_confidence``
MAX_WEAK_CALLERS = 30  # name-only (unresolved) callers listed per report
MAX_WEAK_NAMES = 60  # symbol names looked up among unresolved call sites
MAX_MATCHES_PER_SYMBOL = 25
WEAK_VIA = "unresolved-name"  # ``via`` of callers known only by a name match
WEAK_CALLER_FILES_THRESHOLD = 5  # this many files calling by name alone adds a risk point
MEDIUM_AT, HIGH_AT, CRITICAL_AT = 3, 5, 7  # risk score thresholds
_CODE_LANGUAGES = PARSED_LANGUAGES - {
    "markdown",
    "xml",
}  # documents and config have no tests to be missing
_BLAST_STEPS = ((40, 4, "very wide"), (15, 3, "wide"), (5, 2, "moderate"), (1, 1, "small"))


def _glob_match(path: str, patterns: Iterable[str]) -> bool:
    """Case-insensitive glob against ``/<path>``; a pattern without a leading ``*`` or ``/``
    may match at any directory depth."""
    target = "/" + path.lower()
    for pattern in patterns:
        p = pattern.lower()
        if not p.startswith(("*", "/")):
            p = "*/" + p
        if fnmatch.fnmatchcase(target, p):
            return True
    return False


@dataclass(frozen=True)
class ImpactConfig:
    """Which paths the risk model treats as security-critical, schema, routes or contracts.

    The lists come from :class:`~eios_core.settings.Settings` (``EIOS_IMPACT_*``); the analyzer
    itself holds no path knowledge.
    """

    security_critical_paths: tuple[str, ...] = DEFAULT_IMPACT_SECURITY_CRITICAL_PATHS
    schema_paths: tuple[str, ...] = DEFAULT_IMPACT_SCHEMA_PATHS
    schema_test_markers: tuple[str, ...] = DEFAULT_IMPACT_SCHEMA_TEST_MARKERS
    route_paths: tuple[str, ...] = DEFAULT_IMPACT_ROUTE_PATHS
    contract_paths: tuple[str, ...] = DEFAULT_IMPACT_CONTRACT_PATHS

    @classmethod
    def from_settings(cls, settings: Settings) -> ImpactConfig:
        return cls(
            security_critical_paths=tuple(settings.impact_security_critical_paths),
            schema_paths=tuple(settings.impact_schema_paths),
            schema_test_markers=tuple(settings.impact_schema_test_markers),
            route_paths=tuple(settings.impact_route_paths),
            contract_paths=tuple(settings.impact_contract_paths),
        )

    def is_security_critical(self, path: str) -> bool:
        return _glob_match(path, self.security_critical_paths)

    def is_schema(self, path: str) -> bool:
        return _glob_match(path, self.schema_paths)

    def is_route(self, path: str) -> bool:
        return _glob_match(path, self.route_paths)

    def is_contract(self, path: str) -> bool:
        return _glob_match(path, self.contract_paths) or self.is_route(path)

    def is_schema_test(self, path: str) -> bool:
        name = posixpath.basename(path).lower()
        return is_test_path(path) and any(m.lower() in name for m in self.schema_test_markers)


class ImpactEntry(BaseModel):
    key: str
    path: str | None
    label: str
    kind: str
    depth: int
    confidence: float
    via: str
    low_confidence: bool = False  # weak evidence: verify by hand (name-only callers are 0.2)


class ImpactRef(BaseModel):
    id: str
    title: str
    detail: str = ""


class ImpactReport(BaseModel):
    project_id: uuid.UUID
    branch: str
    targets: list[str]
    resolved: bool = True  # False when no target could be found: the risk is not assessable
    callers: list[ImpactEntry] = Field(default_factory=list)
    callees: list[ImpactEntry] = Field(default_factory=list)
    dependents: list[ImpactEntry] = Field(default_factory=list)
    importers: list[ImpactEntry] = Field(default_factory=list)  # file importers of symbol targets
    consumers: list[str] = Field(default_factory=list)
    blast_radius: int = 0  # distinct consumer files (name-only callers excluded)
    tests: list[str] = Field(default_factory=list)
    tests_missing: bool = False
    business_rules: list[ImpactRef] = Field(default_factory=list)
    db_scripts: list[str] = Field(default_factory=list)
    related_modules: list[str] = Field(default_factory=list)
    history: list[ImpactRef] = Field(default_factory=list)
    decisions: list[ImpactRef] = Field(default_factory=list)
    similar_patterns: list[ImpactRef] = Field(default_factory=list)
    risk: Risk = Risk.LOW
    risk_factors: list[str] = Field(default_factory=list)
    unresolved: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    ambiguous: dict[str, list[str]] = Field(default_factory=dict)
    low_confidence_edges: int = 0
    confidence: float = 1.0


@dataclass
class _Targets:
    files: dict[str, None] = field(default_factory=dict)  # requested files
    symbols: dict[str, None] = field(default_factory=dict)  # symbol keys, requested + in files
    requested_symbols: dict[str, None] = field(default_factory=dict)
    symbol_files: dict[str, None] = field(default_factory=dict)  # files defining requested symbols
    unresolved: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    ambiguous: dict[str, list[str]] = field(default_factory=dict)

    @property
    def paths(self) -> list[str]:
        return list(dict.fromkeys([*self.files, *self.symbol_files]))


def _stem(path: str) -> str:
    return posixpath.splitext(posixpath.basename(path))[0]


class ImpactAnalyzer:
    def __init__(
        self,
        projects: ProjectService,
        knowledge: KnowledgeService,
        decisions: DecisionRepository,
        config: ImpactConfig | None = None,
    ) -> None:
        self._projects = projects
        self._knowledge = knowledge
        self._decisions = decisions
        self._config = config or ImpactConfig()

    async def analyze(
        self,
        project_id: uuid.UUID,
        *,
        paths: list[str] | None = None,
        symbols: list[str] | None = None,
        branch: str | None = None,
        depth: int = 3,
        include_overlay: bool = True,
    ) -> ImpactReport:
        project = await self._projects.require(project_id)
        resolved_branch = await self._projects.effective_branch(project, branch)
        store = self._projects.store
        scopes = (
            (FileScope.COMMITTED, FileScope.OVERLAY) if include_overlay else (FileScope.COMMITTED,)
        )
        found = await self._resolve_targets(
            project_id, resolved_branch, paths or [], symbols or [], scopes
        )
        file_keys = [f"file:{p}" for p in found.files]
        symbol_keys = list(found.symbols)
        report = ImpactReport(
            project_id=project_id,
            branch=resolved_branch,
            targets=[*file_keys, *symbol_keys],
            unresolved=found.unresolved,
            warnings=found.warnings,
            ambiguous=found.ambiguous,
        )
        if not report.targets:
            report.resolved = False
            report.confidence = 0.0
            report.risk = Risk.MEDIUM  # unknown is not safe: never report "low" for nothing found
            report.risk_factors.append(
                "no target could be resolved in the index: the risk cannot be assessed"
            )
            return report

        target_paths = found.paths
        symbol_only = [p for p in found.symbol_files if p not in found.files]
        walk_kwargs: dict[str, Any] = {"min_confidence": MIN_IMPACT_CONFIDENCE, "scopes": scopes}
        callers = await walk(
            store, project_id, resolved_branch, symbol_keys, depth=depth, direction="in",
            kinds=("calls", "references"), **walk_kwargs,
        )  # fmt: skip
        callees = await walk(
            store, project_id, resolved_branch, symbol_keys, depth=1, direction="out",
            kinds=("calls", "references"), **walk_kwargs,
        )  # fmt: skip
        dependents = await walk(
            store, project_id, resolved_branch, file_keys, depth=depth, direction="in",
            kinds=("imports",), **walk_kwargs,
        )  # fmt: skip
        importers = await walk(
            store, project_id, resolved_branch, [f"file:{p}" for p in symbol_only], depth=depth,
            direction="in", kinds=("imports",), **walk_kwargs,
        )  # fmt: skip
        imports_out = await walk(
            store, project_id, resolved_branch, file_keys, depth=1, direction="out",
            kinds=("imports",), **walk_kwargs,
        )  # fmt: skip

        report.callers = await self._entries(project_id, resolved_branch, callers, scopes)
        report.callees = await self._entries(
            project_id, resolved_branch, {**callees, **imports_out}, scopes
        )
        report.dependents = await self._entries(project_id, resolved_branch, dependents, scopes)
        report.importers = await self._entries(project_id, resolved_branch, importers, scopes)
        solid = report.callers
        report.callers = [
            *solid,
            *await self._weak_callers(project_id, resolved_branch, found, solid, scopes),
        ]
        report.low_confidence_edges = sum(
            1 for e in [*report.callers, *report.dependents] if e.low_confidence
        )

        # Consumers and blast radius follow edges to the targets themselves. The importers of a
        # symbol's file are listed apart: importing a module is not using the symbol.
        target_set = set(target_paths)
        named = await self._named_importers(project_id, resolved_branch, found, scopes)
        consumers = ({e.path for e in [*solid, *report.dependents] if e.path} | named) - target_set
        report.consumers = sorted(consumers)
        report.blast_radius = len(consumers)
        importer_files = {e.path for e in report.importers if e.path} - target_set
        schema_targets = [p for p in target_paths if self._config.is_schema(p)]
        schema_tests = (
            await self._schema_tests(project_id, resolved_branch, scopes)
            if schema_targets
            else set()
        )
        report.tests = sorted(
            {p for p in [*consumers, *importer_files] if is_test_path(p)}
            | await self._named_tests(project_id, resolved_branch, target_paths, scopes)
            | schema_tests
        )
        code_paths = [p for p in target_paths if detect_language(p) in _CODE_LANGUAGES]
        report.tests_missing = (
            bool(code_paths) and not report.tests and not all(is_test_path(p) for p in code_paths)
        )
        report.db_scripts = sorted(
            p for p in {*consumers, *target_set} if self._config.is_schema(p)
        )
        report.related_modules = await self._siblings(
            project_id, resolved_branch, target_paths, scopes
        )

        names = sorted(
            {k.split("::", 1)[1] for k in symbol_keys} | {_stem(p) for p in target_paths}
        )
        report.business_rules = await self._rules(project_id, names, target_paths)
        report.decisions = await self._decision_refs(project_id, names)
        report.similar_patterns = await self._patterns(project_id, symbol_keys, names)
        report.history = await self._history(project_id, target_paths)

        assess_risk(report, target_paths, self._config)
        return report

    # ------------------------------------------------------------------------------------
    async def _resolve_targets(
        self,
        project_id: uuid.UUID,
        branch: str,
        paths: list[str],
        symbols: list[str],
        scopes: Sequence[FileScope],
    ) -> _Targets:
        store = self._projects.store
        out = _Targets()
        file_rows: dict[str, FileRow] = {}
        if paths:
            rows = await store.find_files(project_id, branch, paths, scopes=scopes)
            gone = {r.path for r in rows if r.status == "deleted"}
            for frow in rows:
                if frow.path not in gone and (
                    frow.path not in file_rows or frow.scope == FileScope.OVERLAY.value
                ):
                    file_rows[frow.path] = frow
        for wanted in paths:
            clean = wanted.strip("/")
            suffix = sorted(p for p in file_rows if p.endswith("/" + clean)) if clean else []
            exact = clean in file_rows
            chosen = [clean] if exact else suffix
            if not chosen:
                out.unresolved.append(f"path '{wanted}' is not in the index")
                continue
            for path in chosen:
                out.files[path] = None
            matches = [*chosen, *(suffix if exact else [])]
            if len(matches) > 1:
                out.ambiguous[wanted] = matches[:10]
                shown = ", ".join(matches[:5]) + (", ..." if len(matches) > 5 else "")
                advice = (
                    "only the exact path was analysed"
                    if exact
                    else "all of them were analysed, pass a longer path to narrow it"
                )
                out.warnings.append(
                    f"path '{wanted}' is ambiguous: it matches {len(matches)} files ({shown}); "
                    + advice
                )

        if symbols:
            rows_by_name = await store.find_symbols(
                project_id,
                branch,
                [s.lower().split(".")[-1] for s in symbols],
                scopes=scopes,
                limit=2000,
            )
            for wanted_sym in symbols:
                w = wanted_sym.lower()
                matched: dict[str, SymbolRow] = {}
                for row in rows_by_name:
                    q = row.qualified_name.lower()
                    if q == w or q.endswith("." + w):
                        matched.setdefault(f"symbol:{row.path}::{row.qualified_name}", row)
                if not matched:
                    out.unresolved.append(f"symbol '{wanted_sym}' is not in the index")
                    continue
                keys = list(matched)[:MAX_MATCHES_PER_SYMBOL]
                for key in keys:
                    out.symbols[key] = None
                    out.requested_symbols[key] = None
                    out.symbol_files[matched[key].path] = None
                if len(matched) > 1:
                    out.ambiguous[wanted_sym] = [k.removeprefix("symbol:") for k in keys[:10]]
                    scope_note = (
                        f"the first {MAX_MATCHES_PER_SYMBOL} were analysed"
                        if len(keys) < len(matched)
                        else "all were analysed"
                    )
                    out.warnings.append(
                        f"symbol '{wanted_sym}' is ambiguous: {len(matched)} definitions match; "
                        + scope_note
                    )

        # a file target includes the symbols it defines
        for path in out.files:
            for s in await store.file_symbols(file_rows[path].id):
                if s.kind not in {"section", "variable", "field"}:
                    out.symbols[f"symbol:{s.path}::{s.qualified_name}"] = None
        return out

    async def _weak_callers(
        self,
        project_id: uuid.UUID,
        branch: str,
        found: _Targets,
        solid: list[ImpactEntry],
        scopes: Sequence[FileScope],
    ) -> list[ImpactEntry]:
        """Call sites that name a target but never resolved to it (``callee:<name>``, 0.2).

        Resolution is best effort; a call it could not place is still a possible caller, so these
        are listed - flagged ``low_confidence`` and never counted in the blast radius.
        """
        names: dict[str, None] = {}
        for key in found.requested_symbols:
            names[key.split("::", 1)[1].split(".")[-1]] = None
        for key in found.symbols:
            path, _, qualified = key.removeprefix("symbol:").partition("::")
            name = qualified.split(".")[-1]
            # migration entry points (upgrade/downgrade) are called by name by the tool, not by code
            if not name.startswith("_") and not self._config.is_schema(path):
                names.setdefault(name, None)
        if not names:
            return []
        reached = await unresolved_callers(
            self._projects.store, project_id, branch, list(names)[:MAX_WEAK_NAMES], scopes=scopes
        )
        known = {e.key for e in solid} | set(found.symbols)
        entries = await self._entries(
            project_id, branch, {k: n for k, n in reached.items() if k not in known}, scopes
        )
        families = {language_family(p) for p in found.paths}
        weak = [
            e.model_copy(update={"via": WEAK_VIA, "low_confidence": True})
            for e in entries
            if e.path and language_family(e.path) in families
        ]
        return sorted(weak, key=lambda e: (e.path or "", e.key))[:MAX_WEAK_CALLERS]

    async def _named_importers(
        self,
        project_id: uuid.UUID,
        branch: str,
        found: _Targets,
        scopes: Sequence[FileScope],
    ) -> set[str]:
        """Files that import a requested top-level symbol *by name* (one re-export hop followed).

        Importing a module is not using one of its symbols, but ``from pkg.events import
        EventType`` is, and a class or constant referenced by attribute leaves no call edge.
        Methods are left to the call edges: importing their class says nothing about them.
        """
        names = {
            qualified
            for key in found.requested_symbols
            if "." not in (qualified := key.split("::", 1)[1])
        }
        if not names:
            return set()
        store = self._projects.store
        frontier = [f"file:{p}" for p in found.symbol_files]
        users: set[str] = set()
        for _ in range(2):  # the defining file, then a package entry point that re-exports it
            hubs: list[str] = []
            for edge in await store.edges_to(
                project_id, branch, frontier, kinds=["imports"], scopes=scopes, limit=5000
            ):
                src = str(edge["src_key"])
                if not src.startswith("file:") or edge["confidence"] < MIN_IMPACT_CONFIDENCE:
                    continue
                imported = set((edge.get("attrs") or {}).get("names") or ())
                if not (names & imported or "*" in imported):
                    continue
                users.add(src.removeprefix("file:"))
                if is_reexport_hub(src):
                    hubs.append(src)
            if not hubs:
                break
            frontier = hubs
        return users

    async def _schema_tests(
        self, project_id: uuid.UUID, branch: str, scopes: Sequence[FileScope]
    ) -> set[str]:
        """Migration and schema tests by naming convention: nothing imports a migration or an
        SQL script, so only the name of a test says that it covers them."""
        rows = await self._projects.store.find_paths_containing(
            project_id,
            branch,
            self._config.schema_test_markers,
            also_any=("test", "spec", "e2e"),
            scopes=scopes,
        )
        return {
            r.path for r in rows if r.status != "deleted" and self._config.is_schema_test(r.path)
        }

    # ------------------------------------------------------------------------------------
    async def _entries(
        self,
        project_id: uuid.UUID,
        branch: str,
        reached: dict[str, WalkNode],
        scopes: Sequence[FileScope],
    ) -> list[ImpactEntry]:
        keys = [k for k, n in reached.items() if n.distance > 0]
        nodes = {
            n["key"]: n
            for n in await self._projects.store.nodes_by_keys(
                project_id, branch, sorted(keys), scopes=scopes
            )
        }
        out: list[ImpactEntry] = []
        for key in keys:
            node = nodes.get(key)
            if node is None or node["kind"] == "dir":
                continue
            walked = reached[key]
            out.append(
                ImpactEntry(
                    key=key,
                    path=node.get("path"),
                    label=str(node["label"]),
                    kind=str((node.get("attrs") or {}).get("kind", node["kind"])),
                    depth=walked.distance,
                    confidence=round(walked.confidence, 3),
                    via=walked.via,
                    low_confidence=walked.confidence < LOW_CONFIDENCE,
                )
            )
        return sorted(out, key=lambda e: (e.depth, -e.confidence, e.key))

    async def _named_tests(
        self, project_id: uuid.UUID, branch: str, target_paths: list[str], scopes: Any
    ) -> set[str]:
        """Tests found by naming convention (test_foo.py, FooTests.cs, foo.test.ts)."""
        candidates: list[str] = []
        for path in target_paths:
            stem = _stem(path)
            ext = posixpath.splitext(path)[1]
            candidates += [
                f"test_{stem}.py", f"{stem}_test.py", f"{stem}_test.go", f"{stem}.test{ext}",
                f"{stem}.spec{ext}", f"{stem}Tests.cs", f"{stem}Test.cs", f"{stem}Test.java",
            ]  # fmt: skip
        rows = await self._projects.store.find_files(project_id, branch, candidates, scopes=scopes)
        return {r.path for r in rows if r.status != "deleted" and is_test_path(r.path)}

    async def _siblings(
        self, project_id: uuid.UUID, branch: str, target_paths: list[str], scopes: Any
    ) -> list[str]:
        out: dict[str, None] = {}
        for directory in sorted({posixpath.dirname(p) for p in target_paths}):
            rows = await self._projects.store.list_files(
                project_id, branch, path_prefix=f"{directory}/" if directory else None, limit=100
            )
            for row in rows:
                if posixpath.dirname(row.path) == directory and row.path not in target_paths:
                    out.setdefault(row.path, None)
        return list(out)[:20]

    async def _rules(
        self, project_id: uuid.UUID, names: list[str], paths: list[str]
    ) -> list[ImpactRef]:
        scope = SearchScope(project_id=project_id)
        refs: dict[str, ImpactRef] = {}
        hits = await self._knowledge.search_text(
            or_query(" ".join(names)), scope, limit=8, kinds=_RULE_KINDS
        )
        for item, _ in hits:
            refs[str(item.id)] = ImpactRef(
                id=f"knowledge:{item.id}", title=item.title, detail=item.content[:200]
            )
        return list(refs.values())

    async def _decision_refs(self, project_id: uuid.UUID, names: list[str]) -> list[ImpactRef]:
        hits = await self._decisions.search_text(
            or_query(" ".join(names)), SearchScope(project_id=project_id), limit=5
        )
        return [
            ImpactRef(
                id=f"decision:{d.id}",
                title=d.question,
                detail=f"{d.status.value}: {d.selected}"[:200],
            )
            for d, _ in hits
        ]

    async def _patterns(
        self, project_id: uuid.UUID, symbol_keys: list[str], names: list[str]
    ) -> list[ImpactRef]:
        text = " ".join(names[:5]) or "pattern"
        hits = await self._knowledge.search_vector(
            text, SearchScope(project_id=project_id), limit=5, kinds=_PATTERN_KINDS
        )
        return [
            ImpactRef(id=f"knowledge:{i.id}", title=i.title, detail=f"similarity {s:.2f}")
            for i, s in hits
            if s >= 0.15
        ]

    async def _history(self, project_id: uuid.UUID, paths: list[str]) -> list[ImpactRef]:
        try:
            commits = await self._projects.history(project_id, paths=paths, limit=15)
        except Exception:  # history is enrichment only
            return []
        return [
            ImpactRef(
                id=f"commit:{c.sha}",
                title=c.subject,
                detail=("issue-related: " if _FIX_WORDS.search(c.subject) else "")
                + f"{c.date[:10]} {c.author}",
            )
            for c in commits
        ]


_RISK_ORDER = (Risk.LOW, Risk.MEDIUM, Risk.HIGH, Risk.CRITICAL)


def _level(score: int) -> Risk:
    if score >= CRITICAL_AT:
        return Risk.CRITICAL
    if score >= HIGH_AT:
        return Risk.HIGH
    return Risk.MEDIUM if score >= MEDIUM_AT else Risk.LOW


def _listed(paths: Sequence[str], limit: int = 3) -> str:
    return ", ".join(paths[:limit]) + (
        f" (+{len(paths) - limit} more)" if len(paths) > limit else ""
    )


def assess_risk(report: ImpactReport, target_paths: Collection[str], config: ImpactConfig) -> None:
    """Score a report and set ``risk``, ``risk_factors`` and ``confidence``.

    Every point comes from a listed factor, so the level is never a black box. Security-critical
    targets are floored at HIGH and schema targets at MEDIUM regardless of the score.
    """
    factors: list[str] = []
    score = 0
    floor = Risk.LOW
    blast = report.blast_radius
    for threshold, points, label in _BLAST_STEPS:
        if blast >= threshold:
            score += points
            factors.append(f"{label} blast radius: {blast} dependent file(s)")
            break
    security = sorted(p for p in target_paths if config.is_security_critical(p))
    if security:
        score += 2
        floor = Risk.HIGH
        factors.append(f"security-critical code, never rated below high: {_listed(security)}")
    elif exposed := sorted(p for p in report.consumers if config.is_security_critical(p)):
        score += 1
        factors.append(f"used by security-critical code: {_listed(exposed)}")
    if any(config.is_schema(p) for p in target_paths):
        score += 2
        floor = max(floor, Risk.MEDIUM, key=_RISK_ORDER.index)
        factors.append(
            "changes the database schema (SQL, migrations or table definitions): hard to undo; "
            "apply and review manually, never executed here"
        )
    elif report.db_scripts:
        score += 1
        factors.append("involves SQL scripts (apply and review manually; never executed here)")
    if contracts := sorted(p for p in target_paths if config.is_contract(p)):
        score += 1
        factors.append(f"public contract surface (API, events or manifests): {_listed(contracts)}")
    if report.tests_missing:
        if all(config.is_route(p) for p in target_paths):
            score += 1
            factors.append(
                "no tests found for this route module by import (it may be tested over HTTP only)"
            )
        else:
            score += 2
            factors.append("no tests found for the targets or their dependents")
    issues = [h for h in report.history if h.detail.startswith("issue-related")]
    if len(issues) >= 3:
        score += 1
        factors.append(f"{len(issues)} past fix/incident commits touch these files")
    if report.business_rules:
        score += 1
        factors.append(f"{len(report.business_rules)} business rule(s) reference the targets")
    weak = [e for e in report.callers if e.via == WEAK_VIA]
    if weak:
        factors.append(
            f"{len(weak)} possible caller(s) matched by name only, not verified "
            "(confidence 0.2; not counted in the blast radius)"
        )
        if len({e.path for e in weak}) >= WEAK_CALLER_FILES_THRESHOLD:
            score += 1
    if other_low := report.low_confidence_edges - len(weak):
        factors.append(f"{other_low} dependent link(s) are low confidence")
    if report.unresolved:
        score += 1
        factors.append("some targets could not be resolved")
    if report.ambiguous:
        factors.append(f"{len(report.ambiguous)} target(s) are ambiguous: see warnings")
    report.risk = max(_level(score), floor, key=_RISK_ORDER.index)
    report.risk_factors = factors
    known = len(report.targets) - len(report.unresolved)
    report.confidence = round(
        max(
            0.0,
            min(
                1.0,
                1.0
                - 0.1 * len(report.unresolved)
                - 0.05 * len(report.ambiguous)
                - min(0.3, 0.02 * report.low_confidence_edges)
                - (0.0 if known else 0.5),
            ),
        ),
        3,
    )
