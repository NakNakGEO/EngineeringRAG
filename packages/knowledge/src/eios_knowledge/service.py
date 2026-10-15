"""Application service: embeds, stores and emits events for knowledge, memory and decisions."""

from __future__ import annotations

from collections.abc import Sequence

from eios_domain.events import EventType
from eios_domain.knowledge import (
    Decision,
    DecisionCreate,
    KnowledgeItem,
    KnowledgeItemCreate,
    MemoryCreate,
    SourceKind,
)
from eios_knowledge.decision_repo import DecisionRepository
from eios_knowledge.embeddings import EmbeddingProvider
from eios_knowledge.knowledge_repo import KnowledgeRepository
from eios_knowledge.memory_repo import MemoryItem, MemoryRepository
from eios_knowledge.scope import SearchScope
from eios_observability import RunContext


class KnowledgeService:
    def __init__(
        self,
        knowledge: KnowledgeRepository,
        memory: MemoryRepository,
        decisions: DecisionRepository,
        embedder: EmbeddingProvider,
    ) -> None:
        self.knowledge = knowledge
        self.memory = memory
        self.decisions = decisions
        self.embedder = embedder

    async def add_item(
        self, create: KnowledgeItemCreate, ctx: RunContext | None = None
    ) -> KnowledgeItem:
        [embedding] = await self.embedder.embed([f"{create.title}\n{create.content}"])
        item = await self.knowledge.add(
            create, embedding=embedding, embedding_model=self.embedder.model_id
        )
        if ctx is not None:
            await ctx.emit(
                EventType.KNOWLEDGE_UPDATED,
                f"knowledge added: {item.title[:80]}",
                data={
                    "item_id": str(item.id),
                    "vault": item.vault.value,
                    "trust": item.trust.value,
                    "health": item.health.value,
                },
            )
        return item

    async def add_memory(self, create: MemoryCreate, ctx: RunContext | None = None) -> MemoryItem:
        [embedding] = await self.embedder.embed([create.content])
        item = await self.memory.add(
            create, embedding=embedding, embedding_model=self.embedder.model_id
        )
        if ctx is not None:
            await ctx.emit(
                EventType.KNOWLEDGE_UPDATED,
                "memory added",
                data={"memory_id": str(item.id), "vault": item.vault.value},
            )
        return item

    async def record_decision(
        self, create: DecisionCreate, ctx: RunContext | None = None
    ) -> Decision:
        decision = await self.decisions.create(create)
        if ctx is not None:
            await ctx.emit(
                EventType.KNOWLEDGE_UPDATED,
                f"decision recorded: {decision.question[:80]}",
                data={"decision_id": str(decision.id), "status": decision.status.value},
            )
        return decision

    async def search_text(
        self,
        query: str,
        scope: SearchScope,
        *,
        limit: int = 20,
        kinds: Sequence[str] | None = None,
        source_kinds: Sequence[SourceKind] | None = None,
        exclude_kinds: Sequence[str] | None = None,
    ) -> list[tuple[KnowledgeItem, float]]:
        return await self.knowledge.search_text(
            query,
            scope,
            limit=limit,
            kinds=kinds,
            source_kinds=source_kinds,
            exclude_kinds=exclude_kinds,
        )

    async def search_vector(
        self,
        query: str,
        scope: SearchScope,
        *,
        limit: int = 20,
        kinds: Sequence[str] | None = None,
        source_kinds: Sequence[SourceKind] | None = None,
        exclude_kinds: Sequence[str] | None = None,
    ) -> list[tuple[KnowledgeItem, float]]:
        [embedding] = await self.embedder.embed([query])
        return await self.knowledge.search_vector(
            embedding,
            scope,
            limit=limit,
            kinds=kinds,
            source_kinds=source_kinds,
            exclude_kinds=exclude_kinds,
        )
