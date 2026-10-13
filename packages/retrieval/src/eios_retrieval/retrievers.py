"""Individual retrievers. Each produces ranked :class:`Candidate`s from one source."""

from __future__ import annotations

import asyncio
import math
import re
import uuid
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Protocol

from eios_domain.knowledge import KnowledgeItem, SourceKind
from eios_domain.project import FileScope
from eios_domain.vault import Vault
from eios_knowledge import KnowledgeService, SearchScope
from eios_knowledge.decision_repo import DecisionRepository
from eios_knowledge.memory_repo import MemoryRepository
from eios_knowledge.text import significant_terms, tokenize
from eios_project_intelligence import Project, ProjectService
from eios_project_intelligence.store import FileRow, ProjectStore, SymbolRow
from eios_retrieval.entities import Entity
from eios_retrieval.intent import QueryIntent
from eios_retrieval.models import Candidate, CandidateKind, RetrievalQuery, Source
from eios_retrieval.terms import covered_terms, query_terms

# Kinds that are lexical noise for a name search (they still resolve as named entities).
LEXICAL_SKIP_KINDS = frozenset({"section", "variable", "field"})


@dataclass
class RetrievalContext:
    """Everything a retriever may need for one request."""

    query: RetrievalQuery
    entities: list[Entity]
    project: Project | None = None
    branch: str | None = None
    scopes: tuple[FileScope, ...] = (FileScope.COMMITTED,)
    search_scope: SearchScope = field(default_factory=SearchScope)
    or_text: str = ""
    intent: QueryIntent = field(default_factory=QueryIntent)


class Retriever(Protocol):
    name: str
    source: Source

    async def retrieve(self, rc: RetrievalContext, limit: int) -> list[Candidate]: ...


def symbol_candidate(row: SymbolRow, source: Source, rank: int, score: float = 0.0) -> Candidate:
    return Candidate(
        id=f"symbol:{row.path}::{row.qualified_name}",
        kind=CandidateKind.SYMBOL,
        title=f"{row.qualified_name} ({row.kind})",
        content=row.signature,
        path=row.path,
        start_line=row.start_line,
        end_line=row.end_line,
        source=source,
        rank=rank,
        raw_score=score,
        scope=row.scope,
        metadata={"kind": row.kind, "exported": row.exported, "name": row.name},
    )


def file_candidate(row: FileRow, source: Source, rank: int, score: float = 0.0) -> Candidate:
    return Candidate(
        id=f"file:{row.path}",
        kind=CandidateKind.FILE,
        title=row.path,
        path=row.path,
        source=source,
        rank=rank,
        raw_score=score,
        scope=row.scope,
        metadata={"language": row.language, "lines": row.line_count},
    )


def knowledge_candidate(item: KnowledgeItem, source: Source, rank: int, score: float) -> Candidate:
    return Candidate(
        id=f"knowledge:{item.id}",
        kind=CandidateKind.KNOWLEDGE,
        title=item.title,
        content=item.content,
        path=str(item.metadata.get("path")) if item.metadata.get("path") else None,
        source=source,
        rank=rank,
        raw_score=score,
        trust=item.trust,
        health=item.health,
        confidence=item.confidence,
        subject_key=item.subject_key,
        metadata={
            "kind": item.kind,
            "vault": item.vault.value,
            "project_id": str(item.project_id) if item.project_id else None,
            "source_kind": item.source_kind.value,
            "version_ref": item.version_ref,
            "updated_at": item.updated_at.isoformat(),
            **(
                {"logical_id": item.metadata["logical_id"]} if "logical_id" in item.metadata else {}
            ),
        },
    )


def _name_matches(entity: Entity, row: SymbolRow) -> bool:
    """Hints and `backticked` names match any case; a name found in prose must match exactly, and
    an ALL_CAPS_NAME in prose can only mean a constant."""
    if entity.explicit:
        return True
    text = entity.text.rsplit(".", 1)[-1]
    if text != row.name:
        return False
    if text.isupper() and "_" in text:
        return row.kind in {"variable", "field"}
    return True


