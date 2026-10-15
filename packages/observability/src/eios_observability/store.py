"""Event store and run repository contracts (ports) plus their paging models."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import Protocol

from pydantic import BaseModel

from eios_domain.events import EventDraft, EventEnvelope, EventStatus
from eios_domain.run import Run, RunStatus


class EventPage(BaseModel):
    items: list[EventEnvelope]
    next_cursor: int | None = None  # pass as ``after_seq`` to continue
    has_more: bool = False


class RunPage(BaseModel):
    items: list[Run]
    next_cursor: str | None = None


class EventStore(Protocol):
    async def append(self, draft: EventDraft) -> EventEnvelope: ...

    async def append_many(self, drafts: Sequence[EventDraft]) -> list[EventEnvelope]: ...

    async def list_events(
        self,
        run_id: uuid.UUID,
        *,
        after_seq: int = 0,
        limit: int = 100,
        types: Sequence[str] | None = None,
        status: EventStatus | None = None,
        span_id: uuid.UUID | None = None,
    ) -> EventPage: ...

    async def latest_seq(self, run_id: uuid.UUID) -> int: ...


class RunRepository(Protocol):
    async def create(self, run: Run, *, budget: dict[str, object] | None = None) -> Run: ...

    async def get(self, run_id: uuid.UUID) -> Run | None: ...

    async def save_transition(self, run: Run, *, expected: RunStatus) -> Run: ...

    async def list_runs(
        self,
        *,
        limit: int = 50,
        cursor: str | None = None,
        status: RunStatus | None = None,
        created_before: datetime | None = None,
    ) -> RunPage: ...
