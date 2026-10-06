"""Individual retrievers. Each produces ranked :class:`Candidate`s from one source."""

from __future__ import annotations

import asyncio
import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Protocol

from eios_domain.knowledge import KnowledgeItem, SourceKind
from eios_domain.project import FileScope
from eios_domain.vault import Vault
from eios_knowledge import KnowledgeService, SearchScope
from eios_knowledge.decision_repo import DecisionRepository
from eios_knowledge.memory_repo import MemoryRepository
from eios_knowledge.text import significant_terms
from eios_project_intelligence import Project, ProjectService
from eios_project_intelligence.store import FileRow, SymbolRow
from eios_retrieval.entities import Entity
from eios_retrieval.models import Candidate, CandidateKind, RetrievalQuery, Source


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
        identifiers = [e.text.lower() for e in rc.entities if e.kind == "identifier"]
        plain = sorted({n for n in identifiers if "." not in n})
        dotted = sorted({n for n in identifiers if "." in n})
        files, symbols, qualified = await asyncio.gather(
            store.find_files(rc.project.id, rc.branch, paths, scopes=rc.scopes),
            store.find_symbols(rc.project.id, rc.branch, plain, scopes=rc.scopes, limit=limit * 4),
            store.find_symbols(
                rc.project.id,
                rc.branch,
                dotted,
                scopes=rc.scopes,
                limit=limit * 4,
                by_qualified=True,
            ),
        )
        symbols = [*qualified, *symbols]
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
    """Substring matches of distinctive question words against symbol names."""

    name = "symbol"
    source = Source.SYMBOL
    MIN_TERM = 4

    def __init__(self, projects: ProjectService) -> None:
        self._projects = projects

    async def retrieve(self, rc: RetrievalContext, limit: int) -> list[Candidate]:
        if rc.project is None or rc.branch is None:
            return []
        terms = [t for t in significant_terms(rc.query.text, limit=8) if len(t) >= self.MIN_TERM]
        out: list[Candidate] = []
        seen: set[str] = set()
        results = await asyncio.gather(
            *(
                self._projects.store.search_symbols(
                    rc.project.id, rc.branch, t, scopes=rc.scopes, limit=6
                )
                for t in terms
            )
        )
        for rows in results:
            for row in rows:
                cid = f"symbol:{row.path}::{row.qualified_name}"
                if cid in seen or row.kind in {"section", "variable"}:
                    continue
                seen.add(cid)
                out.append(symbol_candidate(row, Source.SYMBOL, len(out) + 1))
        return out[:limit]


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