async def find_entity_symbols(
    store: ProjectStore,
    project_id: uuid.UUID,
    branch: str,
    scopes: Sequence[FileScope],
    entities: Sequence[Entity],
    *,
    limit: int = 80,
) -> dict[str, list[SymbolRow]]:
    """entity text -> the indexed symbols it names. The one definition of "this name exists",
    shared by the exact retriever and the coverage check so the two can never disagree about
    whether a named entity was found (a dotted name falls back to its last component)."""
    idents = [e for e in entities if e.kind == "identifier"]
    plain = [e for e in idents if "." not in e.text]
    dotted = [e for e in idents if "." in e.text]
    qualified_rows = await store.find_symbols(
        project_id,
        branch,
        sorted({e.text.lower() for e in dotted}),
        scopes=scopes,
        limit=limit,
        by_qualified=True,
    )
    qualified_hit = {r.qualified_name.lower() for r in qualified_rows}
    last_parts = {
        e.text.lower().rsplit(".", 1)[-1] for e in dotted if e.text.lower() not in qualified_hit
    }
    name_rows = await store.find_symbols(
        project_id,
        branch,
        sorted({e.text.lower() for e in plain} | last_parts),
        scopes=scopes,
        limit=limit,
    )
    found: dict[str, list[SymbolRow]] = {}
    for e in plain:
        found[e.text] = [
            r for r in name_rows if r.name.lower() == e.text.lower() and _name_matches(e, r)
        ]
    for e in dotted:
        lowered = e.text.lower()
        rows = [r for r in qualified_rows if r.qualified_name.lower() == lowered]
        if not rows:
            last = lowered.rsplit(".", 1)[-1]
            rows = [r for r in name_rows if r.name.lower() == last and _name_matches(e, r)]
        found[e.text] = rows
    return found


class ExactRetriever:
    """Entities named in the request that exist in the index: files by path, symbols by name."""

    name = "exact"
    source = Source.EXACT

    def __init__(self, projects: ProjectService) -> None:
        self._projects = projects

    async def retrieve(self, rc: RetrievalContext, limit: int) -> list[Candidate]:
        if rc.project is None or rc.branch is None:
            return []
        store = self._projects.store
        out: list[Candidate] = []
        paths = [e.text for e in rc.entities if e.kind == "path"]
        files, found = await asyncio.gather(
            store.find_files(rc.project.id, rc.branch, paths, scopes=rc.scopes),
            find_entity_symbols(
                store, rc.project.id, rc.branch, rc.scopes, rc.entities, limit=limit * 4
            ),
        )
        symbols = [row for rows in found.values() for row in rows]
        seen_files: set[str] = set()
        for frow in files:
            if frow.status == "deleted" or frow.path in seen_files:
                continue
            seen_files.add(frow.path)
            out.append(file_candidate(frow, Source.EXACT, len(out) + 1, 1.0))
        # prefer overlay rows over committed rows of the same symbol
        best: dict[str, SymbolRow] = {}
        for row in symbols:
            key = f"{row.path}::{row.qualified_name}"
            if key not in best or row.scope == FileScope.OVERLAY.value:
                best[key] = row
        ordered = sorted(
            best.values(), key=lambda r: (not r.exported, r.kind in {"variable", "section"}, r.path)
        )
        for row in ordered[:limit]:
            out.append(symbol_candidate(row, Source.EXACT, len(out) + 1, 1.0))
        return out[:limit]


