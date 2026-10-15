"""Live activity: the newest events across every run, for the "Start here" feed."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any

import sqlalchemy as sa
from fastapi import APIRouter, Query

from eios_api.deps import ContainerDep
from eios_storage.tables.observability import event as event_t
from eios_storage.tables.platform import run as run_t

router = APIRouter(tags=["activity"])


@router.get("/activity")
async def activity(
    container: ContainerDep,
    after: datetime | None = None,
    project_id: uuid.UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=300)] = 80,
) -> list[dict[str, Any]]:
    """Newest first. Pass the newest ``timestamp`` as ``after`` to get only what is new."""
    stmt = (
        sa.select(
            event_t.c.event_id,
            event_t.c.run_id,
            event_t.c.type,
            event_t.c.timestamp,
            event_t.c.status,
            event_t.c.summary,
            event_t.c.data,
            event_t.c.actor_id,
            run_t.c.kind.label("run_kind"),
            run_t.c.project_id,
            run_t.c.goal,
        )
        .select_from(event_t.join(run_t, run_t.c.id == event_t.c.run_id))
        .order_by(event_t.c.timestamp.desc(), event_t.c.seq.desc())
        .limit(limit)
    )
    if after is not None:
        stmt = stmt.where(event_t.c.timestamp > after)
    if project_id is not None:
        stmt = stmt.where(run_t.c.project_id == project_id)
    async with container.engine.connect() as conn:
        rows = (await conn.execute(stmt)).all()
    return [
        {
            "event_id": str(r.event_id),
            "run_id": str(r.run_id),
            "run_kind": r.run_kind,
            "project_id": str(r.project_id) if r.project_id else None,
            "type": r.type,
            "timestamp": r.timestamp.isoformat(),
            "status": r.status,
            "summary": r.summary,
            "actor": r.actor_id,
            "data": r.data or {},
            "goal": r.goal,
        }
        for r in rows
    ]
