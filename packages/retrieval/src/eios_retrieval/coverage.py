"""Coverage evaluation: is the context sufficient for the decision, and what is missing?

The evaluator never hides a gap behind fabricated confidence: every shortfall becomes a
:class:`MissingContext` entry and lowers coverage/confidence.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass

from eios_domain.knowledge import Health, SourceKind, Trust
from eios_domain.policy import Risk
from eios_domain.project import BootstrapState
from eios_knowledge.text import tokenize
from eios_project_intelligence import ProjectService
from eios_project_intelligence.languages import is_test_path
from eios_retrieval.graph import DEPENDENCIES_PER_SEED, DEPENDENCY_KINDS
from eios_retrieval.intent import is_doc_path
from eios_retrieval.models import (
    CandidateKind,
    ContextItem,
    CoverageReport,
    ExpansionAction,
    InformationGap,
    MissingContext,
    Source,
)
from eios_retrieval.retrievers import RetrievalContext, find_entity_symbols
from eios_retrieval.seeds import named_code
from eios_retrieval.terms import covered_terms, query_terms, term_overlap

READY_THRESHOLD: dict[Risk, float] = {
    Risk.LOW: 0.55, Risk.MEDIUM: 0.70, Risk.HIGH: 0.85, Risk.CRITICAL: 0.90,
}  # fmt: skip
# Minimum share of the seeds' direct dependencies that must be present before the pack may be
# called sufficient. Absent dependencies are the classic way an edit goes wrong.
DEPENDENCY_THRESHOLD: dict[Risk, float] = {
    Risk.LOW: 0.5, Risk.MEDIUM: 0.8, Risk.HIGH: 0.95, Risk.CRITICAL: 1.0,
}  # fmt: skip
INDEX_HEALTH: dict[BootstrapState, float] = {
    BootstrapState.CURRENT: 1.0,
    BootstrapState.DIRTY: 0.9,
    BootstrapState.STALE: 0.6,
    BootstrapState.BRANCH_CHANGED: 0.5,
    BootstrapState.MAJOR_DIVERGENCE: 0.3,
    BootstrapState.NEW: 0.0,
    BootstrapState.ERROR: 0.0,
}
MAX_REQUIRED_DEPENDENCIES = 30


@dataclass
class Evaluation:
    gap: InformationGap
    report: CoverageReport
    required_expansions: list[ExpansionAction]
    contradictions: list[str]
    critical_ids: list[str]


_GRAPH_SOURCES = frozenset({Source.EXACT.value, "graph_tests"})
# Sources that only re-derive what the others found: they do not corroborate anything.
_DERIVED = frozenset({Source.GRAPH.value, "graph_tests"})


def _is_test_evidence(rc: RetrievalContext, item: ContextItem) -> bool:
    """A test the request is plausibly about: named outright, found by the dependents-only test
    lookup, or a test item that mentions what was named (or, with nothing named, most of the
    question's words). A symbol with "test" in its name, or a test reached by walking out and back
    through a shared caller, does not count."""
    c = item.candidate
    if not (c.path and is_test_path(c.path)):
        return False
    if _GRAPH_SOURCES & set(item.sources):
        return True
    text = f"{c.title} {c.content[:4000]}".lower()
    names = [e.text.lower().rsplit(".", 1)[-1] for e in rc.entities if e.kind == "identifier"]
    if names:
        return any(name in text for name in names)
    terms = query_terms(rc.query.text)
    return bool(terms) and term_overlap(terms, text) >= 0.5


def _is_rationale(item: ContextItem) -> bool:
    """Items that can explain *why*: decisions, memories, history, knowledge and documents."""
    c = item.candidate
    if c.kind in {CandidateKind.DECISION, CandidateKind.MEMORY, CandidateKind.COMMIT}:
        return True
    if c.kind is CandidateKind.KNOWLEDGE and c.metadata.get("source_kind") != SourceKind.CODE.value:
        return True  # curated knowledge; text extracted from source says what, not why
    return is_doc_path(c.path)


def _paths_of(ids: set[str]) -> set[str]:
    out: set[str] = set()
    for i in ids:
        if i.startswith("file:"):
            out.add(i[len("file:") :])
        elif i.startswith("symbol:"):
            out.add(i[len("symbol:") :].split("::", 1)[0])
    return out


def _present(key: str, ids: set[str], paths: set[str] | None = None) -> bool:
    """A dependency is covered if it is in the pack, if the file containing a symbol dependency
    is, or (for a file dependency) if any of that file's symbols is."""
    if key in ids:
        return True
    if key.startswith("symbol:"):
        return f"file:{key[len('symbol:') :].split('::', 1)[0]}" in ids
    if key.startswith("file:"):
        return key[len("file:") :] in (paths if paths is not None else _paths_of(ids))
    return False


async def resolve_entities(projects: ProjectService, rc: RetrievalContext) -> dict[str, list[str]]:
    """entity text -> candidate ids it resolves to in the index (empty list = not found)."""
    resolved: dict[str, list[str]] = {}
    if rc.project is None or rc.branch is None:
        return {e.text: [] for e in rc.entities}
    store = projects.store
    found = await find_entity_symbols(
        store, rc.project.id, rc.branch, rc.scopes, rc.entities, limit=80
    )
    for entity in rc.entities:
        ids: list[str] = []
        if entity.kind == "path":
            for row in await store.find_files(
                rc.project.id, rc.branch, [entity.text], scopes=rc.scopes
            ):
                if row.status != "deleted":
                    ids.append(f"file:{row.path}")
        else:
            ids.extend(
                f"symbol:{sym.path}::{sym.qualified_name}" for sym in found.get(entity.text, [])
            )
        resolved[entity.text] = ids
    return resolved


class CoverageEvaluator:
    def __init__(self, projects: ProjectService) -> None:
        self._projects = projects

    async def evaluate(
        self,
        rc: RetrievalContext,
        selected: Sequence[ContextItem],
        resolved: dict[str, list[str]],
        *,
        truncated: bool,
        project_state: BootstrapState | None,
    ) -> Evaluation:
        ids = {i.candidate.id for i in selected}
        missing: list[MissingContext] = []
        required: list[ExpansionAction] = []
        notes: list[str] = []

        # -- entities named in the request ------------------------------------------------
        # Only names the caller clearly means (hints, `backticks`, paths) or that exist in the
        # index count against the pack. A capitalised prose word that matches nothing is noted,
        # and a name the request itself introduces is not expected to exist yet.
        found_in_index = 0
        in_pack = 0
        counted = 0
        for entity in rc.entities:
            targets = resolved.get(entity.text, [])
            if not targets:
                if rc.project is None:
                    continue
                if entity.new:
                    missing.append(
                        MissingContext(
                            kind="new_name",
                            description=f"'{entity.text}' is introduced by the request",
                            subject=entity.text,
                        )
                    )
                elif entity.explicit:
                    counted += 1
                    missing.append(
                        MissingContext(
                            kind="unresolved_entity",
                            description=(
                                f"'{entity.text}' ({entity.kind}) was not found in the index"
                            ),
                            subject=entity.text,
                            blocking=True,
                        )
                    )
                    required.append(
                        ExpansionAction(
                            action="resolve_entity",
                            reason="not found in the indexed project; clarify the name or sync",
                            target=entity.text,
                        )
                    )
                else:
                    missing.append(
                        MissingContext(
                            kind="unresolved_term",
                            description=f"'{entity.text}' ({entity.kind}) matches nothing indexed",
                            subject=entity.text,
                        )
                    )
                continue
            found_in_index += 1
            counted += 1
            if any(t in ids or _present(t, ids) for t in targets):
                in_pack += 1
            else:
                missing.append(
                    MissingContext(
                        kind="entity_not_included",
                        description=f"'{entity.text}' exists but is not in the context pack",
                        subject=entity.text,
                        blocking=True,
                    )
                )
                required.append(
                    ExpansionAction(
                        action="raise_level", reason="named entity missing", target=entity.text
                    )
                )
        entities_total = len(rc.entities)
        relevant = self._relevant_count(rc, selected)

        # -- do the pack's words cover the question's? ---------------------------------------------
        terms = query_terms(rc.query.text)
        covered, uncovered = covered_terms(terms, self._pack_tokens(selected))
        term_cov = len(covered) / len(terms) if terms else 1.0
        if uncovered and selected:
            missing.append(
                MissingContext(
                    kind="uncovered_terms",
                    description="no item in the pack mentions: " + ", ".join(uncovered[:8]),
                )
            )
        grounded = bool(counted) and rc.project is not None
        if grounded:
            entity_cov = in_pack / counted
        elif rc.project is None:  # knowledge-only request: any two relevant items will do
            entity_cov = min(1.0, relevant / 2) if selected else 0.0
        else:
            # Nothing in the request names indexed code, so "the named things are present" is
            # not a test the pack can pass. What can be checked is agreement: items that two
            # independent retrievers both surfaced, and how much of the question the pack echoes.
            corroborated = sum(1 for i in selected if len(set(i.sources) - _DERIVED) >= 2)
            entity_cov = (0.6 * min(1.0, corroborated / 3) + 0.4 * term_cov) if selected else 0.0
            missing.append(
                MissingContext(
                    kind="ungrounded",
                    description=(
                        "no name in the request matched indexed code; sufficiency rests on "
                        f"{corroborated} corroborated item(s) and word overlap"
                    ),
                )
            )

        # -- dependencies of the seeds ---------------------------------------------------
        dep_total, dep_included, dep_missing = await self._dependencies(rc, selected, ids)
        dep_cov = dep_included / dep_total if dep_total else 1.0
        for key in dep_missing[:10]:
            missing.append(
                MissingContext(
                    kind="missing_dependency",
                    description=f"direct dependency {key} of included code is not in the pack",
                    subject=key,
                )
            )
        if dep_total and dep_cov < DEPENDENCY_THRESHOLD[rc.query.risk]:
            missing.append(
                MissingContext(
                    kind="insufficient_dependency_coverage",
                    description=(
                        f"only {dep_included}/{dep_total} direct dependencies of the requested "
                        f"code are in the pack (needs {DEPENDENCY_THRESHOLD[rc.query.risk]:.0%} "
                        f"at {rc.query.risk.value} risk)"
                    ),
                    blocking=True,
                )
            )
        if dep_missing:
            required.append(
                ExpansionAction(
                    action="fetch_dependencies",
                    reason=f"{len(dep_missing)} direct dependencies of the seeds are absent",
                    target=dep_missing[0],
                )
            )

        # -- evidence support & contradictions -----------------------------------------------
        evidence, evidence_missing = self._evidence(rc, selected, ids, resolved, term_cov)
        missing.extend(evidence_missing)
        intent_missing, intent_required = self._intent_gaps(rc, selected)
        missing.extend(intent_missing)
        required.extend(intent_required)
        contradictions = self._contradictions(selected)
        for text in contradictions:
            missing.append(MissingContext(kind="contradiction", description=text))

        # -- index health ----------------------------------------------------------------------
        if rc.project is None:
            health = 1.0
        else:
            health = INDEX_HEALTH.get(project_state or BootstrapState.NEW, 0.0)
            if health < 1.0:
                missing.append(
                    MissingContext(
                        kind="stale_index",
                        description=(
                            f"project index state is {(project_state or BootstrapState.NEW).value}"
                        ),
                        # a stale index can silently mislead: block unless the task is trivial
                        blocking=health == 0.0 or (health < 0.7 and rc.query.risk is not Risk.LOW),
                    )
                )
                if health < 0.9:
                    required.append(
                        ExpansionAction(action="sync_project", reason="index is not current")
                    )
        if truncated:
            missing.append(
                MissingContext(
                    kind="budget",
                    description="the token budget forced lower-ranked context out of the pack",
                )
            )
            notes.append("token budget reached")
        if not selected:
            missing.append(
                MissingContext(
                    kind="no_evidence", description="nothing relevant was found", blocking=True
                )
            )
            required.append(ExpansionAction(action="raise_level", reason="empty result"))

        # A component that cannot apply (dependencies of code nobody named) is left out and the
        # rest re-weighted, rather than awarded for free.
        dep_applies = rc.project is None or bool(named_code(selected))
        weights = {
            "entity": 0.4,
            "dep": 0.3 if dep_applies else 0.0,
            "evidence": 0.2,
            "health": 0.1,
        }
        coverage = (
            weights["entity"] * entity_cov
            + weights["dep"] * dep_cov
            + weights["evidence"] * evidence
            + weights["health"] * health
        ) / sum(weights.values())
        confidence = coverage - 0.1 * len(contradictions) - (0.1 if truncated else 0.0)
        if rc.project is not None and health < 1.0:
            confidence -= (1.0 - health) * 0.2
        confidence = max(0.0, min(1.0, confidence))
        coverage = max(0.0, min(1.0, coverage))
        blocking = any(m.blocking for m in missing)
        ready = coverage >= READY_THRESHOLD[rc.query.risk] and not blocking
        critical = [i.candidate.id for i in selected if i.critical]
        report = CoverageReport(
            entity_coverage=round(entity_cov, 4),
            dependency_coverage=round(dep_cov, 4),
            evidence_support=round(evidence, 4),
            index_health=round(health, 4),
            term_coverage=round(term_cov, 4),
            uncovered_terms=uncovered[:12],
            entities_total=entities_total,
            entities_found=found_in_index,
            dependencies_total=dep_total,
            dependencies_included=dep_included,
            notes=notes,
        )
        return Evaluation(
            gap=InformationGap(
                ready_to_act=ready,
                missing_context=missing,
                confidence=round(confidence, 4),
                coverage=round(coverage, 4),
            ),
            report=report,
            required_expansions=self._dedupe_actions(required),
            contradictions=contradictions,
            critical_ids=critical,
        )

    # ------------------------------------------------------------------------------------
    @staticmethod
    def _dedupe_actions(actions: list[ExpansionAction]) -> list[ExpansionAction]:
        seen: set[tuple[str, str | None]] = set()
        out: list[ExpansionAction] = []
        for a in actions:
            if (a.action, a.target) not in seen:
                seen.add((a.action, a.target))
                out.append(a)
        return out

    @staticmethod
    def _pack_tokens(selected: Sequence[ContextItem]) -> list[str]:
        tokens: list[str] = []
        for item in selected:
            c = item.candidate
            tokens.extend(tokenize(f"{c.title} {c.path or ''} {c.content[:2000]}"))
        return tokens

    @staticmethod
    def _relevant_count(rc: RetrievalContext, selected: Sequence[ContextItem]) -> int:
        terms = query_terms(rc.query.text)
        if not terms:
            return len(selected)
        count = 0
        for item in selected:
            c = item.candidate
            hit, _ = covered_terms(terms, tokenize(f"{c.title} {c.path or ''} {c.content[:2000]}"))
            if len(hit) / len(terms) >= 0.25 or Source.EXACT.value in item.sources:
                count += 1
        return count

    @staticmethod
    def _intent_gaps(
        rc: RetrievalContext, selected: Sequence[ContextItem]
    ) -> tuple[list[MissingContext], list[ExpansionAction]]:
        """Evidence the *kind* of question needs: tests for a tests question, a source for 'why'."""
        intent = rc.intent
        missing: list[MissingContext] = []
        required: list[ExpansionAction] = []
        gating = rc.query.risk is not Risk.LOW
        if intent.wants_tests and not any(_is_test_evidence(rc, i) for i in selected):
            missing.append(
                MissingContext(
                    kind="no_test_evidence",
                    description="the request concerns tests but no test file is in the pack",
                    blocking=intent.asks_tests and gating,
                )
            )
            required.append(
                ExpansionAction(action="raise_level", reason="no test evidence", target="tests")
            )
        if intent.asks_docs and not any(is_doc_path(i.candidate.path) for i in selected):
            missing.append(
                MissingContext(
                    kind="no_doc_evidence",
                    description="the request concerns documentation but no document is in the pack",
                    blocking=gating,
                )
            )
            required.append(
                ExpansionAction(action="raise_level", reason="no document", target="docs")
            )
        if intent.asks_rationale and not any(_is_rationale(i) for i in selected):
            missing.append(
                MissingContext(
                    kind="no_rationale_evidence",
                    description=(
                        "the request asks why, but no document, decision, memory or history "
                        "item is in the pack"
                    ),
                    blocking=gating,
                )
            )
            required.append(
                ExpansionAction(action="raise_level", reason="no rationale source", target="why")
            )
        return missing, required

    async def _dependencies(
        self, rc: RetrievalContext, selected: Sequence[ContextItem], ids: set[str]
    ) -> tuple[int, int, list[str]]:
        """Direct (confident) dependencies of the seed code that should be in the pack."""
        if rc.project is None or rc.branch is None:
            return 0, 0, []
        seeds = [i.candidate for i in named_code(selected)]
        # a symbol is held to its own calls; only a *named file* is held to its imports
        keys = [c.id for c in seeds if c.id.startswith(("file:", "symbol:"))]
        if not keys:
            return 0, 0, []
        edges = await self._projects.store.edges_from(
            rc.project.id, rc.branch, keys, kinds=DEPENDENCY_KINDS, scopes=rc.scopes, limit=2000
        )
        terms = query_terms(rc.query.text)
        seed_ids = {s.id for s in seeds}
        per_seed: dict[str, dict[str, float]] = defaultdict(dict)
        for e in edges:
            dst = e["dst_key"]
            if e["confidence"] < 0.5 or dst.startswith(("external:", "callee:")) or dst in keys:
                continue
            if _present(dst, seed_ids):  # the seed's own file is not a dependency of itself
                continue
            current = per_seed[e["src_key"]]
            current[dst] = max(current.get(dst, 0.0), float(e["confidence"]))
        # the same best-N-per-seed the pack protects (see ContextGovernor._mark_critical)
        top: dict[str, None] = {}
        for group in per_seed.values():
            ordered = sorted(group, key=lambda k: (-group[k], -term_overlap(terms, k), k))
            for key in ordered[:DEPENDENCIES_PER_SEED]:
                top.setdefault(key, None)
        wanted = list(top)[:MAX_REQUIRED_DEPENDENCIES]
        paths = _paths_of(ids)
        included = [k for k in wanted if _present(k, ids, paths)]
        return len(wanted), len(included), [k for k in wanted if k not in included]

    @staticmethod
    def _evidence(
        rc: RetrievalContext,
        selected: Sequence[ContextItem],
        ids: set[str],
        resolved: dict[str, list[str]],
        term_cov: float,
    ) -> tuple[float, list[MissingContext]]:
        missing: list[MissingContext] = []
        has_code = any(
            i.candidate.kind in {CandidateKind.SYMBOL, CandidateKind.FILE} for i in selected
        )
        knowledge = [i.candidate for i in selected if i.candidate.kind is CandidateKind.KNOWLEDGE]
        usable = [
            c
            for c in knowledge
            if c.health not in {Health.STALE, Health.CONTRADICTED, Health.HISTORICAL}
            and c.trust is not None
        ]
        strong = [c for c in usable if c.trust is not None and c.trust >= Trust.VERIFIED]
        if (has_code and any(resolved.values())) or strong:
            score = 1.0
        elif any(c.trust is not None and c.trust >= Trust.OBSERVED for c in usable):
            score = 0.7
        elif usable or has_code:
            score = 0.4 + 0.5 * term_cov  # current source or knowledge that echoes the question
        else:
            score = 0.0
        if rc.query.risk in {Risk.HIGH, Risk.CRITICAL} and not (strong or has_code):
            missing.append(
                MissingContext(
                    kind="no_verified_evidence",
                    description=(
                        "high-risk request without verified knowledge or current source evidence"
                    ),
                )
            )
            score = min(score, 0.4)
        return score, missing

    @staticmethod
    def _contradictions(selected: Sequence[ContextItem]) -> list[str]:
        by_subject: dict[str, list[ContextItem]] = defaultdict(list)
        out: list[str] = []
        for item in selected:
            c = item.candidate
            if c.kind is CandidateKind.KNOWLEDGE and c.subject_key:
                by_subject[c.subject_key].append(item)
            if c.health is Health.CONTRADICTED:
                out.append(f"'{c.title}' is marked CONTRADICTED")
        for subject, items in by_subject.items():
            texts = {" ".join(tokenize(i.candidate.content)) for i in items}
            if len(items) > 1 and len(texts) > 1:
                out.append(f"subject '{subject}' has {len(items)} differing statements")
        return out