class SymbolRetriever:
    """Symbols - and, when two of the question's words name them, files - echoing its *rare* words.

    Every question word is checked against the index first: a word that appears in the names of
    more than :attr:`GENERIC_ABOVE` symbols cannot tell them apart, so it is dropped rather than
    allowed to fill the slots, and the rarest words are searched first. Words are stemmed
    (``backticked`` meets ``backticks``) and each hit is confirmed against the symbol's name parts,
    so ``code`` no longer finds ``_decode_cursor``.
    """

    name = "symbol"
    source = Source.SYMBOL
    MIN_TERM = 4
    MAX_TERMS = 12
    GENERIC_ABOVE = 40
    PER_TERM = 6
    MAX_PATH_HITS = 3

    def __init__(self, projects: ProjectService) -> None:
        self._projects = projects

    async def retrieve(self, rc: RetrievalContext, limit: int) -> list[Candidate]:
        if rc.project is None or rc.branch is None:
            return []
        store = self._projects.store
        stems = [
            t
            for t in query_terms(rc.query.text, limit=self.MAX_TERMS * 2)
            if len(t) >= self.MIN_TERM
        ]
        if not stems:
            return []
        counts = await store.symbol_match_counts(rc.project.id, rc.branch, stems, scopes=rc.scopes)
        usable = sorted(
            (t for t in stems if 0 < counts.get(t, 0) <= self.GENERIC_ABOVE),
            key=lambda t: (counts[t], t),
        )[: self.MAX_TERMS]
        batches = await asyncio.gather(
            *(
                store.search_symbols(
                    rc.project.id, rc.branch, t, scopes=rc.scopes, limit=self.PER_TERM * 3
                )
                for t in usable
            )
        )
        scores: dict[str, float] = defaultdict(float)
        rows: dict[str, SymbolRow] = {}
        for term, found in zip(usable, batches, strict=True):
            weight = 1.0 / math.log2(2 + counts[term])  # rarer words say more
            for row in found:
                if row.kind in LEXICAL_SKIP_KINDS:
                    continue
                in_name = bool(covered_terms([term], tokenize(row.name))[0])
                if not in_name and not covered_terms([term], tokenize(row.qualified_name))[0]:
                    continue
                cid = f"symbol:{row.path}::{row.qualified_name}"
                scores[cid] += weight * (1.0 if in_name else 0.5)
                rows.setdefault(cid, row)
        ordered = sorted(rows, key=lambda c: (-scores[c], len(rows[c].qualified_name), c))
        files = await self._path_hits(rc.project.id, rc.branch, rc.scopes, stems)
        room = max(1, limit - len(files))
        out = [
            symbol_candidate(rows[cid], Source.SYMBOL, n, scores[cid])
            for n, cid in enumerate(ordered[:room], 1)
        ]
        out.extend(
            file_candidate(row, Source.SYMBOL, len(out) + n, score)
            for n, (row, score) in enumerate(files, 1)
        )
        return out[:limit]

    async def _path_hits(
        self,
        project_id: uuid.UUID,
        branch: str,
        scopes: tuple[FileScope, ...],
        stems: list[str],
    ) -> list[tuple[FileRow, float]]:
        """Files whose path mentions two of the question's words, or one that few paths share."""
        rows = await self._projects.store.search_paths(project_id, branch, stems, scopes=scopes)
        covered_by: dict[str, list[str]] = {}
        frequency: dict[str, int] = defaultdict(int)
        for row in rows:
            hit, _ = covered_terms(stems, tokenize(row.path.replace("/", " ").replace(".", " ")))
            covered_by[row.path] = hit
            for term in hit:
                frequency[term] += 1
        scored = [
            (row, float(len(hit)))
            for row in rows
            if (hit := covered_by[row.path])
            and (len(hit) >= 2 or all(frequency[t] <= self.MAX_PATH_HITS for t in hit))
        ]
        scored.sort(key=lambda t: (-t[1], len(t[0].path), t[0].path))
        return scored[: self.MAX_PATH_HITS]


class FtsRetriever:
    name = "fts"
    source = Source.FTS

    def __init__(
        self,
        knowledge: KnowledgeService,
        *,
        name: str = "fts",
        source: Source = Source.FTS,
        kinds: Sequence[str] | None = None,
        source_kinds: Sequence[SourceKind] | None = None,
        exclude_kinds: Sequence[str] | None = None,
    ) -> None:
        self._knowledge = knowledge
        self.name = name
        self.source = source
        self._kinds = kinds
        self._source_kinds = source_kinds
        self._exclude = exclude_kinds

    async def retrieve(self, rc: RetrievalContext, limit: int) -> list[Candidate]:
        hits = await self._knowledge.search_text(
            rc.or_text or rc.query.text,
            rc.search_scope,
            limit=limit,
            kinds=self._kinds,
            source_kinds=self._source_kinds,
            exclude_kinds=self._exclude,
        )
        return [knowledge_candidate(i, self.source, n, s) for n, (i, s) in enumerate(hits, 1)]


class VectorRetriever:
    name = "vector"
    source = Source.VECTOR

    def __init__(
        self,
        knowledge: KnowledgeService,
        *,
        name: str = "vector",
        source: Source = Source.VECTOR,
        kinds: Sequence[str] | None = None,
        source_kinds: Sequence[SourceKind] | None = None,
        exclude_kinds: Sequence[str] | None = None,
        min_similarity: float = 0.05,
    ) -> None:
        self._knowledge = knowledge
        self.name = name
        self.source = source
        self._kinds = kinds
        self._source_kinds = source_kinds
        self._exclude = exclude_kinds
        self._min = min_similarity

    async def retrieve(self, rc: RetrievalContext, limit: int) -> list[Candidate]:
        hits = await self._knowledge.search_vector(
            rc.query.text,
            rc.search_scope,
            limit=limit,
            kinds=self._kinds,
            source_kinds=self._source_kinds,
            exclude_kinds=self._exclude,
        )
        return [
            knowledge_candidate(i, self.source, n, s)
            for n, (i, s) in enumerate((h for h in hits if h[1] >= self._min), 1)
        ]


