"""Runs, their events, the derived execution graph and the live SSE stream."""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from typing import Annotated, Literal

from fastapi import APIRouter, Header, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from eios_api.deps import ContainerDep
from eios_domain.errors import DomainError
from eios_domain.events import EventEnvelope, EventStatus
from eios_domain.run import Run, RunStatus
from eios_observability.demo import run_demo
from eios_observability.graph import RunGraph, build_graph
from eios_observability.store import EventPage, RunPage
from eios_observability.stream import follow_events

router = APIRouter(prefix="/runs", tags=["runs"])

GRAPH_EVENT_LIMIT = 20_000


class CreateRunRequest(BaseModel):
    kind: Literal["demo"] = "demo"
    goal: str = Field(default="", max_length=4000)
    project_id: uuid.UUID | None = None
    fail: bool = Field(default=False, description="demo only: end the run with RUN_FAILED")
    step_delay_ms: int = Field(default=0, ge=0, le=5000, description="demo only: pause per step")


class CreateRunResponse(BaseModel):
    run_id: uuid.UUID
    trace_id: uuid.UUID


@router.post("", status_code=status.HTTP_202_ACCEPTED, response_model=CreateRunResponse)
async def create_run(body: CreateRunRequest, container: ContainerDep) -> CreateRunResponse:
    """Start a run. Phase 1 supports the ``demo`` kind; workflow runs arrive with Phase 7."""
    ctx = await container.recorder.start_run(
        kind=body.kind, goal=body.goal, project_id=body.project_id
    )
    container.background.spawn(
        run_demo(container.recorder, ctx, fail=body.fail, step_delay=body.step_delay_ms / 1000),
        name=f"demo-run-{ctx.run_id}",
    )
    return CreateRunResponse(run_id=ctx.run_id, trace_id=ctx.trace_id)


@router.get("", response_model=RunPage)
async def list_runs(
    container: ContainerDep,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    cursor: str | None = None,
    run_status: Annotated[RunStatus | None, Query(alias="status")] = None,
) -> RunPage:
    try:
        return await container.runs.list_runs(limit=limit, cursor=cursor, status=run_status)
    except DomainError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc


async def _require_run(container: ContainerDep, run_id: uuid.UUID) -> Run:
    run = await container.runs.get(run_id)
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "run not found")
    return run


@router.get("/{run_id}", response_model=Run)
async def get_run(run_id: uuid.UUID, container: ContainerDep) -> Run:
    return await _require_run(container, run_id)


@router.get("/{run_id}/events", response_model=EventPage)
async def list_events(
    run_id: uuid.UUID,
    container: ContainerDep,
    after_seq: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    type_: Annotated[list[str] | None, Query(alias="type")] = None,
    event_status: Annotated[EventStatus | None, Query(alias="status")] = None,
    span_id: uuid.UUID | None = None,
) -> EventPage:
    await _require_run(container, run_id)
    return await container.events.list_events(
        run_id,
        after_seq=after_seq,
        limit=limit,
        types=type_,
        status=event_status,
        span_id=span_id,
    )


@router.get("/{run_id}/graph", response_model=RunGraph)
async def get_graph(run_id: uuid.UUID, container: ContainerDep) -> RunGraph:
    await _require_run(container, run_id)
    page = await container.events.list_events(run_id, limit=1000)
    events: list[EventEnvelope] = list(page.items)
    while page.has_more and len(events) < GRAPH_EVENT_LIMIT:
        page = await container.events.list_events(run_id, after_seq=events[-1].seq, limit=1000)
        events.extend(page.items)
    return build_graph(run_id, events, truncated=page.has_more)


def _sse(event: EventEnvelope) -> str:
    payload = json.dumps(event.model_dump(mode="json"), separators=(",", ":"))
    return f"id: {event.seq}\nevent: {event.type}\ndata: {payload}\n\n"


@router.get("/{run_id}/stream")
async def stream_events(
    run_id: uuid.UUID,
    request: Request,
    container: ContainerDep,
    after_seq: Annotated[int, Query(ge=0)] = 0,
    last_event_id: Annotated[str | None, Header()] = None,
) -> StreamingResponse:
    """Server-Sent Events. Resume with ``Last-Event-ID`` (or ``?after_seq=``): ids are ``seq``."""
    await _require_run(container, run_id)
    cursor = after_seq
    if last_event_id and last_event_id.isdigit():
        cursor = max(cursor, int(last_event_id))
    settings = container.settings

    async def generate() -> AsyncIterator[str]:
        yield ": stream open\n\n"
        async for item in follow_events(
            events=container.events,
            runs=container.runs,
            hub=container.hub,
            run_id=run_id,
            after_seq=cursor,
            poll_interval=settings.sse_poll_interval_seconds,
            max_seconds=settings.sse_max_seconds,
            keepalive_seconds=settings.sse_keepalive_seconds,
        ):
            if await request.is_disconnected():
                return
            yield ": keepalive\n\n" if item is None else _sse(item)
        yield "event: end\ndata: {}\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
