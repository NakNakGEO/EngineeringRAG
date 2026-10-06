"""Workflow graph domain: definitions, run/node state, acceptance criteria, team selection.

Macro-deterministic, micro-agentic: the graph (this module + the engine) controls transitions,
retries, approvals, budgets and completion criteria; the model works *inside* a node.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from eios_domain.policy import Risk

NODE_ID = re.compile(r"^[a-z][a-z0-9_]{1,40}$")


class Stage(StrEnum):
    UNDERSTAND = "UNDERSTAND"
    DESIGN = "DESIGN"
    IMPLEMENT = "IMPLEMENT"
    VERIFY = "VERIFY"
    CURATE = "CURATE"


class WorkflowStatus(StrEnum):
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class NodeStatus(StrEnum):
    ACTIVE = "active"
    WAITING_APPROVAL = "waiting_approval"
    PASSED = "passed"
    FAILED = "failed"
    SKIPPED = "skipped"


TERMINAL_WORKFLOW_STATUSES = frozenset(
    {WorkflowStatus.COMPLETED, WorkflowStatus.FAILED, WorkflowStatus.CANCELLED}
)


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Criterion(_Frozen):
    """One acceptance criterion, evaluated deterministically against a node report.

    * ``field``     - ``outputs[field]`` is present (and non-empty unless ``allow_empty``);
    * ``equals``    - ``outputs[field] == value``;
    * ``evidence``  - at least ``min_count`` registered evidence records are referenced
                      (optionally only from ``source_tool``);
    * ``capability``- the run recorded a completed call of ``capability`` during this node.
    """

    id: str = Field(min_length=1, max_length=60)
    description: str = Field(min_length=3, max_length=300)
    kind: Literal["field", "equals", "evidence", "capability"]
    field: str | None = Field(default=None, max_length=60)
    value: Any = None
    allow_empty: bool = False
    min_count: int = Field(default=1, ge=1, le=100)
    source_tool: str | None = None
    capability: str | None = None

    @model_validator(mode="after")
    def _shape(self) -> Criterion:
        if self.kind in {"field", "equals"} and not self.field:
            raise ValueError(f"criterion '{self.id}': kind {self.kind} needs 'field'")
        if self.kind == "capability" and not self.capability:
            raise ValueError(f"criterion '{self.id}': kind capability needs 'capability'")
        return self


class FailureAction(_Frozen):
    action: Literal["retry", "goto", "fail"] = "retry"
    goto: str | None = None

    @model_validator(mode="after")
    def _goto(self) -> FailureAction:
        if (self.action == "goto") != (self.goto is not None):
            raise ValueError("'goto' must be set exactly when action is 'goto'")
        return self


class ApprovalGate(_Frozen):
    """Human approval before the node may start. ``when_risk_at_least`` makes it conditional."""

    when_risk_at_least: Risk = Risk.LOW


class NodeDef(_Frozen):
    id: str
    stage: Stage
    description: str = Field(min_length=3, max_length=500)
    capabilities: list[str] = Field(default_factory=list, max_length=50)
    roles: list[str] = Field(default_factory=list, max_length=10)
    writer_allowed: bool = False
    criteria: list[Criterion] = Field(default_factory=list, max_length=30)
    max_attempts: int = Field(default=3, ge=1, le=10)
    on_failure: FailureAction = Field(default_factory=FailureAction)
    gate: ApprovalGate | None = None
    next: str | None = None  # None = terminal node

    @field_validator("id")
    @classmethod
    def _id(cls, v: str) -> str:
        if not NODE_ID.match(v):
            raise ValueError("node ids are lowercase snake_case")
        return v


class WorkflowDefinition(_Frozen):
    id: str = Field(min_length=3, max_length=80)
    version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    description: str = Field(min_length=3, max_length=500)
    start: str
    nodes: list[NodeDef] = Field(min_length=1, max_length=50)
    max_total_steps: int = Field(default=40, ge=1, le=500)

    @model_validator(mode="after")
    def _graph(self) -> WorkflowDefinition:
        ids = [n.id for n in self.nodes]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate node ids")
        known = set(ids)
        if self.start not in known:
            raise ValueError(f"start node '{self.start}' is not defined")
        for n in self.nodes:
            if n.next is not None and n.next not in known:
                raise ValueError(f"node '{n.id}': next '{n.next}' is not defined")
            if n.on_failure.goto is not None and n.on_failure.goto not in known:
                raise ValueError(
                    f"node '{n.id}': failure goto '{n.on_failure.goto}' is not defined"
                )
        reachable: set[str] = set()
        frontier = [self.start]
        index = {n.id: n for n in self.nodes}
        while frontier:
            cur = frontier.pop()
            if cur in reachable:
                continue
            reachable.add(cur)
            node = index[cur]
            frontier += [x for x in (node.next, node.on_failure.goto) if x]
        if reachable != known:
            raise ValueError(f"unreachable nodes: {sorted(known - reachable)}")
        if not any(n.next is None for n in self.nodes):
            raise ValueError("a workflow needs at least one terminal node")
        return self

    def node(self, node_id: str) -> NodeDef:
        for n in self.nodes:
            if n.id == node_id:
                return n
        raise KeyError(node_id)


class CriterionResult(BaseModel):
    id: str
    passed: bool
    detail: str = ""


class NodeRun(BaseModel):
    id: uuid.UUID
    workflow_id: uuid.UUID
    node_id: str
    stage: Stage
    attempt: int
    status: NodeStatus
    started_at: datetime
    finished_at: datetime | None = None
    reporter: str | None = None
    outputs: dict[str, Any] = Field(default_factory=dict)
    criteria: list[CriterionResult] = Field(default_factory=list)
    error: str | None = None
    approval_id: uuid.UUID | None = None


class TeamSelection(BaseModel):
    """Output of the Agent Team Selector: the minimum effective team."""

    primary_role: str
    specialists: list[str] = Field(default_factory=list)
    reasons: dict[str, str] = Field(default_factory=dict)
    capabilities_needed: list[str] = Field(default_factory=list)
    review_requirements: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)
    skipped: dict[str, str] = Field(default_factory=dict)  # role -> why it was not used
    writer: str | None = None  # the single role allowed to change files

    @property
    def roles(self) -> list[str]:
        return [self.primary_role, *self.specialists]


class WorkflowRun(BaseModel):
    id: uuid.UUID
    run_id: uuid.UUID  # the observability run that carries this workflow's events
    definition_id: str
    definition_version: str
    goal: str
    project_id: uuid.UUID | None
    status: WorkflowStatus
    current_node: str | None
    risk: Risk
    team: TeamSelection
    steps: int
    version: int
    created_at: datetime
    updated_at: datetime
    finished_at: datetime | None = None
    error: str | None = None
    node_runs: list[NodeRun] = Field(default_factory=list)
