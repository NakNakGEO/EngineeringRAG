"""Impact Analyzer (master plan 6.19): what could a change to these targets affect?"""

from __future__ import annotations

import posixpath
import re
import uuid
from typing import Any

from pydantic import BaseModel, Field

from eios_domain.policy import Risk
from eios_domain.project import FileScope
from eios_knowledge import KnowledgeService, SearchScope
from eios_knowledge.decision_repo import DecisionRepository
from eios_knowledge.text import or_query
from eios_project_intelligence import ProjectService
from eios_project_intelligence.languages import is_test_path
from eios_retrieval.graph import walk

_FIX_WORDS = re.compile(r"\b(fix|bug|issue|regress|hotfix|revert|crash|incident)\w*", re.I)
_RULE_KINDS = ("rule", "business_rule", "requirement")
_PATTERN_KINDS = ("pattern", "component")
MIN_IMPACT_CONFIDENCE = 0.3  # impact analysis is conservative: include weaker edges, flag them


class ImpactEntry(BaseModel):
    key: str
    path: str | None
    label: str
    kind: str
    depth: int
    confidence: float
    via: str


class ImpactRef(BaseModel):
    id: str
    title: str
    detail: str = ""


class ImpactReport(BaseModel):
    project_id: uuid.UUID
    branch: str
    targets: list[str]
    callers: list[ImpactEntry] = Field(default_factory=list)
    callees: list[ImpactEntry] = Field(default_factory=list)
    dependents: list[ImpactEntry] = Field(default_factory=list)
    consumers: list[str] = Field(default_factory=list)
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
    low_confidence_edges: int = 0
    confidence: float = 1.0


def _stem(path: str) -> str:
    return posixpath.splitext(posixpath.basename(path))[0]


