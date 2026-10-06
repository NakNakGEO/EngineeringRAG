"""The Run aggregate: one goal-directed execution, identified by a trace."""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from eios_domain.errors import InvalidTransitionError
from eios_domain.ids import new_id, utcnow


class RunStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def is_terminal(self) -> bool:
        return self in {RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED}


_ALLOWED: dict[RunStatus, frozenset[RunStatus]] = {
    RunStatus.PENDING: frozenset({RunStatus.RUNNING, RunStatus.CANCELLED, RunStatus.FAILED}),
    RunStatus.RUNNING: frozenset({RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED}),
    RunStatus.COMPLETED: frozenset(),
    RunStatus.FAILED: frozenset(),
    RunStatus.CANCELLED: frozenset(),
}


class Run(BaseModel):
    """Immutable value; state transitions return a new instance and enforce the lifecycle."""

    model_config = ConfigDict(frozen=True)

    id: uuid.UUID = Field(default_factory=new_id)
    trace_id: uuid.UUID = Field(default_factory=new_id)
    kind: str = Field(default="generic", min_length=1, max_length=100)
    goal: str = Field(default="", max_length=4000)
    status: RunStatus = RunStatus.PENDING
    project_id: uuid.UUID | None = None
    created_at: datetime = Field(default_factory=utcnow)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    def _transition(self, target: RunStatus, **changes: Any) -> Run:
        if target not in _ALLOWED[self.status]:
            raise InvalidTransitionError(f"run {self.id}: {self.status} -> {target} is not allowed")
        return self.model_copy(update={"status": target, **changes})

    def start(self) -> Run:
        return self._transition(RunStatus.RUNNING, started_at=utcnow())

    def complete(self) -> Run:
        return self._transition(RunStatus.COMPLETED, finished_at=utcnow())

    def fail(self, error: str) -> Run:
        return self._transition(RunStatus.FAILED, finished_at=utcnow(), error=error[:2000])

    def cancel(self) -> Run:
        return self._transition(RunStatus.CANCELLED, finished_at=utcnow())
