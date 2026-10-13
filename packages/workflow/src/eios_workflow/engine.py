"""Workflow engine: the graph controls transitions, retries, approvals, budgets and completion.

The reasoning model (an external LLM client) works *inside* the active node and reports back;
this engine decides - deterministically - whether the node's acceptance criteria are met, what
happens next, and when a human has to decide. Nothing here calls a model.

Observability: a workflow run has one root span (RUN_STARTED ... RUN_COMPLETED). Every node
attempt is a child span, identified by the id of its ``NodeRun``, so the STARTED event (written
when the attempt opens) and the COMPLETED event (written by a later report) land in the same
span and a failed attempt keeps its own failed status in the execution graph.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import replace
from typing import Any, NoReturn

from eios_domain.events import ActorType, EventStatus, EventType
from eios_domain.ids import new_id, utcnow
from eios_domain.knowledge import EvidenceRecord
from eios_domain.policy import PolicyEffect, PolicyRequest, Risk
from eios_domain.workflow import (
    REVIEWER_ROLE,
    TERMINAL_WORKFLOW_STATUSES,
    WRITER_ROLE,
    Criterion,
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
from eios_workflow.signals import RISK_RANK, FactsProvider, ProjectFacts, max_risk, merge_signals
from eios_workflow.signals import snapshot as signals_snapshot
from eios_workflow.store import ConcurrentUpdateError, WorkflowStore
from eios_workflow.team import (
    AgentInfo,
    TeamSignals,
    assert_one_writer,
    docs_paths_only,
    select_team,
)

DEFAULT_DEFINITION = "engineering.default"
DOCS_DEFINITION = "engineering.docs"  # documentation-only work at low risk; evidence is optional
AgentsProvider = Callable[[], Awaitable[dict[str, AgentInfo]]]
EvidenceLookup = Callable[[list[uuid.UUID]], Awaitable[list[EvidenceRecord]]]


class WorkflowError(Exception):
    """A workflow operation that is not allowed. ``kind`` is stable for API mapping."""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind


def _fits(node: NodeDef, team: TeamSelection, role: str) -> bool:
    return (
        role in node.roles
        or (REVIEWER_ROLE in node.roles and role != team.writer)
        or (WRITER_ROLE in node.roles and role == team.writer)
    )


def _criterion_view(c: Criterion) -> dict[str, Any]:
    """What a client needs to satisfy the criterion: the exact output key and expected value."""
    view: dict[str, Any] = {
        "id": c.id,
        "description": c.description,
        "kind": c.kind,
        "field": c.field,
        "value": c.value,
        "allow_empty": c.allow_empty,
        "min_count": c.min_count,
    }
    if c.source_tool is not None:
        view["source_tool"] = c.source_tool
    if c.capability is not None:
        view["capability"] = c.capability
    return view


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
        facts: FactsProvider | None = None,
    ) -> None:
        self._store = store
        self._definitions = definitions
        self._recorder = recorder
        self._events = events
        self._policy = policy
        self._agents = agents
        self._evidence = evidence
        self._facts = facts

    @property
    def definitions(self) -> dict[str, WorkflowDefinition]:
        return self._definitions

    # -- start -----------------------------------------------------------------------------
    async def start(
        self,
        *,
        goal: str,
        definition_id: str | None = None,
        project_id: uuid.UUID | None = None,
        risk: Risk = Risk.MEDIUM,
        signals: TeamSignals | None = None,
        team: TeamSelection | None = None,
    ) -> WorkflowRun:
        """Start a run. ``definition_id=None`` picks the definition from the team and risk."""
        if definition_id is not None and definition_id not in self._definitions:
            raise WorkflowError("unknown_definition", f"workflow '{definition_id}' is not defined")
        agents = await self._agents()
        base = signals or TeamSignals(risk=risk)
        caller_risk = max_risk(risk, base.risk)
        base = replace(base, risk=caller_risk)
        facts, facts_error = await self._project_facts(project_id, base)
        effective = merge_signals(base, facts)
        risk = effective.risk  # never lower than what the caller said or the analysis found
        if team is None:
            team = select_team(goal, effective, agents)
        assert_one_writer(team, agents)
        team = team.model_copy(
            update={
                "signals": signals_snapshot(
                    effective, caller_risk=caller_risk, facts=facts, error=facts_error
                )
            }
        )
        definition = self._choose_definition(definition_id, team, risk)
        ctx = await self._recorder.start_run(
            kind="workflow",
            goal=goal[:500],
            project_id=project_id,
            metadata={"definition": f"{definition.id}@{definition.version}"},
        )
        await ctx.emit(
            EventType.GOAL_CLASSIFIED,
            f"risk {risk.value}",
            data={"risk": risk.value, "definition": definition.id, "signals": team.signals},
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

    def _choose_definition(
        self, definition_id: str | None, team: TeamSelection, risk: Risk
    ) -> WorkflowDefinition:
        if definition_id is None:
            docs = team.primary_role == "technical_writer" and risk is Risk.LOW
            definition_id = DOCS_DEFINITION if docs else DEFAULT_DEFINITION
            if definition_id not in self._definitions:
                definition_id = DEFAULT_DEFINITION
        definition = self._definitions.get(definition_id)
        if definition is None:
            raise WorkflowError("unknown_definition", f"workflow '{definition_id}' is not defined")
        return definition

    async def _project_facts(
        self, project_id: uuid.UUID | None, signals: TeamSignals
    ) -> tuple[ProjectFacts | None, str | None]:
        """Impact and coverage for the named targets; enrichment never blocks starting work."""
        if self._facts is None or project_id is None or not (signals.paths or signals.symbols):
            return None, None
        if not signals.symbols and docs_paths_only(signals.paths):
            return None, None  # nothing depends on a README; tests-missing would be noise
        try:
            return await self._facts(project_id, signals.paths, signals.symbols), None
        except Exception as exc:  # unknown project, not indexed yet...
            return None, f"{type(exc).__name__}: {str(exc)[:200]}"

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
        active = self._active_node_run(run, node_id)
        if active is None:  # defensive: state corrupted
            raise WorkflowError("invalid_state", "no active attempt for this node")
        span = ctx.child(span_id=active.id, actor_type=ActorType.AGENT, actor_id=reporter)
        allowed = await self._reporters(node, run.team)
        if reporter not in run.team.roles and reporter not in (allowed or ()):
            raise WorkflowError("unknown_role", f"'{reporter}' is not part of this run's team")
        if allowed is not None and reporter not in allowed:
            await self._independence(span, run, node, reporter, allowed)
        elif allowed is None and REVIEWER_ROLE in node.roles and reporter == run.team.writer:
            await self._denial_span(span, "workflow").emit(
                EventType.POLICY_ALLOWED,
                "self-review allowed: the team has no other member",
                data={"rule": "independent_review", "waived": True, "node": node_id},
            )
        changed = outputs.get("changed_files")
        if changed and (reporter != run.team.writer or not node.writer_allowed):
            await self._denial_span(span, "workflow").emit(
                EventType.POLICY_DENIED,
                "writer rule violated",
                status=EventStatus.DENIED,
                data={
                    "rule": "one_writer_many_reviewers",
                    "reporter": reporter,
                    "writer": run.team.writer,
                    "node": node_id,
                },
            )
            raise WorkflowError(
                "writer_violation",
                f"only the selected writer ({run.team.writer}) may change files, and only in a "
                "writer node",
            )
        started_at = active.started_at
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
        await span.emit(
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

    async def _independence(
        self, span: RunContext, run: WorkflowRun, node: NodeDef, reporter: str, allowed: list[str]
    ) -> NoReturn:
        """Reject a report from a role the node does not allow (the writer verifying itself)."""
        self_review = reporter == run.team.writer and REVIEWER_ROLE in node.roles
        kind = "self_review" if self_review else "role_not_allowed"
        message = (
            f"'{reporter}' wrote this change and cannot review it; report as one of: "
            f"{', '.join(allowed)}"
            if self_review
            else f"'{reporter}' may not report node '{node.id}'; allowed: {', '.join(allowed)}"
        )
        await self._denial_span(span, "workflow").emit(
            EventType.POLICY_DENIED,
            f"{kind}: {reporter} on {node.id}",
            status=EventStatus.DENIED,
            data={
                "rule": "independent_review" if self_review else "node_roles",
                "reporter": reporter,
                "writer": run.team.writer,
                "node": node.id,
                "allowed": allowed,
            },
        )
        raise WorkflowError(kind, message)

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
        node_run_id = new_id()  # also the id of this attempt's span
        span = ctx.child(
            span_id=node_run_id,
            actor_type=ActorType.AGENT,
            actor_id=self._lead_role(node, run.team),
        )
        status = NodeStatus.ACTIVE
        run_status = WorkflowStatus.RUNNING
        approval_id: uuid.UUID | None = None
        if node.gate is not None and RISK_RANK[run.risk] >= RISK_RANK[node.gate.when_risk_at_least]:
            decision = await self._gate(run, node, span)
            if decision.effect is not PolicyEffect.ALLOW:
                status, run_status, approval_id = (
                    NodeStatus.WAITING_APPROVAL,
                    WorkflowStatus.WAITING_APPROVAL,
                    decision.approval_id,
                )
        node_run = NodeRun(
            id=node_run_id,
            workflow_id=run.id,
            node_id=node.id,
            stage=node.stage,
            attempt=attempt,
            status=status,
            started_at=utcnow(),
            approval_id=approval_id,
        )
        await span.emit(
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

    async def _gate(self, run: WorkflowRun, node: NodeDef, span: RunContext):  # type: ignore[no-untyped-def]
        # the policy decision is its own span: a pending approval shows as denied there while
        # the attempt itself stays open
        return await self._policy.evaluate(
            PolicyRequest(
                actor_type=ActorType.SYSTEM,
                actor_id="workflow",
                action="workflow.gate",
                target=f"{run.id}:{node.id}",
                run_id=run.run_id,
                attributes={"risk": run.risk.value, "node": node.id},
            ),
            self._denial_span(span, "policy"),
        )

    @staticmethod
    def _denial_span(parent: RunContext, actor_id: str) -> RunContext:
        return parent.child(actor_type=ActorType.SYSTEM, actor_id=actor_id)

    # -- approval resume / cancel -------------------------------------------------------------
    async def resume(self, workflow_id: uuid.UUID) -> WorkflowRun:
        """Re-check a gated node: if a human approved it (single use) the node becomes active."""
        run = await self._must_get(workflow_id)
        if run.status is not WorkflowStatus.WAITING_APPROVAL or run.current_node is None:
            return run
        definition = await self._store.definition_of(workflow_id)
        node = definition.node(run.current_node)
        waiting = self._active_node_run(run, node.id, status=NodeStatus.WAITING_APPROVAL)
        if waiting is None:
            return run
        ctx = await self._recorder.resume(run.run_id)
        span = ctx.child(
            span_id=waiting.id, actor_type=ActorType.AGENT, actor_id=self._lead_role(node, run.team)
        )
        decision = await self._gate(run, node, span)
        if decision.effect is not PolicyEffect.ALLOW:
            return run
        activated = waiting.model_copy(
            update={"status": NodeStatus.ACTIVE, "approval_id": decision.approval_id}
        )
        new_run = run.model_copy(update={"status": WorkflowStatus.RUNNING})
        await self._store.save(new_run, expected_version=run.version, updated_nodes=[activated])
        await span.emit(
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
        open_attempt = next(
            (
                n
                for n in reversed(run.node_runs)
                if n.status in (NodeStatus.ACTIVE, NodeStatus.WAITING_APPROVAL)
            ),
            None,
        )
        if open_attempt is not None:  # close its span so the graph does not show it as running
            await ctx.child(
                span_id=open_attempt.id, actor_type=ActorType.HUMAN, actor_id=actor
            ).emit(
                EventType.WORKFLOW_NODE_COMPLETED,
                f"{open_attempt.stage.value}/{open_attempt.node_id} attempt "
                f"{open_attempt.attempt}: cancelled",
                status=EventStatus.FAILED,
                data={
                    "workflow_id": str(run.id),
                    "node": open_attempt.node_id,
                    "stage": open_attempt.stage.value,
                    "attempt": open_attempt.attempt,
                    "cancelled": True,
                },
            )
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
            "definition": f"{run.definition_id}@{run.definition_version}",
            "status": run.status.value,
            "goal": run.goal,
            "risk": run.risk.value,
            "team": run.team.model_dump(mode="json", exclude={"signals"}),
            "signals": run.team.signals,
            "steps": run.steps,
            "error": run.error,
            "node": None,
        }
        if run.current_node is not None:
            node = definition.node(run.current_node)
            attempts = sum(1 for n in run.node_runs if n.node_id == node.id)
            active = next((n for n in reversed(run.node_runs) if n.node_id == node.id), None)
            reporters = await self._reporters(node, run.team)
            out["node"] = {
                "id": node.id,
                "stage": node.stage.value,
                "description": node.description,
                "capabilities": node.capabilities,
                "writer_allowed": node.writer_allowed,
                "roles": node.roles,
                "reporters": reporters if reporters is not None else run.team.roles,
                "attempt": attempts,
                "max_attempts": node.max_attempts,
                "criteria": [_criterion_view(c) for c in node.criteria],
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

    async def _reporters(self, node: NodeDef, team: TeamSelection) -> list[str] | None:
        """Roles that may report ``node``; None when any team member may."""
        if not node.roles:
            return None
        allowed = [r for r in team.roles if _fits(node, team, r)]
        named = [
            r for r in node.roles if r not in (REVIEWER_ROLE, WRITER_ROLE) and r not in team.roles
        ]
        if named:  # a role the node names may report although the selector did not pick it
            agents = await self._agents()
            allowed += [r for r in named if r in agents and agents[r].routable]
        return allowed or None

    @staticmethod
    def _lead_role(node: NodeDef, team: TeamSelection) -> str:
        """The role expected to work the node (the actor of its span)."""
        if node.writer_allowed and team.writer is not None:
            return team.writer
        return next((r for r in team.roles if _fits(node, team, r)), team.primary_role)

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
