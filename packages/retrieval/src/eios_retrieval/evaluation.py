"""Retrieval evaluation: compare strategies on controlled fixtures with known answers.

Strategies (master plan, Phase 4): vector-only, FTS-only, hybrid, hybrid + graph, and hybrid +
graph + coverage expansion. Baselines see every source file as a *chunk* knowledge item (classic
chunk-RAG); the system arms use the symbol index, the code graph and the Context Governor.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from eios_retrieval.fusion import _jaccard, _shingles, estimate_tokens
from eios_retrieval.models import Candidate, CandidateKind, RetrievalQuery
from eios_retrieval.retrievers import RetrievalContext

STALE_HEALTH = {"STALE", "CONTRADICTED", "HISTORICAL", "SUPERSEDED"}


@dataclass(frozen=True)
class EvalCase:
    id: str
    question: str
    relevant: frozenset[str]  # logical ids expected within the top-k
    support: frozenset[str] = frozenset()  # logical ids needed to fully answer (anywhere in pack)
    required_deps: frozenset[str] = frozenset()  # dependencies that must be present
    top1: frozenset[str] = frozenset()  # acceptable first results (symbol accuracy)
    symbols: tuple[str, ...] = ()
    paths: tuple[str, ...] = ()
    expect_ready: bool | None = None  # honesty probe for strategies that report readiness


@dataclass
class ArmOutput:
    candidates: list[Candidate]
    ready: bool | None = None
    tokens: int = 0


def logical_id(candidate: Candidate) -> str:
    """Chunk knowledge items stand for the code they contain; credit them as that code."""
    return str(candidate.metadata.get("logical_id") or candidate.id)


@dataclass
class CaseMetrics:
    case_id: str
    recall_at_k: float
    top1_hit: bool | None
    graph_coverage: float | None
    duplicate_rate: float
    stale_rate: float
    tokens: int
    answer_support: float | None
    ready: bool | None


@dataclass
class ArmReport:
    name: str
    cases: list[CaseMetrics] = field(default_factory=list)

    @staticmethod
    def _mean(values: list[float]) -> float:
        return sum(values) / len(values) if values else 0.0

    @property
    def recall_at_k(self) -> float:
        return self._mean([c.recall_at_k for c in self.cases])

    @property
    def symbol_accuracy(self) -> float:
        return self._mean(
            [1.0 if c.top1_hit else 0.0 for c in self.cases if c.top1_hit is not None]
        )

    @property
    def graph_coverage(self) -> float:
        return self._mean([c.graph_coverage for c in self.cases if c.graph_coverage is not None])

    @property
    def duplicate_rate(self) -> float:
        return self._mean([c.duplicate_rate for c in self.cases])

    @property
    def stale_rate(self) -> float:
        return self._mean([c.stale_rate for c in self.cases])

    @property
    def avg_tokens(self) -> float:
        return self._mean([float(c.tokens) for c in self.cases])

    @property
    def answer_support(self) -> float:
        return self._mean([c.answer_support for c in self.cases if c.answer_support is not None])


def score_case(case: EvalCase, out: ArmOutput, *, k: int) -> CaseMetrics:
    ranked = list(dict.fromkeys(logical_id(c) for c in out.candidates))  # first occurrence wins
    top = ranked[:k]
    recall = len(case.relevant & set(top)) / len(case.relevant) if case.relevant else 1.0
    top1 = (ranked[0] in case.top1) if case.top1 and ranked else (False if case.top1 else None)
    deps = (
        len(case.required_deps & set(ranked)) / len(case.required_deps)
        if case.required_deps
        else None
    )
    knowledge = [c for c in out.candidates if c.kind is CandidateKind.KNOWLEDGE]
    seen: list[set[str]] = []
    dups = 0
    for cand in knowledge:
        shingles = _shingles(f"{cand.title}\n{cand.content}")
        if any(_jaccard(shingles, other) >= 0.85 for other in seen):
            dups += 1
        seen.append(shingles)
    stale = sum(1 for c in knowledge[:k] if c.health is not None and c.health.value in STALE_HEALTH)
    return CaseMetrics(
        case_id=case.id,
        recall_at_k=recall,
        top1_hit=top1,
        graph_coverage=deps,
        duplicate_rate=dups / len(knowledge) if knowledge else 0.0,
        stale_rate=stale / len(knowledge[:k]) if knowledge[:k] else 0.0,
        tokens=out.tokens or sum(estimate_tokens(c.title + c.content) for c in out.candidates),
        answer_support=(len(case.support & set(ranked)) / len(case.support))
        if case.support
        else None,
        ready=out.ready,
    )


Arm = Callable[[EvalCase], Awaitable[ArmOutput]]


async def run_eval(arms: dict[str, Arm], cases: list[EvalCase], *, k: int = 10) -> list[ArmReport]:
    reports: list[ArmReport] = []
    for name, arm in arms.items():
        report = ArmReport(name)
        for case in cases:
            report.cases.append(score_case(case, await arm(case), k=k))
        reports.append(report)
    return reports


def to_markdown(reports: list[ArmReport], *, k: int) -> str:
    header = [
        "strategy", f"recall@{k}", "symbol acc.", "graph cov.", "answer support",
        "dup rate", "stale rate", "avg tokens",
    ]  # fmt: skip
    lines = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    for r in reports:
        cells = [
            r.name,
            f"{r.recall_at_k:.2f}",
            f"{r.symbol_accuracy:.2f}",
            f"{r.graph_coverage:.2f}",
            f"{r.answer_support:.2f}",
            f"{r.duplicate_rate:.2f}",
            f"{r.stale_rate:.2f}",
            f"{r.avg_tokens:.0f}",
        ]
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


# ------------------------------------------------------------------------------------------
def make_context(
    query: RetrievalQuery, project: object, branch: str, scope: object
) -> RetrievalContext:
    """Build a RetrievalContext for baseline arms that call retrievers directly."""
    from eios_knowledge.text import or_query
    from eios_retrieval.entities import extract_entities

    return RetrievalContext(
        query=query,
        entities=extract_entities(query.text, symbols=query.symbols, paths=query.paths),
        project=project,  # type: ignore[arg-type]
        branch=branch,
        search_scope=scope,  # type: ignore[arg-type]
        or_text=or_query(query.text),
    )


__all__ = [
    "Arm",
    "ArmOutput",
    "ArmReport",
    "CaseMetrics",
    "EvalCase",
    "logical_id",
    "make_context",
    "run_eval",
    "score_case",
    "to_markdown",
]
