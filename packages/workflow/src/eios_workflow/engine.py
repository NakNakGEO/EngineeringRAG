"""Workflow engine: the graph controls transitions, retries, approvals, budgets and completion.

The reasoning model (an external LLM client) works *inside* the active node and reports back;
this engine decides - deterministically - whether the node's acceptance criteria are met, what
happens next, and when a human has to decide. Nothing here calls a model.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from eios_domain.events import ActorType, EventStatus, EventType
from eios_domain.ids import new_id, utcnow
from eios_domain.knowledge import EvidenceRecord
from eios_domain.policy import PolicyEffect, PolicyRequest, Risk
from eios_domain.workflow import (
    TERMINAL_WORKFLOW_STATUSES,
    NodeDef,
    NodeRun,
    NodeStatus,
    TeamSelection,
    WorkflowDefinition,
    WorkflowRun,
    WorkflowStatus,
)
from eios_observability.recorder import RunContext, RunRecorder
from eios_observability.store import EventStore
from eios_policy import PolicyEngine
from eios_workflow.criteria import evaluate_criteria
from eios_workflow.store import ConcurrentUpdateError, WorkflowStore
from eios_workflow.team import AgentInfo, TeamSignals, assert_one_writer, select_team

_RISK_RANK = {Risk.LOW: 0, Risk.MEDIUM: 1, Risk.HIGH: 2, Risk.CRITICAL: 3}
AgentsProvider = Callable[[], Awaitable[dict[str, AgentInfo]]]
EvidenceLookup = Callable[[list[uuid.UUID]], Awaitable[list[EvidenceRecord]]]


class WorkflowError(Exception):
    """A workflow operation that is not allowed. ``kind`` is stable for API mapping."""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind


class WorkflowEngine:
    def __init__(
        self,
        *,
        store: WorkflowStore,
        definitions: dict[str, WorkflowDefinition],
        recorder: RunRecorder,
        events: EventStore,
        policy: PolicyEngine,
        agents: AgentsProvider,
        evidence: EvidenceLookup,
    ) -> None:
        self._store = store
        self._definitions = definitions
        self._recorder = recorder
        self._events = events
        self._policy = policy
        self._agents = agents
        self._evidence = evidence

    @property
    def definitions(self) -> dict[str, WorkflowDefinition]:
        return self._definitions

    # -- start -----------------------------------------------------------------------------
    async def start(
        self,
        *,
        goal: str,
        definition_id: str = "engineering.default",
        project_id: uuid.UUID | None = None,
        risk: Risk = Risk.MEDIUM,
        signals: TeamSignals | None = None,
        team: TeamSelection | None = None,
    ) -> WorkflowRun:
        definition = self._definitions.get(definition_id)
        if definition is None:
            raise WorkflowError("unknown_definition", f"workflow '{definition_id}' is not defined")
        agents = await self._agents()
        if team is None:
            team = select_team(goal, signals or TeamSignals(risk=risk), agents)
        assert_one_writer(team, agents)
        ctx = await self._recorder.start_run(
            kind="workflow",
            goal=goal[:500],
            project_id=project_id,
            metadata={"definition": f"{definition.id}@{definition.version}"},
        )
        await ctx.emit(
            EventType.GOAL_CLASSIFIED,
            f"risk {risk.value}",
            data={"risk": risk.value, "definition": definition.id},
        )
        for role in team.roles:
            await ctx.emit(
                EventType.AGENT_SELECTED,
                f"{role} selected",
                data={
                    "role": role,
                    "reason": team.reasons.get(role, ""),
                    "writer": role == team.writer,
                },
                actor_type=ActorType.SYSTEM,
                actor_id="team_selector",
            )
        wid = await self._store.create(
            run_id=ctx.run_id,
            definition=definition,
            goal=goal,
            project_id=project_id,
            risk=risk,
            team=team,
        )
        run = await self._must_get(wid)
        run, new_nodes = await self._enter(run, definition, definition.start, ctx)
        await self._store.save(run, expected_version=run.version, new_nodes=new_nodes)
        return await self._must_get(wid)

    # -- report ----------------------------------------------------------------------------
    async def report(
        self,
        workflow_id: uuid.UUID,
        *,
        node_id: str,
        reporter: str,
        outputs: dict[str, Any],
        evidence_ids: list[uuid.UUID] | None = None,
    ) -> WorkflowRun:
        run = await self._must_get(workflow_id)
        definition = await self._store.definition_of(workflow_id)
        ctx = await self._recorder.resume(run.run_id)
        if run.status in TERMINAL_WORKFLOW_STATUSES:
            raise WorkflowError("invalid_state", f"workflow is already {run.status.value}")
        if run.status is WorkflowStatus.WAITING_APPROVAL:
            raise WorkflowError(
                "waiting_approval", "the active node is waiting for a human decision"
            )
        if run.current_node != node_id:
            raise WorkflowError(
                "wrong_node", f"active node is '{run.current_node}', not '{node_id}'"
            )
        node = definition.node(node_id)
        if reporter not in run.team.roles:
            raise WorkflowError("unknown_role", f"'{reporter}' is not part of this run's team")
        changed = outputs.get("changed_files")
        if changed and (reporter != run.team.writer or not node.writer_allowed):
            await ctx.emit(
                EventType.POLICY_DENIED,
                "writer rule violated",
                status=EventStatus.DENIED,
                data={
                    "rule": "one_writer_many_reviewers",
                    "reporter": reporter,
                    "writer": run.team.writer,
                    "node": node_id,
                },
                actor_type=ActorType.SYSTEM,
                actor_id="workflow",
            )
            raise WorkflowError(
                "writer_violation",
                f"only the selected writer ({run.team.writer}) may change files, and only in a "
                "writer node",
            )
        active = self._active_node_run(run, node_id)
        started_at = active.started_at if active else utcnow()
        results = await evaluate_criteria(
            list(node.criteria),
            outputs,
            evidence_ids or [],
            lookup_evidence=self._evidence,
            capability_completed=lambda cap: self._capability_completed(
                run.run_id, cap, started_at
            ),
            project_id=run.project_id,
        )
        passed = all(r.passed for r in results)
        if active is None:  # defensive: state corrupted
            raise WorkflowError("invalid_state", "no active attempt for this node")
        finished = active.model_copy(
            update={
                "status": NodeStatus.PASSED if passed else NodeStatus.FAILED,
                "finished_at": utcnow(),
                "reporter": reporter,
                "outputs": outputs,
                "criteria": results,
                "error": None
                if passed
                else "; ".join(r.detail for r in results if not r.passed)[:1000],
            }
        )
        await ctx.emit(
            EventType.WORKFLOW_NODE_COMPLETED,
            f"{node.stage.value}/{node.id} attempt {finished.attempt}: "
            + ("passed" if passed else "failed"),
            status=EventStatus.COMPLETED if passed else EventStatus.FAILED,
            data={
                "workflow_id": str(run.id),
                "node": node.id,
                "stage": node.stage.value,
                "attempt": finished.attempt,
                "reporter": reporter,
                "criteria": [r.model_dump() for r in results],
            },
        )
        expected = run.version
        new_nodes: list[NodeRun] = []
        if passed:
            if node.next is None:
                run = run.model_copy(
                    update={"status": WorkflowStatus.COMPLETED, "current_node": None}
                )
            else:
                run, new_nodes = await self._enter(run, definition, node.next, ctx)
        else:
            run, new_nodes = await self._after_failure(run, definition, node, finished, ctx)
        try:
            await self._store.save(
                run, expected_version=expected, new_nodes=new_nodes, updated_nodes=[finished]
            )
        except ConcurrentUpdateError as exc:
            raise WorkflowError("conflict", str(exc)) from exc
        if run.status is WorkflowStatus.COMPLETED:
            await self._recorder.complete_run(ctx, "workflow completed")
        elif run.status is WorkflowStatus.FAILED:
            await self._recorder.fail_run(ctx, run.error or "workflow failed")
        return await self._must_get(workflow_id)

    async def _after_failure(
        self,
        run: WorkflowRun,
        definition: WorkflowDefinition,
        node: NodeDef,
        finished: NodeRun,
        ctx: RunContext,
    ) -> tuple[WorkflowRun, list[NodeRun]]:
        if finished.attempt < node.max_attempts:
            await ctx.emit(
                EventType.RETRY_STARTED,
                f"retrying {node.id} (attempt {finished.attempt + 1})",
                data={"node": node.id, "attempt": finished.attempt + 1, "reason": finished.error},
            )
            return await self._enter(run, definition, node.id, ctx, retry=True)
        if node.on_failure.action == "goto" and node.on_failure.goto:
            target = definition.node(node.on_failure.goto)
            attempts_used = sum(1 for n in run.node_runs if n.node_id == target.id)
            if attempts_used >= target.max_attempts:
                return self._fail(
                    run, f"'{target.id}' exhausted its {target.max_attempts} attempts"
                ), []
            await ctx.emit(
                EventType.RETRY_STARTED,
                f"{node.id} failed; returning to {target.id}",
                data={"from": node.id, "to": target.id, "reason": finished.error},
            )
            return await self._enter(run, definition, target.id, ctx, retry=True)
        return self._fail(
            run, f"node '{node.id}' failed after {finished.attempt} attempt(s): {finished.error}"
        ), []

    def _fail(self, run: WorkflowRun, error: str) -> WorkflowRun:
        return run.model_copy(
            update={"status": WorkflowStatus.FAILED, "current_node": None, "error": error[:1000]}
        )

    # -- entering a node --------------------------------------------------------------------
    async def _enter(
        self,
        run: WorkflowRun,
        definition: WorkflowDefinition,
        node_id: str,
        ctx: RunContext,
        *,
        retry: bool = False,
    ) -> tuple[WorkflowRun, list[NodeRun]]:
        steps = run.steps + 1
        if steps > definition.max_total_steps:
            await ctx.emit(
                EventType.BUDGET_EXCEEDED,
                "workflow step budget exceeded",
                status=EventStatus.FAILED,
                data={"max_total_steps": definition.max_total_steps},
            )
            return self._fail(
                run.model_copy(update={"steps": steps}),
                f"workflow exceeded its step budget ({definition.max_total_steps})",
            ), []
        node = definition.node(node_id)
        attempt = sum(1 for n in run.node_runs if n.node_id == node_id) + 1
        status = NodeStatus.ACTIVE
        run_status = WorkflowStatus.RUNNING
        approval_id: uuid.UUID | None = None
        if (
            node.gate is not None
            and _RISK_RANK[run.risk] >= _RISK_RANK[node.gate.when_risk_at_least]
        ):
            decision = await self._gate(run, node, ctx)
            if decision.effect is not PolicyEffect.ALLOW:
                status, run_status, approval_id = (
                    NodeStatus.WAITING_APPROVAL,
                    WorkflowStatus.WAITING_APPROVAL,
                    decision.approval_id,
                )
        node_run = NodeRun(
            id=new_id(),
            workflow_id=run.id,
            node_id=node.id,
            stage=node.stage,
            attempt=attempt,
            status=status,
            started_at=utcnow(),
            approval_id=approval_id,
        )
        await ctx.emit(
            EventType.WORKFLOW_NODE_STARTED,
            f"{node.stage.value}/{node.id} attempt {attempt}",
            status=EventStatus.STARTED,
            data={
                "workflow_id": str(run.id),
                "node": node.id,
                "stage": node.stage.value,
                "attempt": attempt,
                "capabilities": node.capabilities,
                "waiting_approval": status is NodeStatus.WAITING_APPROVAL,
                "approval_id": str(approval_id) if approval_id else None,
                "retry": retry,
            },
        )
        updated = run.model_copy(
            update={
                "current_node": node.id,
                "status": run_status,
                "steps": steps,
                "node_runs": [*run.node_runs, node_run],
            }
        )
        return updated, [node_run]

    async def _gate(self, run: WorkflowRun, node: NodeDef, ctx: RunContext):  # type: ignore[no-untyped-def]
        return await self._policy.evaluate(
            PolicyRequest(
                actor_type=ActorType.SYSTEM,
                actor_id="workflow",
                action="workflow.gate",
                target=f"{run.id}:{node.id}",
                run_id=run.run_id,
                attributes={"risk": run.risk.value, "node": node.id},
            ),
            ctx,
        )

    # -- approval resume / cancel -------------------------------------------------------------
    async def resume(self, workflow_id: uuid.UUID) -> WorkflowRun:
        """Re-check a gated node: if a human approved it (single use) the node becomes active."""
        run = await self._must_get(workflow_id)
        if run.status is not WorkflowStatus.WAITING_APPROVAL or run.current_node is None:
            return run
        definition = await self._store.definition_of(workflow_id)
        node = definition.node(run.current_node)
        ctx = await self._recorder.resume(run.run_id)
        decision = await self._gate(run, node, ctx)
        if decision.effect is not PolicyEffect.ALLOW:
            return run
        waiting = self._active_node_run(run, node.id, status=NodeStatus.WAITING_APPROVAL)
        if waiting is None:
            return run
        activated = waiting.model_copy(
            update={"status": NodeStatus.ACTIVE, "approval_id": decision.approval_id}
        )
        new_run = run.model_copy(update={"status": WorkflowStatus.RUNNING})
        await self._store.save(new_run, expected_version=run.version, updated_nodes=[activated])
        await ctx.emit(
            EventType.APPROVAL_DECIDED,
            f"{node.id} approved; continuing",
            data={
                "workflow_id": str(run.id),
                "node": node.id,
                "approval_id": str(decision.approval_id),
            },
        )
        return await self._must_get(workflow_id)

    async def cancel(self, workflow_id: uuid.UUID, *, actor: str, reason: str) -> WorkflowRun:
        run = await self._must_get(workflow_id)
        if run.status in TERMINAL_WORKFLOW_STATUSES:
            raise WorkflowError("invalid_state", f"workflow is already {run.status.value}")
        ctx = await self._recorder.resume(run.run_id)
        cancelled = run.model_copy(
            update={
                "status": WorkflowStatus.CANCELLED,
                "current_node": None,
                "error": f"cancelled by {actor}: {reason}"[:1000],
            }
        )
        await self._store.save(cancelled, expected_version=run.version)
        await self._recorder.fail_run(ctx, f"cancelled by {actor}: {reason}")
        return await self._must_get(workflow_id)

    # -- queries ------------------------------------------------------------------------------
    async def get(self, workflow_id: uuid.UUID) -> WorkflowRun | None:
        return await self._store.get(workflow_id)

    async def list_runs(
        self, *, status: WorkflowStatus | None = None, limit: int = 50
    ) -> list[WorkflowRun]:
        return await self._store.list_runs(status=status, limit=limit)

    async def brief(self, workflow_id: uuid.UUID) -> dict[str, Any]:
        """What a model needs to act: where it is, what is allowed, what must be true to proceed."""
        run = await self._must_get(workflow_id)
        definition = await self._store.definition_of(workflow_id)
        out: dict[str, Any] = {
            "workflow_id": str(run.id),
            "run_id": str(run.run_id),
            "status": run.status.value,
            "goal": run.goal,
            "risk": run.risk.value,
            "team": run.team.model_dump(mode="json"),
            "steps": run.steps,
            "error": run.error,
            "node": None,
        }
        if run.current_node is not None:
            node = definition.node(run.current_node)
            attempts = sum(1 for n in run.node_runs if n.node_id == node.id)
            active = next((n for n in reversed(run.node_runs) if n.node_id == node.id), None)
            out["node"] = {
                "id": node.id,
                "stage": node.stage.value,
                "description": node.description,
                "capabilities": node.capabilities,
                "writer_allowed": node.writer_allowed,
                "attempt": attempts,
                "max_attempts": node.max_attempts,
                "criteria": [{"id": c.id, "description": c.description} for c in node.criteria],
                "waiting_approval": run.status is WorkflowStatus.WAITING_APPROVAL,
                "approval_id": str(active.approval_id) if active and active.approval_id else None,
                "last_failure": next(
                    (n.error for n in reversed(run.node_runs) if n.node_id == node.id and n.error),
                    None,
                ),
            }
        return out

    # -- helpers -----------------------------------------------------------------------------------
    async def _must_get(self, workflow_id: uuid.UUID) -> WorkflowRun:
        run = await self._store.get(workflow_id)
        if run is None:
            raise WorkflowError("not_found", f"workflow {workflow_id} not found")
        return run

    @staticmethod
    def _active_node_run(
        run: WorkflowRun, node_id: str, *, status: NodeStatus = NodeStatus.ACTIVE
    ) -> NodeRun | None:
        for n in reversed(run.node_runs):
            if n.node_id == node_id and n.status is status:
                return n
        return None

    async def _capability_completed(self, run_id: uuid.UUID, capability: str, since: Any) -> bool:
        page = await self._events.list_events(
            run_id, types=[EventType.TOOL_COMPLETED.value], limit=500
        )
        return any(
            e.data.get("capability") == capability
            and e.status is EventStatus.COMPLETED
            and e.timestamp >= since
            for e in page.items
        )
