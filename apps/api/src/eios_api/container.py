"""Explicit dependency container for the API (no module-level globals)."""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.ext.asyncio import AsyncEngine

from eios_core.logging import get_logger
from eios_core.settings import Settings
from eios_observability import (
    EventHub,
    PostgresEventStore,
    PostgresRunRepository,
    RunRecorder,
)

_log = get_logger("eios.api.container")


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
    background: BackgroundTasks = field(default_factory=BackgroundTasks)


def build_container(settings: Settings, engine: AsyncEngine) -> Container:
    hub = EventHub()
    runs = PostgresRunRepository(engine)
    events = PostgresEventStore(engine)
    return Container(
        settings=settings,
        engine=engine,
        runs=runs,
        events=events,
        hub=hub,
        recorder=RunRecorder(runs, events, hub),
    )
