from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from eios_domain.events import (
    MAX_DATA_BYTES,
    ActorType,
    EventDraft,
    EventEnvelope,
    EventStatus,
    EventType,
)


def _draft(**kw: object) -> EventDraft:
    base: dict[str, object] = {
        "run_id": uuid.uuid4(),
        "trace_id": uuid.uuid4(),
        "span_id": uuid.uuid4(),
        "type": EventType.RUN_STARTED,
    }
    base.update(kw)
    return EventDraft(**base)  # type: ignore[arg-type]


def test_master_plan_event_types_are_all_present() -> None:
    required = {
        "RUN_STARTED", "GOAL_CLASSIFIED", "CONTEXT_REQUESTED", "CONTEXT_EXPANDED",
        "RETRIEVAL_STARTED", "KNOWLEDGE_HIT", "GRAPH_HIT", "MEMORY_HIT", "AGENT_SELECTED",
        "SKILL_SELECTED", "CAPABILITY_REQUESTED", "TOOL_SELECTED", "POLICY_ALLOWED",
        "POLICY_DENIED", "TOOL_STARTED", "TOOL_COMPLETED", "LLM_STARTED", "LLM_COMPLETED",
        "EVIDENCE_CREATED", "FINDING_CREATED", "FILE_CHANGED", "BUILD_STARTED",
        "BUILD_COMPLETED", "TEST_STARTED", "TEST_COMPLETED", "RETRY_STARTED",
        "KNOWLEDGE_UPDATED", "RUN_COMPLETED", "RUN_FAILED",
    }  # fmt: skip
    assert required <= {e.value for e in EventType}


def test_defaults_follow_the_contract() -> None:
    d = _draft()
    assert d.actor_type is ActorType.SYSTEM and d.status is EventStatus.COMPLETED


def test_summary_and_data_are_bounded() -> None:
    with pytest.raises(ValidationError):
        _draft(summary="x" * 501)
    with pytest.raises(ValidationError):
        _draft(data={"blob": "x" * (MAX_DATA_BYTES + 1)})


def test_envelope_requires_positive_seq_and_utc() -> None:
    kw = {
        "event_id": uuid.uuid4(),
        "run_id": uuid.uuid4(),
        "trace_id": uuid.uuid4(),
        "span_id": uuid.uuid4(),
        "type": "RUN_STARTED",
        "actor_type": "system",
        "actor_id": "eios",
        "status": "completed",
    }
    with pytest.raises(ValidationError):
        EventEnvelope(seq=0, timestamp=datetime.now(UTC), **kw)  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        EventEnvelope(seq=1, timestamp=datetime(2026, 1, 1), **kw)  # type: ignore[arg-type]  # naive


def test_envelope_tolerates_unknown_event_types() -> None:
    """Forward compatibility: a newer producer's type must not break an older consumer."""
    e = EventEnvelope(
        event_id=uuid.uuid4(),
        run_id=uuid.uuid4(),
        trace_id=uuid.uuid4(),
        span_id=uuid.uuid4(),
        seq=1,
        type="SOMETHING_FROM_THE_FUTURE",
        timestamp=datetime.now(UTC),
        actor_type=ActorType.SYSTEM,
        actor_id="x",
        status=EventStatus.COMPLETED,
    )
    assert e.type == "SOMETHING_FROM_THE_FUTURE"
    assert e.schema_version == 1


def test_json_contract_keys() -> None:
    e = EventEnvelope(
        event_id=uuid.uuid4(),
        run_id=uuid.uuid4(),
        trace_id=uuid.uuid4(),
        span_id=uuid.uuid4(),
        seq=1,
        type=EventType.TOOL_STARTED,
        timestamp=datetime.now(UTC),
        actor_type=ActorType.TOOL,
        actor_id="t",
        status=EventStatus.STARTED,
    )
    assert set(e.model_dump(mode="json")) == {
        "event_id", "run_id", "trace_id", "span_id", "parent_span_id", "seq", "schema_version",
        "type", "timestamp", "actor_type", "actor_id", "status", "summary", "data",
    }  # fmt: skip
