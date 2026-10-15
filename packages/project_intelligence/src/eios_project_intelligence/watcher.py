"""Keeps the index live: polls registered projects and queues a sync when their files change.

A change is anything that moves the project's worktree signature (HEAD, or the size/mtime of any
tracked or untracked file). The first look at a project only records a baseline, so starting the
worker never causes a sync storm. Syncs are queued through the normal job queue, so they show up as
``project_sync`` runs with ``FILE_CHANGED`` events, which is what the live feed reads.
"""

from __future__ import annotations

import asyncio
import time
import uuid

from eios_core.logging import get_logger
from eios_project_intelligence.service import ProjectService

_log = get_logger("eios.watcher")


class ProjectWatcher:
    def __init__(
        self, projects: ProjectService, *, interval: float = 5.0, min_gap: float = 20.0
    ) -> None:
        self._projects = projects
        self._interval = interval
        self._min_gap = min_gap
        self._seen: dict[uuid.UUID, str] = {}
        self._last_sync: dict[uuid.UUID, float] = {}

    async def poll_once(self) -> list[uuid.UUID]:
        """Check every project; returns the ids a sync was queued for."""
        queued: list[uuid.UUID] = []
        for project in await self._projects.list_projects():
            try:
                signature = await self._projects.worktree_signature(project)
            except Exception as exc:  # unreadable project: skip it this round, never stop the loop
                _log.warning("watch_signature_failed", project=project.name, error=repr(exc)[:200])
                continue
            previous = self._seen.get(project.id)
            if previous is None:
                self._seen[project.id] = signature  # baseline
                continue
            if signature == previous:
                continue
            last = self._last_sync.get(project.id)  # monotonic time; unset = never synced
            if last is not None and time.monotonic() - last < self._min_gap:
                continue  # keep the new signature unseen so the change is picked up next round
            self._seen[project.id] = signature
            self._last_sync[project.id] = time.monotonic()
            try:
                await self._projects.request_sync(project.id)
            except Exception as exc:
                _log.warning("watch_sync_failed", project=project.name, error=repr(exc)[:200])
                continue
            queued.append(project.id)
            _log.info("watch_change_detected", project=project.name)
        return queued

    async def run_forever(self, stop: asyncio.Event) -> None:
        if self._interval <= 0:
            return
        while not stop.is_set():
            started = time.monotonic()
            try:
                await self.poll_once()
            except Exception as exc:
                _log.error("watch_poll_failed", error=repr(exc)[:200])
            # a slow tree (a network or Windows-mounted drive) is polled less often: rest at
            # least twice as long as the pass took, so watching never becomes the load itself
            pause = max(self._interval, 2 * (time.monotonic() - started))
            try:
                await asyncio.wait_for(stop.wait(), pause)
            except TimeoutError:
                continue