class ImpactAnalyzer:
    def __init__(
        self,
        projects: ProjectService,
        knowledge: KnowledgeService,
        decisions: DecisionRepository,
    ) -> None:
        self._projects = projects
        self._knowledge = knowledge
        self._decisions = decisions

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
        unresolved: list[str] = []
        target_files: dict[str, None] = {}
        target_symbols: dict[str, None] = {}

        for row in await store.find_files(project_id, resolved_branch, paths or [], scopes=scopes):
            if row.status != "deleted":
                target_files[row.path] = None
        found_paths = set(target_files)
        for wanted in paths or []:
            if not any(
                p == wanted.strip("/") or p.endswith("/" + wanted.strip("/")) for p in found_paths
            ):
                unresolved.append(f"path '{wanted}' is not in the index")
        for sym in await store.find_symbols(
            project_id,
            resolved_branch,
            [s.lower().split(".")[-1] for s in symbols or []],
            scopes=scopes,
            limit=200,
        ):
            wanted_names = {s.lower() for s in symbols or []}
            if sym.name.lower() in wanted_names or sym.qualified_name.lower() in wanted_names:
                target_symbols[f"symbol:{sym.path}::{sym.qualified_name}"] = None
                target_files[sym.path] = None
        found_names = {k.split("::", 1)[1].split(".")[-1].lower() for k in target_symbols}
        for wanted_sym in symbols or []:
            if wanted_sym.lower().split(".")[-1] not in found_names:
                unresolved.append(f"symbol '{wanted_sym}' is not in the index")

        # a file target includes the symbols it defines
        for path in list(target_files):
            if not any(k.startswith(f"symbol:{path}::") for k in target_symbols):
                for row in await store.find_files(
                    project_id, resolved_branch, [path], scopes=scopes
                ):
                    for s in await store.file_symbols(row.id):
                        if s.kind not in {"section", "variable"}:
                            target_symbols[f"symbol:{s.path}::{s.qualified_name}"] = None
        file_keys = [f"file:{p}" for p in target_files]
        symbol_keys = list(target_symbols)
        all_targets = [*file_keys, *symbol_keys]
        report = ImpactReport(
            project_id=project_id,
            branch=resolved_branch,
            targets=all_targets,
            unresolved=unresolved,
        )
        if not all_targets:
            report.confidence = 0.0
            report.risk_factors.append("no target could be resolved in the index")
            return report

        walk_kwargs: dict[str, Any] = {
            "min_confidence": MIN_IMPACT_CONFIDENCE,
            "scopes": scopes,
        }
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
        imports_out = await walk(
            store, project_id, resolved_branch, file_keys, depth=1, direction="out",
            kinds=("imports",), **walk_kwargs,
        )  # fmt: skip

        report.callers = await self._entries(project_id, resolved_branch, callers, scopes)
        report.callees = await self._entries(
            project_id, resolved_branch, {**callees, **imports_out}, scopes
        )
        report.dependents = await self._entries(project_id, resolved_branch, dependents, scopes)
        low = [e for e in [*report.callers, *report.dependents] if e.confidence < 0.5]
        report.low_confidence_edges = len(low)

        affected_files = {
            *(e.path for e in report.callers if e.path),
            *(e.path for e in report.dependents if e.path),
        } - set(target_files)
        report.consumers = sorted(affected_files)
        report.tests = sorted(
            {p for p in affected_files if is_test_path(p)}
            | await self._named_tests(project_id, resolved_branch, list(target_files), scopes)
        )
        report.tests_missing = not report.tests
        report.db_scripts = sorted(
            p for p in {*affected_files, *target_files} if p.lower().endswith(".sql")
        )
        report.related_modules = await self._siblings(
            project_id, resolved_branch, list(target_files), scopes
        )

        names = sorted(
            {k.split("::", 1)[1] for k in symbol_keys} | {_stem(p) for p in target_files}
        )
        report.business_rules = await self._rules(project_id, names, list(target_files))
        report.decisions = await self._decision_refs(project_id, names)
        report.similar_patterns = await self._patterns(project_id, symbol_keys, names)
        report.history = await self._history(project_id, list(target_files))

        self._assess(report, target_files, scopes)
        return report

    # ------------------------------------------------------------------------------------
    async def _entries(
        self, project_id: uuid.UUID, branch: str, reached: dict[str, Any], scopes: Any
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

    @staticmethod
    def _assess(report: ImpactReport, target_files: dict[str, None], scopes: Any) -> None:
        factors: list[str] = []
        score = 0
        blast = len({e.path for e in [*report.callers, *report.dependents] if e.path})
        if blast >= 15:
            score += 3
            factors.append(f"wide blast radius: {blast} dependent files")
        elif blast >= 5:
            score += 2
            factors.append(f"moderate blast radius: {blast} dependent files")
        elif blast >= 1:
            score += 1
            factors.append(f"{blast} dependent file(s)")
        if report.tests_missing:
            score += 2
            factors.append("no tests found for the targets or their dependents")
        if report.db_scripts:
            score += 1
            factors.append("involves SQL scripts (apply and review manually; never executed here)")
        issues = [h for h in report.history if h.detail.startswith("issue-related")]
        if len(issues) >= 3:
            score += 1
            factors.append(f"{len(issues)} past fix/incident commits touch these files")
        if report.business_rules:
            score += 1
            factors.append(f"{len(report.business_rules)} business rule(s) reference the targets")
        if report.low_confidence_edges:
            factors.append(f"{report.low_confidence_edges} dependent link(s) are low confidence")
        if report.unresolved:
            score += 1
            factors.append("some targets could not be resolved")
        report.risk = (
            Risk.CRITICAL
            if score >= 7
            else Risk.HIGH
            if score >= 5
            else Risk.MEDIUM
            if score >= 3
            else Risk.LOW
        )
        report.risk_factors = factors
        known = len(report.targets) - len(report.unresolved)
        report.confidence = round(
            max(
                0.0,
                min(
                    1.0,
                    1.0
                    - 0.1 * len(report.unresolved)
                    - 0.02 * report.low_confidence_edges
                    - (0.0 if known else 0.5),
                ),
            ),
            3,
        )
