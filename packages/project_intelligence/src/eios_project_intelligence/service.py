"""Project intelligence application service."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from eios_domain.errors import NotFoundError
from eios_domain.events import EventType
from eios_domain.project import FileScope
from eios_jobs import Job, JobQueue
from eios_observability import RunRecorder
from eios_project_intelligence.indexer import BootstrapResult, ProjectIndexer, SyncResult
from eios_project_intelligence.languages import PARSED_LANGUAGES
from eios_project_intelligence.store import FileRow, Project, ProjectStore, SymbolRow

SYNC_JOB = "project.sync"


class ProjectService:
    def __init__(
        self,
        store: ProjectStore,
        indexer: ProjectIndexer,
        queue: JobQueue,
        recorder: RunRecorder,
    ) -> None:
        self.store = store
        self._indexer = indexer
        self._queue = queue
        self._recorder = recorder

    async def bootstrap(self, path: str | Path) -> BootstrapResult:
        return await self._indexer.bootstrap(path)

    async def require(self, project_id: uuid.UUID) -> Project:
        project = await self.store.get_project(project_id)
        if project is None:
            raise NotFoundError(f"project {project_id} not found")
        return project

    async def sync_now(self, project_id: uuid.UUID) -> tuple[SyncResult, uuid.UUID]:
        """Synchronise inline, recorded as a ``project_sync`` run. Returns (result, run_id)."""
        await self.require(project_id)
        ctx = await self._recorder.start_run(
            kind="project_sync", goal=f"sync project {project_id}", project_id=project_id
        )
        try:
            result = await self._indexer.sync(project_id, ctx=ctx)
        except Exception as exc:
            await self._recorder.fail_run(ctx, f"{type(exc).__name__}: {exc}")
            raise
        await self._recorder.complete_run(ctx, f"synced {result.branch}@{result.commit[:8]}")
        return result, ctx.run_id

    async def request_sync(self, project_id: uuid.UUID) -> tuple[Job, uuid.UUID]:
        """Queue a background sync (deduplicated while one is already queued or running)."""
        await self.require(project_id)
        existing = await self._queue.find_active(SYNC_JOB, {"project_id": str(project_id)})
        if existing is not None and existing.run_id is not None:
            return existing, existing.run_id
        ctx = await self._recorder.start_run(
            kind="project_sync", goal=f"sync project {project_id}", project_id=project_id
        )
        job = await self._queue.enqueue(
            SYNC_JOB, {"project_id": str(project_id), "run_id": str(ctx.run_id)}, run_id=ctx.run_id
        )
        return job, ctx.run_id

    async def handle_sync_job(self, job: Job) -> dict[str, Any]:
        """Worker handler for ``project.sync`` jobs."""
        project_id = uuid.UUID(str(job.payload["project_id"]))
        run_id = uuid.UUID(str(job.payload["run_id"]))
        ctx = await self._recorder.resume(run_id)
        if job.attempts > 1:
            await ctx.emit(EventType.RETRY_STARTED, f"sync attempt {job.attempts}")
        try:
            result = await self._indexer.sync(project_id, ctx=ctx)
        except Exception as exc:
            if job.attempts >= job.max_attempts:
                await self._recorder.fail_run(ctx, f"{type(exc).__name__}: {exc}")
            raise
        await self._recorder.complete_run(ctx, f"synced {result.branch}@{result.commit[:8]}")
        return {
            "branch": result.branch,
            "commit": result.commit,
            "changed": len(result.changed),
            "removed": len(result.removed),
            "overlay": len(result.overlay),
        }

    # -- queries ----------------------------------------------------------------------------
    async def effective_branch(self, project: Project, branch: str | None) -> str:
        resolved = branch or project.last_branch
        if resolved is None:
            raise NotFoundError("project has not been indexed yet")
        return resolved

    async def list_files(
        self,
        project_id: uuid.UUID,
        *,
        branch: str | None = None,
        scope: FileScope = FileScope.COMMITTED,
        after_path: str | None = None,
        limit: int = 200,
        language: str | None = None,
        path_prefix: str | None = None,
    ) -> list[FileRow]:
        project = await self.require(project_id)
        return await self.store.list_files(
            project_id,
            await self.effective_branch(project, branch),
            scope=scope,
            after_path=after_path,
            limit=limit,
            language=language,
            path_prefix=path_prefix,
        )

    async def search_symbols(
        self,
        project_id: uuid.UUID,
        query: str,
        *,
        branch: str | None = None,
        include_overlay: bool = False,
        limit: int = 50,
    ) -> list[SymbolRow]:
        project = await self.require(project_id)
        scopes: Sequence[FileScope] = (
            (FileScope.COMMITTED, FileScope.OVERLAY) if include_overlay else (FileScope.COMMITTED,)
        )
        return await self.store.search_symbols(
            project_id,
            await self.effective_branch(project, branch),
            query,
            scopes=scopes,
            limit=limit,
        )

    async def neighborhood(
        self,
        project_id: uuid.UUID,
        key: str,
        *,
        branch: str | None = None,
        include_overlay: bool = False,
        limit: int = 200,
    ) -> dict[str, Any]:
        project = await self.require(project_id)
        scopes: Sequence[FileScope] = (
            (FileScope.COMMITTED, FileScope.OVERLAY) if include_overlay else (FileScope.COMMITTED,)
        )
        return await self.store.neighborhood(
            project_id,
            await self.effective_branch(project, branch),
            key,
            scopes=scopes,
            limit=limit,
        )

    async def semantic_coverage(
        self, project_id: uuid.UUID, *, branch: str | None = None
    ) -> dict[str, Any]:
        """Foundation for coverage-aware retrieval: how much of the project is understood."""
        project = await self.require(project_id)
        resolved = await self.effective_branch(project, branch)
        counts = await self.store.coverage_counts(project_id, resolved)
        files_total = sum(counts["languages"].values())
        parsable = sum(n for lang, n in counts["languages"].items() if lang in PARSED_LANGUAGES)
        return {
            "branch": resolved,
            "files_total": files_total,
            "parsable_files": parsable,
            "files_with_symbols": counts["files_with_symbols"],
            "files_with_knowledge": counts["files_with_knowledge"],
            "symbol_coverage": counts["files_with_symbols"] / parsable if parsable else 0.0,
            "knowledge_coverage": counts["files_with_knowledge"] / files_total
            if files_total
            else 0.0,
            "parse_errors": counts["parse_errors"],
            "languages": counts["languages"],
        }
