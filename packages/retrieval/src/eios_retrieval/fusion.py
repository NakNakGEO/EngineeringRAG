"""Fusion, deduplication and reranking."""

from __future__ import annotations

import hashlib
import math
from collections import defaultdict
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Protocol

from eios_domain.knowledge import Health, Trust
from eios_knowledge.text import significant_terms, tokenize
from eios_project_intelligence.languages import is_test_path
from eios_retrieval.models import Candidate, CandidateKind, ContextItem, Source

RRF_K = 60

# How much each source is trusted to surface relevant material (weights for reciprocal rank).
SOURCE_WEIGHTS: dict[str, float] = {
    Source.EXACT.value: 1.6,
    Source.SYMBOL.value: 1.1,
    Source.FTS.value: 1.0,
    Source.VECTOR.value: 0.9,
    Source.GRAPH.value: 0.9,
    Source.MEMORY.value: 0.7,
    Source.DECISION.value: 0.9,
    Source.GIT_HISTORY.value: 0.6,
    Source.REUSE.value: 0.8,
    Source.RESEARCH.value: 0.7,
}


def rrf_fuse(
    batches: dict[str, list[Candidate]],
    *,
    weights: dict[str, float] | None = None,
    k: int = RRF_K,
) -> list[ContextItem]:
    """Weighted reciprocal-rank fusion over every candidate list, merged by candidate id."""
    weights = weights or SOURCE_WEIGHTS
    scores: dict[str, float] = defaultdict(float)
    best: dict[str, Candidate] = {}
    sources: dict[str, list[str]] = defaultdict(list)
    reasons: dict[str, list[str]] = defaultdict(list)
    for name, candidates in batches.items():
        for cand in candidates:
            weight = weights.get(cand.source.value, 1.0)
            scores[cand.id] += weight / (k + cand.rank)
            sources[cand.id].append(name)
            reasons[cand.id].append(f"{name}#{cand.rank}")
            current = best.get(cand.id)
            if current is None or (cand.content and not current.content):
                merged = cand if current is None else cand.model_copy(update={"rank": current.rank})
                best[cand.id] = merged
            elif cand.scope == "overlay" and current.scope != "overlay":
                best[cand.id] = cand
            if cand.distance is not None and (best[cand.id].distance is None):
                best[cand.id] = best[cand.id].model_copy(
                    update={"distance": cand.distance, "via": cand.via}
                )
    items = [
        ContextItem(
            candidate=best[cid],
            score=scores[cid],
            sources=sorted(set(sources[cid])),
            reasons=reasons[cid],
        )
        for cid in scores
    ]
    return sorted(items, key=lambda i: (-i.score, i.candidate.id))


def _shingles(text: str, n: int = 3) -> set[str]:
    tokens = tokenize(text)
    if len(tokens) < n:
        return {" ".join(tokens)} if tokens else set()
    return {" ".join(tokens[i : i + n]) for i in range(len(tokens) - n + 1)}


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def dedupe(
    items: Sequence[ContextItem], *, near_duplicate: float = 0.85
) -> tuple[list[ContextItem], list[str]]:
    """Mark redundant items (kept best-first). Returns (items with ``redundant_of`` set, ids)."""
    ordered = sorted(items, key=lambda i: -i.score)
    present_symbol_paths = {
        i.candidate.path
        for i in ordered
        if i.candidate.kind is CandidateKind.SYMBOL and i.candidate.path
    }
    kept_knowledge: list[tuple[ContextItem, set[str], str]] = []
    seen_hash: dict[str, str] = {}
    result: list[ContextItem] = []
    redundant: list[str] = []
    for item in ordered:
        cand = item.candidate
        redundant_of: str | None = None
        if (
            cand.kind is CandidateKind.FILE
            and Source.EXACT.value not in item.sources
            and cand.path in present_symbol_paths  # its symbols are already included
        ):
            redundant_of = f"symbol:{cand.path}"
        if cand.kind is CandidateKind.KNOWLEDGE and redundant_of is None:
            text = f"{cand.title}\n{cand.content}"
            digest = hashlib.sha256(" ".join(tokenize(text)).encode()).hexdigest()
            if digest in seen_hash:
                redundant_of = seen_hash[digest]
            else:
                shingles = _shingles(text)
                for other, other_shingles, other_id in kept_knowledge:
                    if _jaccard(shingles, other_shingles) >= near_duplicate and (
                        other.candidate.subject_key == cand.subject_key
                    ):
                        redundant_of = other_id
                        break
                if redundant_of is None:
                    seen_hash[digest] = cand.id
                    kept_knowledge.append((item, shingles, cand.id))
        if redundant_of is not None:
            redundant.append(cand.id)
            item = item.model_copy(update={"redundant_of": redundant_of})
        result.append(item)
    return result, redundant


