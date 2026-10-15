"""The observability event contract.

``EventEnvelope`` is a stable, versioned contract: backend producers, the SSE stream and the
future live UI all depend on it. Additive changes only; bump ``schema_version`` for anything
else. Consumers must tolerate event ``type`` values they do not know.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from eios_domain.ids import ensure_utc

SCHEMA_VERSION = 1
MAX_SUMMARY_LENGTH = 500
MAX_DATA_BYTES = 64 * 1024


class EventType(StrEnum):
    # Master plan section 6.20
    RUN_STARTED = "RUN_STARTED"
    GOAL_CLASSIFIED = "GOAL_CLASSIFIED"
    CONTEXT_REQUESTED = "CONTEXT_REQUESTED"
    CONTEXT_EXPANDED = "CONTEXT_EXPANDED"
    RETRIEVAL_STARTED = "RETRIEVAL_STARTED"
    KNOWLEDGE_HIT = "KNOWLEDGE_HIT"
    GRAPH_HIT = "GRAPH_HIT"
    MEMORY_HIT = "MEMORY_HIT"
    AGENT_SELECTED = "AGENT_SELECTED"
    SKILL_SELECTED = "SKILL_SELECTED"
    CAPABILITY_REQUESTED = "CAPABILITY_REQUESTED"
    TOOL_SELECTED = "TOOL_SELECTED"
    POLICY_ALLOWED = "POLICY_ALLOWED"
    POLICY_DENIED = "POLICY_DENIED"
    TOOL_STARTED = "TOOL_STARTED"
    TOOL_COMPLETED = "TOOL_COMPLETED"
    LLM_STARTED = "LLM_STARTED"
    LLM_COMPLETED = "LLM_COMPLETED"
    EVIDENCE_CREATED = "EVIDENCE_CREATED"
    FINDING_CREATED = "FINDING_CREATED"
    FILE_CHANGED = "FILE_CHANGED"
    BUILD_STARTED = "BUILD_STARTED"
    BUILD_COMPLETED = "BUILD_COMPLETED"
    TEST_STARTED = "TEST_STARTED"
    TEST_COMPLETED = "TEST_COMPLETED"
    RETRY_STARTED = "RETRY_STARTED"
    KNOWLEDGE_UPDATED = "KNOWLEDGE_UPDATED"
    RUN_COMPLETED = "RUN_COMPLETED"
    RUN_FAILED = "RUN_FAILED"
    # Additive extensions (schema_version 1 stays valid: consumers ignore unknown types)
    APPROVAL_REQUESTED = "APPROVAL_REQUESTED"
    APPROVAL_DECIDED = "APPROVAL_DECIDED"
    WORKFLOW_NODE_STARTED = "WORKFLOW_NODE_STARTED"
    WORKFLOW_NODE_COMPLETED = "WORKFLOW_NODE_COMPLETED"
    CAPABILITY_RESOLVED = "CAPABILITY_RESOLVED"
    CAPABILITY_GAP_DETECTED = "CAPABILITY_GAP_DETECTED"
    WORKSHOP_PROPOSAL_UPDATED = "WORKSHOP_PROPOSAL_UPDATED"
    PROJECT_SYNCED = "PROJECT_SYNCED"
    BUDGET_EXCEEDED = "BUDGET_EXCEEDED"
    EVALUATION_RECORDED = "EVALUATION_RECORDED"


TERMINAL_EVENT_TYPES = frozenset({EventType.RUN_COMPLETED, EventType.RUN_FAILED})


class ActorType(StrEnum):
    LLM = "llm"
    AGENT = "agent"
    TOOL = "tool"
    SYSTEM = "system"
    HUMAN = "human"


class EventStatus(StrEnum):
    STARTED = "started"
    COMPLETED = "completed"
    FAILED = "failed"
    DENIED = "denied"


class EventDraft(BaseModel):
    """What a producer supplies; the store assigns ``event_id``, ``seq`` and ``timestamp``."""

    model_config = ConfigDict(frozen=True)

    run_id: uuid.UUID
    trace_id: uuid.UUID
    span_id: uuid.UUID
    parent_span_id: uuid.UUID | None = None
    type: EventType
    actor_type: ActorType = ActorType.SYSTEM
    actor_id: str = Field(default="eios", min_length=1, max_length=200)
    status: EventStatus = EventStatus.COMPLETED
    summary: str = Field(default="", max_length=MAX_SUMMARY_LENGTH)
    data: dict[str, Any] = Field(default_factory=dict)

    @field_validator("data")
    @classmethod
    def _bounded_data(cls, value: dict[str, Any]) -> dict[str, Any]:
        if len(json.dumps(value, default=str).encode()) > MAX_DATA_BYTES:
            raise ValueError(f"event data exceeds {MAX_DATA_BYTES} bytes")
        return value


class EventEnvelope(BaseModel):
    """A persisted event. ``seq`` is gap-free and strictly increasing per run (starts at 1)."""

    model_config = ConfigDict(frozen=True)

    event_id: uuid.UUID
    run_id: uuid.UUID
    trace_id: uuid.UUID
    span_id: uuid.UUID
    parent_span_id: uuid.UUID | None = None
    seq: int = Field(ge=1)
    schema_version: int = SCHEMA_VERSION
    type: EventType | str
    timestamp: datetime
    actor_type: ActorType
    actor_id: str
    status: EventStatus
    summary: str = ""
    data: dict[str, Any] = Field(default_factory=dict)

    @field_validator("timestamp")
    @classmethod
    def _utc(cls, value: datetime) -> datetime:
        return ensure_utc(value)
