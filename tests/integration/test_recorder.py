from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from eios_domain.budget import ExecutionBudget
from eios_domain.errors import BudgetExceededError
from eios_domain.events import ActorType, EventStatus, EventType
from eios_domain.run import RunStatus
from eios_observability import PostgresEventStore, PostgresRunRepository, RunRecorder
from eios_observability.demo import run_demo
from eios_observability.graph import build_graph

pytestmark = pytest.mark.integration


def _recorder(db: AsyncEngine) -> tuple[RunRecorder, PostgresEventStore, PostgresRunRepository]:
    runs, events = PostgresRunRepository(db), PostgresEventStore(db)
    return RunRecorder(runs, events), events, runs


async def _all(events: PostgresEventStore, run_id):  # type: ignore[no-untyped-def]
    return (await events.list_events(run_id, limit=1000)).items


async def test_context_manager_completes_run(db: AsyncEngine) -> None:
    recorder, events, runs = _recorder(db)
    async with recorder.run(kind="t", goal="g") as ctx:
        await ctx.emit(EventType.GOAL_CLASSIFIED, "x")
    stored = await runs.get(ctx.run_id)
    assert stored is not None and stored.status is RunStatus.COMPLETED
    types = [e.type for e in await _all(events, ctx.run_id)]
    assert types == [EventType.RUN_STARTED, EventType.GOAL_CLASSIFIED, EventType.RUN_COMPLETED]


async def test_exception_fails_run_and_reraises(db: AsyncEngine) -> None:
    recorder, events, runs = _recorder(db)
    with pytest.raises(RuntimeError, match="boom"):
        async with recorder.run(kind="t") as ctx:
            raise RuntimeError("boom")
    stored = await runs.get(ctx.run_id)
    assert stored is not None and stored.status is RunStatus.FAILED
    assert stored.error is not None and "boom" in stored.error
    last = (await _all(events, ctx.run_id))[-1]
    assert last.type == EventType.RUN_FAILED and last.status is EventStatus.FAILED


async def test_budget_overrun_is_recorded_then_raised(db: AsyncEngine) -> None:
    recorder, events, _ = _recorder(db)
    ctx = await recorder.start_run(kind="t", budget=ExecutionBudget(max_tool_calls=1))
    await ctx.charge("tool_calls")
    with pytest.raises(BudgetExceededError):
        await ctx.charge("tool_calls")
    denied = [e for e in await _all(events, ctx.run_id) if e.type == EventType.BUDGET_EXCEEDED]
    assert len(denied) == 1 and denied[0].status is EventStatus.DENIED
    assert denied[0].data["kind"] == "tool_calls"


async def test_demo_run_produces_inspectable_timeline_and_graph(db: AsyncEngine) -> None:
    recorder, events, runs = _recorder(db)
    ctx = await recorder.start_run(kind="demo", goal="demo goal")
    await run_demo(recorder, ctx)

    timeline = await _all(events, ctx.run_id)
    assert [e.seq for e in timeline] == list(range(1, len(timeline) + 1))
    assert timeline[0].type == EventType.RUN_STARTED
    assert timeline[-1].type == EventType.RUN_COMPLETED
    assert {e.trace_id for e in timeline} == {ctx.trace_id}
    assert all(e.timestamp.tzinfo is not None for e in timeline)
    assert [e.timestamp for e in timeline] == sorted(e.timestamp for e in timeline)
    present = {e.type for e in timeline}
    assert {
        EventType.CONTEXT_REQUESTED, EventType.KNOWLEDGE_HIT, EventType.AGENT_SELECTED,
        EventType.POLICY_ALLOWED, EventType.TOOL_STARTED, EventType.TOOL_COMPLETED,
        EventType.LLM_COMPLETED, EventType.EVIDENCE_CREATED,
    } <= present  # fmt: skip

    # parent span links form a tree rooted at the run's root span
    graph = build_graph(ctx.run_id, timeline)
    roots = [n for n in graph.nodes if n.parent_span_id is None]
    assert len(roots) == 1 and roots[0].span_id == ctx.span_id
    assert any(n.actor_type == ActorType.TOOL.value for n in graph.nodes)
    stored = await runs.get(ctx.run_id)
    assert stored is not None and stored.status is RunStatus.COMPLETED


async def test_demo_failure_path_ends_with_run_failed(db: AsyncEngine) -> None:
    recorder, events, runs = _recorder(db)
    ctx = await recorder.start_run(kind="demo")
    await run_demo(recorder, ctx, fail=True)
    assert (await _all(events, ctx.run_id))[-1].type == EventType.RUN_FAILED
    stored = await runs.get(ctx.run_id)
    assert stored is not None and stored.status is RunStatus.FAILED


async def test_run_listing_is_cursor_paginated_newest_first(db: AsyncEngine) -> None:
    recorder, _, runs = _recorder(db)
    ids = []
    for _ in range(5):
        ctx = await recorder.start_run(kind="t")
        ids.append(ctx.run_id)
    first = await runs.list_runs(limit=2)
    second = await runs.list_runs(limit=2, cursor=first.next_cursor)
    third = await runs.list_runs(limit=2, cursor=second.next_cursor)
    got = [r.id for r in first.items + second.items + third.items]
    assert got == list(reversed(ids)) and third.next_cursor is None
    running = await runs.list_runs(status=RunStatus.RUNNING)
    assert len(running.items) == 5