class Reranker(Protocol):
    def rerank(
        self, items: Sequence[ContextItem], query: str, *, level_rank: int
    ) -> list[ContextItem]: ...


TRUST_FACTOR = {
    Trust.RAW: 0.6, Trust.OBSERVED: 0.8, Trust.DERIVED: 0.95,
    Trust.VERIFIED: 1.1, Trust.APPROVED: 1.2,
}  # fmt: skip
HEALTH_FACTOR = {
    Health.CURRENT: 1.0, Health.UNVERIFIED: 0.85, Health.STALE: 0.5, Health.CONTRADICTED: 0.3,
    Health.SUPERSEDED: 0.1, Health.HISTORICAL: 0.4, Health.QUARANTINED: 0.0,
}  # fmt: skip


class HeuristicReranker:
    """Combines trust, health, freshness and lexical agreement with the fused rank score.

    Principles from the master plan: prefer current source over stale summaries; low-trust and
    unhealthy knowledge loses ranking (it is flagged, not silently dropped); entities the user
    named outrank guesses.
    """

    def __init__(self, *, now: datetime | None = None) -> None:
        self._now = now

    def _freshness(self, cand: Candidate) -> float:
        raw = cand.metadata.get("updated_at")
        if not isinstance(raw, str):
            return 1.0
        try:
            updated = datetime.fromisoformat(raw)
        except ValueError:
            return 1.0
        age_days = max(0.0, ((self._now or datetime.now(UTC)) - updated).total_seconds() / 86400)
        return 1.0 - 0.2 * min(age_days / 365.0, 1.0)

    def rerank(
        self, items: Sequence[ContextItem], query: str, *, level_rank: int
    ) -> list[ContextItem]:
        terms = set(significant_terms(query, limit=16))
        exact_ids = {i.candidate.id for i in items if Source.EXACT.value in i.sources}
        out: list[ContextItem] = []
        for item in items:
            cand = item.candidate
            factor = 1.0
            reasons = list(item.reasons)
            if cand.trust is not None:
                factor *= TRUST_FACTOR[cand.trust]
            if cand.health is not None:
                factor *= HEALTH_FACTOR[cand.health]
                if cand.health in {Health.STALE, Health.CONTRADICTED, Health.HISTORICAL}:
                    reasons.append(f"health:{cand.health.value}")
            if cand.kind is CandidateKind.KNOWLEDGE:
                factor *= self._freshness(cand)
            if cand.scope == "overlay":
                factor *= 1.1  # the working-tree version is the current truth
            if Source.EXACT.value in item.sources:
                factor *= 1.3
            elif (
                cand.distance == 1
                and str(cand.via or "").endswith("/out")
                and cand.metadata.get("parent") in exact_ids
            ):
                # what a named entity directly uses matters even when it looks nothing like the
                # question (graph-connected dependencies, not semantic neighbours)
                factor *= 1.4
                reasons.append("direct-dependency")
            if cand.kind is CandidateKind.COMMIT:
                factor *= 0.7
            if cand.kind is CandidateKind.MEMORY:
                factor *= 0.8
            if cand.path and is_test_path(cand.path) and level_rank < 3:
                factor *= 0.8
            if terms:
                tokens = set(tokenize(f"{cand.title} {cand.content[:2000]}"))
                overlap = len(terms & tokens) / len(terms)
                factor *= 1.0 + 0.5 * overlap
                if overlap:
                    reasons.append(f"lexical:{overlap:.2f}")
            out.append(item.model_copy(update={"score": item.score * factor, "reasons": reasons}))
        return sorted(out, key=lambda i: (-i.score, i.candidate.id))


def estimate_tokens(text: str) -> int:
    """Cheap, model-agnostic token estimate (~4 characters per token)."""
    return max(1, math.ceil(len(text) / 4))
