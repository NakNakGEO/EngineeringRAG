"""SQL persistence for workflow runs and node runs (optimistic concurrency on ``version``)."""

from __future__ import annotations

import uuid
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from eios_domain.ids import utcnow
from eios_domain.policy import Risk
from eios_domain.workflow import (
    NodeRun,
    NodeStatus,
    Stage,
    TeamSelection,
    WorkflowDefinition,
    WorkflowRun,
    WorkflowStatus,
)
from eios_storage.tables.workflow import node_run as node_t
from eios_storage.tables.workflow import run as run_t


class ConcurrentUpdateError(Exception):
    """Another writer changed the workflow since it was read."""


def _node(row: Any) -> NodeRun:
    m = row._mapping
    return NodeRun(
        id=m["id"],
        workflow_id=m["workflow_id"],
        node_id=m["node_id"],
        stage=Stage(m["stage"]),
        attempt=m["attempt"],
        status=NodeStatus(m["status"]),
        started_at=m["started_at"],
        finished_at=m["finished_at"],
        reporter=m["reporter"],
        outputs=m["outputs"],
        criteria=m["criteria"],
        error=m["error"],
        approval_id=m["approval_id"],
    )


def _run(row: Any, nodes: list[NodeRun]) -> WorkflowRun:
    m = row._mapping
    return WorkflowRun(
        id=m["id"],
        run_id=m["run_id"],
        definition_id=m["definition_id"],
        definition_version=m["definition_version"],
        goal=m["goal"],
        project_id=m["project_id"],
        status=WorkflowStatus(m["status"]),
        current_node=m["current_node"],
        risk=Risk(m["risk"]),
        team=TeamSelection.model_validate(m["team"]),
        steps=m["steps"],
        version=m["version"],
        created_at=m["created_at"],
        updated_at=m["updated_at"],
        finished_at=m["finished_at"],
        error=m["error"],
        node_runs=nodes,
    )


class WorkflowStore:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def create(
        self,
        *,
        run_id: uuid.UUID,
        definition: WorkflowDefinition,
        goal: str,
        project_id: uuid.UUID | None,
        risk: Risk,
        team: TeamSelection,
    ) -> uuid.UUID:
        wid, now = uuid.uuid4(), utcnow()
        async with self._engine.begin() as conn:
            await conn.execute(
                sa.insert(run_t).values(
                    id=wid,
                    run_id=run_id,
                    definition_id=definition.id,
                    definition_version=definition.version,
                    definition=definition.model_dump(mode="json"),
                    goal=goal[:4000],
                    project_id=project_id,
                    status=WorkflowStatus.RUNNING.value,
                    current_node=definition.start,
                    risk=risk.value,
                    team=team.model_dump(mode="json"),
                    steps=0,
                    version=0,
                    created_at=now,
                    updated_at=now,
                )
            )
        return wid

    async def definition_of(self, workflow_id: uuid.UUID) -> WorkflowDefinition:
        async with self._engine.connect() as conn:
            raw = (
                await conn.execute(sa.select(run_t.c.definition).where(run_t.c.id == workflow_id))
            ).scalar_one_or_none()
        if raw is None:
            raise LookupError(f"workflow {workflow_id} not found")
        return WorkflowDefinition.model_validate(raw)

    async def get(self, workflow_id: uuid.UUID) -> WorkflowRun | None:
        async with self._engine.connect() as conn:
            row = (await conn.execute(sa.select(run_t).where(run_t.c.id == workflow_id))).first()
            if row is None:
                return None
            nodes = (
                await conn.execute(
                    sa.select(node_t)
                    .where(node_t.c.workflow_id == workflow_id)
                    .order_by(node_t.c.started_at, node_t.c.attempt)
                )
            ).all()
        return _run(row, [_node(n) for n in nodes])

    async def list_runs(
        self, *, status: WorkflowStatus | None = None, limit: int = 50, before: str | None = None
    ) -> list[WorkflowRun]:
        stmt = sa.select(run_t).order_by(run_t.c.created_at.desc(), run_t.c.id).limit(limit)
        if status is not None:
            stmt = stmt.where(run_t.c.status == status.value)
        async with self._engine.connect() as conn:
            rows = (await conn.execute(stmt)).all()
        return [_run(r, []) for r in rows]

    async def add_node_run(self, conn: AsyncConnection, node: NodeRun) -> None:
        await conn.execute(
            sa.insert(node_t).values(
                id=node.id,
                workflow_id=node.workflow_id,
                node_id=node.node_id,
                stage=node.stage.value,
                attempt=node.attempt,
                status=node.status.value,
                started_at=node.started_at,
                finished_at=node.finished_at,
                reporter=node.reporter,
                outputs=node.outputs,
                criteria=[c.model_dump() for c in node.criteria],
                error=node.error,
                approval_id=node.approval_id,
            )
        )

    async def update_node_run(self, conn: AsyncConnection, node: NodeRun) -> None:
        await conn.execute(
            sa.update(node_t)
            .where(node_t.c.id == node.id)
            .values(
                status=node.status.value,
                finished_at=node.finished_at,
                reporter=node.reporter,
                outputs=node.outputs,
                criteria=[c.model_dump() for c in node.criteria],
                error=node.error,
                approval_id=node.approval_id,
            )
        )

    async def save(
        self,
        run: WorkflowRun,
        *,
        expected_version: int,
        new_nodes: list[NodeRun] | None = None,
        updated_nodes: list[NodeRun] | None = None,
    ) -> None:
        """Persist run state + node changes atomically; fails if someone else wrote first."""
        now = utcnow()
        finished = run.status in {
            WorkflowStatus.COMPLETED,
            WorkflowStatus.FAILED,
            WorkflowStatus.CANCELLED,
        }
        async with self._engine.begin() as conn:
            result = await conn.execute(
                sa.update(run_t)
                .where(run_t.c.id == run.id, run_t.c.version == expected_version)
                .values(
                    status=run.status.value,
                    current_node=run.current_node,
                    steps=run.steps,
                    error=run.error,
                    version=expected_version + 1,
                    updated_at=now,
                    finished_at=(now if finished else None),
                )
            )
            if result.rowcount != 1:
                raise ConcurrentUpdateError(f"workflow {run.id} was modified concurrently")
            for n in updated_nodes or []:
                await self.update_node_run(conn, n)
            for n in new_nodes or []:
                await self.add_node_run(conn, n)
