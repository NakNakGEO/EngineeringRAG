"""Explicit dependency container shared by API, worker and MCP (no module-level globals)."""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncEngine

from eios_core.logging import get_logger
from eios_core.settings import Settings
from eios_jobs import PostgresJobQueue
from eios_knowledge import (
    DecisionRepository,
    EvidencePipeline,
    EvidenceRepository,
    HashingEmbedder,
    KnowledgeRepository,
    KnowledgeService,
    LocalBlobStore,
    MemoryRepository,
)
from eios_observability import (
    EventHub,
    PostgresEventStore,
    PostgresRunRepository,
    RunRecorder,
)
from eios_project_intelligence import (
    ApprovedWorkspaces,
    ProjectIndexer,
    ProjectService,
    ProjectStore,
)

_log = get_logger("eios.runtime")


class BackgroundTasks:
    """Tracks fire-and-forget tasks so they are logged on failure and cancelled on shutdown."""

    def __init__(self) -> None:
        self._tasks: set[asyncio.Task[Any]] = set()

    def spawn(self, coro: Coroutine[Any, Any, Any], *, name: str) -> asyncio.Task[Any]:
        task = asyncio.create_task(coro, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._done)
        return task

    def _done(self, task: asyncio.Task[Any]) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and (exc := task.exception()) is not None:
            _log.error("background_task_failed", task=task.get_name(), error=repr(exc))

    async def shutdown(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)


@dataclass
class Container:
    settings: Settings
    engine: AsyncEngine
    runs: PostgresRunRepository
    events: PostgresEventStore
    hub: EventHub
    recorder: RunRecorder
    blobs: LocalBlobStore
    knowledge: KnowledgeService
    evidence: EvidenceRepository
    pipeline: EvidencePipeline
    queue: PostgresJobQueue
    workspaces: ApprovedWorkspaces
    projects: ProjectService
    background: BackgroundTasks = field(default_factory=BackgroundTasks)


def build_container(settings: Settings, engine: AsyncEngine) -> Container:
    hub = EventHub()
    runs = PostgresRunRepository(engine)
    events = PostgresEventStore(engine)
    blobs = LocalBlobStore(settings.blob_dir)
    knowledge = KnowledgeService(
        KnowledgeRepository(engine),
        MemoryRepository(engine),
        DecisionRepository(engine),
        HashingEmbedder(),
    )
    evidence = EvidenceRepository(engine)
    queue = PostgresJobQueue(engine)
    recorder = RunRecorder(runs, events, hub)
    workspaces = ApprovedWorkspaces(settings.workspace_root_paths)
    store = ProjectStore(engine)
    indexer = ProjectIndexer(
        store,
        workspaces,
        max_file_bytes=settings.max_indexed_file_bytes,
        overlay_ttl=timedelta(seconds=settings.overlay_ttl_seconds),
    )
    return Container(
        settings=settings,
        engine=engine,
        runs=runs,
        events=events,
        hub=hub,
        recorder=recorder,
        blobs=blobs,
        knowledge=knowledge,
        evidence=evidence,
        pipeline=EvidencePipeline(
            evidence,
            knowledge,
            blobs,
            max_evidence_bytes=settings.max_evidence_bytes,
            default_ttl=timedelta(seconds=settings.ephemeral_ttl_seconds),
        ),
        queue=queue,
        workspaces=workspaces,
        projects=ProjectService(store, indexer, queue, recorder),
    )
