"""Run recording: the producer-side API used by every component to emit observable events."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from eios_domain.budget import BudgetKind, BudgetTracker, ExecutionBudget
from eios_domain.errors import BudgetExceededError, NotFoundError
from eios_domain.events import (
    ActorType,
    EventDraft,
    EventEnvelope,
    EventStatus,
    EventType,
)
from eios_domain.ids import new_id
from eios_domain.run import Run, RunStatus
from eios_observability.hub import EventHub
from eios_observability.store import EventStore, RunRepository


class RunContext:
    """A handle on one span of a run. Cheap to create; ``child()`` opens a nested span."""

    def __init__(
        self,
        recorder: RunRecorder,
        *,
        run_id: uuid.UUID,
        trace_id: uuid.UUID,
        span_id: uuid.UUID,
        parent_span_id: uuid.UUID | None,
        budget: BudgetTracker,
        actor_type: ActorType = ActorType.SYSTEM,
        actor_id: str = "eios",
    ) -> None:
        self._recorder = recorder
        self.run_id = run_id
        self.trace_id = trace_id
        self.span_id = span_id
        self.parent_span_id = parent_span_id
        self.budget = budget
        self.actor_type = actor_type
        self.actor_id = actor_id

    async def emit(
        self,
        type_: EventType,
        summary: str = "",
        *,
        status: EventStatus = EventStatus.COMPLETED,
        data: dict[str, Any] | None = None,
        actor_type: ActorType | None = None,
        actor_id: str | None = None,
    ) -> EventEnvelope:
        return await self._recorder.append(
            EventDraft(
                run_id=self.run_id,
                trace_id=self.trace_id,
                span_id=self.span_id,
                parent_span_id=self.parent_span_id,
                type=type_,
                actor_type=actor_type or self.actor_type,
                actor_id=actor_id or self.actor_id,
                status=status,
                summary=summary[:500],
                data=data or {},
            )
        )

    def child(
        self, *, actor_type: ActorType | None = None, actor_id: str | None = None
    ) -> RunContext:
        return RunContext(
            self._recorder,
            run_id=self.run_id,
            trace_id=self.trace_id,
            span_id=new_id(),
            parent_span_id=self.span_id,
            budget=self.budget,
            actor_type=actor_type or self.actor_type,
            actor_id=actor_id or self.actor_id,
        )

    @asynccontextmanager
    async def span(
        self, *, actor_type: ActorType | None = None, actor_id: str | None = None
    ) -> AsyncIterator[RunContext]:
        yield self.child(actor_type=actor_type, actor_id=actor_id)

    async def charge(self, kind: BudgetKind, amount: float = 1) -> None:
        """Charge the run budget; an overrun is recorded as an event before it propagates."""
        try:
            self.budget.charge(kind, amount)
        except BudgetExceededError as exc:
            await self.emit(
                EventType.BUDGET_EXCEEDED,
                str(exc),
                status=EventStatus.DENIED,
                data={"kind": exc.kind, "limit": exc.limit, "used": exc.used},
            )
            raise


class RunRecorder:
    def __init__(
        self, runs: RunRepository, events: EventStore, hub: EventHub | None = None
    ) -> None:
        self._runs = runs
        self._events = events
        self._hub = hub or EventHub()

    @property
    def hub(self) -> EventHub:
        return self._hub

    async def append(self, draft: EventDraft) -> EventEnvelope:
        envelope = await self._events.append(draft)
        self._hub.notify(draft.run_id)
        return envelope

    async def start_run(
        self,
        *,
        kind: str,
        goal: str = "",
        project_id: uuid.UUID | None = None,
        metadata: dict[str, Any] | None = None,
        budget: ExecutionBudget | None = None,
        actor_type: ActorType = ActorType.SYSTEM,
        actor_id: str = "eios",
    ) -> RunContext:
        """Create the run, mark it RUNNING and emit RUN_STARTED. Returns the root span context."""
        run = Run(kind=kind, goal=goal, project_id=project_id, metadata=metadata or {})
        budget = budget or ExecutionBudget()
        await self._runs.create(run, budget=budget.model_dump())
        running = run.start()
        await self._runs.save_transition(running, expected=RunStatus.PENDING)
        ctx = RunContext(
            self,
            run_id=run.id,
            trace_id=run.trace_id,
            span_id=new_id(),
            parent_span_id=None,
            budget=BudgetTracker(budget),
            actor_type=actor_type,
            actor_id=actor_id,
        )
        await ctx.emit(
            EventType.RUN_STARTED,
            f"run started: {kind}",
            status=EventStatus.STARTED,
            data={
                "kind": kind,
                "goal": goal[:500],
                "project_id": str(project_id) if project_id else None,
            },
        )
        return ctx

    async def resume(self, run_id: uuid.UUID) -> RunContext:
        """Re-attach to an existing RUNNING run (e.g. from a worker) with a new root-level span."""
        run = await self._runs.get(run_id)
        if run is None:
            raise NotFoundError(f"run {run_id} does not exist")
        return RunContext(
            self,
            run_id=run.id,
            trace_id=run.trace_id,
            span_id=new_id(),
            parent_span_id=None,
            budget=BudgetTracker(),
        )

    async def complete_run(self, ctx: RunContext, summary: str = "run completed") -> None:
        run = await self._runs.get(ctx.run_id)
        if run is None:
            raise NotFoundError(f"run {ctx.run_id} does not exist")
        await self._runs.save_transition(run.complete(), expected=run.status)
        await ctx.emit(
            EventType.RUN_COMPLETED,
            summary,
            data={"usage": ctx.budget.usage},
        )

    async def fail_run(self, ctx: RunContext, error: str) -> None:
        run = await self._runs.get(ctx.run_id)
        if run is None:
            raise NotFoundError(f"run {ctx.run_id} does not exist")
        await self._runs.save_transition(run.fail(error), expected=run.status)
        await ctx.emit(
            EventType.RUN_FAILED,
            error[:500],
            status=EventStatus.FAILED,
            data={"error": error[:2000], "usage": ctx.budget.usage},
        )

    @asynccontextmanager
    async def run(self, **kwargs: Any) -> AsyncIterator[RunContext]:
        """``async with recorder.run(kind=...) as ctx:`` completes or fails the run on exit."""
        ctx = await self.start_run(**kwargs)
        try:
            yield ctx
        except BaseException as exc:
            await self.fail_run(ctx, f"{type(exc).__name__}: {exc}")
            raise
        else:
            await self.complete_run(ctx)