class MemoryRetriever:
    name = "memory"
    source = Source.MEMORY

    def __init__(self, memory: MemoryRepository, knowledge: KnowledgeService) -> None:
        self._memory = memory
        self._knowledge = knowledge

    async def retrieve(self, rc: RetrievalContext, limit: int) -> list[Candidate]:
        [embedding] = await self._knowledge.embedder.embed([rc.query.text])
        text_hits, vec_hits = await asyncio.gather(
            self._memory.search_text(rc.or_text or rc.query.text, rc.search_scope, limit=limit),
            self._memory.search_vector(embedding, rc.search_scope, limit=limit),
        )
        merged: dict[uuid.UUID, tuple[float, Candidate]] = {}
        for hits in (text_hits, vec_hits):
            for item, score in hits:
                if item.id in merged and merged[item.id][0] >= score:
                    continue
                merged[item.id] = (
                    score,
                    Candidate(
                        id=f"memory:{item.id}",
                        kind=CandidateKind.MEMORY,
                        title=item.content[:80],
                        content=item.content,
                        source=Source.MEMORY,
                        rank=1,
                        raw_score=score,
                        metadata={"scope": item.scope, "importance": item.importance},
                    ),
                )
        ordered = sorted(merged.values(), key=lambda t: (-t[0], t[1].id))[:limit]
        return [c.model_copy(update={"rank": n}) for n, (_, c) in enumerate(ordered, 1)]


class DecisionRetriever:
    name = "decision"
    source = Source.DECISION

    def __init__(self, decisions: DecisionRepository) -> None:
        self._decisions = decisions

    async def retrieve(self, rc: RetrievalContext, limit: int) -> list[Candidate]:
        hits = await self._decisions.search_text(
            rc.or_text or rc.query.text, rc.search_scope, limit=limit
        )
        return [
            Candidate(
                id=f"decision:{d.id}",
                kind=CandidateKind.DECISION,
                title=d.question,
                content=f"{d.selected}\n{d.rationale}".strip(),
                source=Source.DECISION,
                rank=n,
                raw_score=s,
                metadata={"status": d.status.value, "decider": d.decider},
            )
            for n, (d, s) in enumerate(hits, 1)
        ]


class GitHistoryRetriever:
    """Commits that touched the entity paths (or mention distinctive terms)."""

    name = "git_history"
    source = Source.GIT_HISTORY

    def __init__(self, projects: ProjectService) -> None:
        self._projects = projects

    async def retrieve(self, rc: RetrievalContext, limit: int) -> list[Candidate]:
        if rc.project is None:
            return []
        paths = [e.text for e in rc.entities if e.kind == "path"]
        seed_paths = list(rc.query.paths) + paths
        commits = []
        try:
            if seed_paths:
                commits += await self._projects.history(
                    rc.project.id, paths=seed_paths, limit=limit
                )
            for term in significant_terms(rc.query.text, limit=3):
                if len(term) >= 5:
                    commits += await self._projects.history(rc.project.id, grep=term, limit=5)
        except Exception:  # history is a best-effort enrichment; never fail retrieval over it
            return []
        out: list[Candidate] = []
        seen: set[str] = set()
        for commit in commits:
            if commit.sha in seen:
                continue
            seen.add(commit.sha)
            out.append(
                Candidate(
                    id=f"commit:{commit.sha}",
                    kind=CandidateKind.COMMIT,
                    title=commit.subject,
                    content=(
                        f"{commit.sha[:10]} {commit.date[:10]} {commit.author}: {commit.subject}"
                    ),
                    source=Source.GIT_HISTORY,
                    rank=len(out) + 1,
                    metadata={"sha": commit.sha, "date": commit.date, "author": commit.author},
                )
            )
        return out[:limit]


def default_scope(query: RetrievalQuery) -> SearchScope:
    vaults = {Vault.DEFAULT, Vault.PROJECT}
    if query.include_ephemeral:
        vaults.add(Vault.EPHEMERAL)
    return SearchScope(project_id=query.project_id, vaults=frozenset(vaults))


_WS = re.compile(r"\s+")


def normalise_text(text: str) -> str:
    return _WS.sub(" ", text.lower()).strip()
