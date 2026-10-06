"""Retrieval and context models (the shapes the Context Governor and clients exchange)."""

from __future__ import annotations

import uuid
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from eios_domain.knowledge import Health, Trust
from eios_domain.policy import Risk


class ContextLevel(StrEnum):
    L0 = "L0"  # immediate: the exact entities named in the request
    L1 = "L1"  # focused: + lexical/semantic matches and direct dependencies
    L2 = "L2"  # expanded: + callers/dependents, memory, decisions, history
    L3 = "L3"  # deep: + transitive dependencies, tests, reusable components, research
    L4 = "L4"  # archaeology: + whole-subsystem siblings and long history

    @property
    def rank(self) -> int:
        return list(ContextLevel).index(self)

    def next(self) -> ContextLevel | None:
        order = list(ContextLevel)
        return order[self.rank + 1] if self.rank + 1 < len(order) else None


class Source(StrEnum):
    EXACT = "exact"
    SYMBOL = "symbol"
    FTS = "fts"
    VECTOR = "vector"
    GRAPH = "graph"
    MEMORY = "memory"
    DECISION = "decision"
    GIT_HISTORY = "git_history"
    REUSE = "reuse"
    RESEARCH = "research"


class CandidateKind(StrEnum):
    KNOWLEDGE = "knowledge"
    SYMBOL = "symbol"
    FILE = "file"
    MEMORY = "memory"
    DECISION = "decision"
    COMMIT = "commit"


class RetrievalQuery(BaseModel):
    model_config = ConfigDict(frozen=True)

    text: str = Field(min_length=1, max_length=4000)
    project_id: uuid.UUID | None = None
    branch: str | None = None
    include_overlay: bool = True
    symbols: list[str] = Field(default_factory=list, description="explicit identifiers of interest")
    paths: list[str] = Field(default_factory=list, description="explicit files of interest")
    risk: Risk = Risk.MEDIUM
    include_ephemeral: bool = True


class Candidate(BaseModel):
    """One retrievable thing. ``id`` is stable and doubles as the graph node key for code."""

    id: str
    kind: CandidateKind
    title: str
    content: str = ""
    path: str | None = None
    start_line: int | None = None
    end_line: int | None = None
    source: Source
    rank: int = Field(ge=1)  # rank within the producing retriever (1 = best)
    raw_score: float = 0.0
    trust: Trust | None = None
    health: Health | None = None
    confidence: float | None = None
    subject_key: str | None = None
    scope: str | None = None  # "committed" | "overlay" for code
    distance: int | None = None  # graph distance from the nearest seed
    via: str | None = None  # how a graph candidate was reached (edge kind)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ContextItem(BaseModel):
    """A candidate after fusion, dedupe and rerank."""

    candidate: Candidate
    score: float
    sources: list[str]
    reasons: list[str] = Field(default_factory=list)
    redundant_of: str | None = None
    critical: bool = False


class MissingContext(BaseModel):
    kind: str  # unresolved_entity | missing_dependency | stale_index | no_evidence | budget | ...
    description: str
    subject: str | None = None
    blocking: bool = False


class ExpansionAction(BaseModel):
    action: str  # e.g. "raise_level", "fetch_dependencies", "resolve_entity", "sync_project"
    reason: str
    target: str | None = None


class InformationGap(BaseModel):
    """Never hides missing context behind fabricated confidence."""

    ready_to_act: bool
    missing_context: list[MissingContext]
    confidence: float = Field(ge=0, le=1)
    coverage: float = Field(ge=0, le=1)


class CoverageReport(BaseModel):
    entity_coverage: float
    dependency_coverage: float
    evidence_support: float
    index_health: float
    entities_total: int
    entities_found: int
    dependencies_total: int
    dependencies_included: int
    notes: list[str] = Field(default_factory=list)


class ContextPack(BaseModel):
    """The Context Governor's output (master plan: Context Governor prompt)."""

    query: str
    level: ContextLevel
    items: list[ContextItem]
    gap: InformationGap
    coverage: CoverageReport
    expansions: list[ExpansionAction] = Field(default_factory=list)
    required_expansions: list[ExpansionAction] = Field(default_factory=list)
    redundant_context_ids: list[str] = Field(default_factory=list)
    critical_evidence_ids: list[str] = Field(default_factory=list)
    contradictions: list[str] = Field(default_factory=list)
    budget_tokens: int
    used_tokens: int
    truncated: bool = False
    levels_tried: list[ContextLevel] = Field(default_factory=list)
    reason: str = ""

    @property
    def ready_to_act(self) -> bool:
        return self.gap.ready_to_act
