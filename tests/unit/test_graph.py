from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from eios_domain.events import ActorType, EventEnvelope, EventStatus, EventType
from eios_observability.graph import build_graph


def _ev(
    seq: int,
    span: uuid.UUID,
    parent: uuid.UUID | None,
    type_: EventType,
    status: EventStatus = EventStatus.COMPLETED,
    data: dict[str, Any] | None = None,
) -> EventEnvelope:
    return EventEnvelope(
        event_id=uuid.uuid4(),
        run_id=RUN,
        trace_id=uuid.uuid4(),
        span_id=span,
        parent_span_id=parent,
        seq=seq,
        type=type_,
        timestamp=datetime.now(UTC),
        actor_type=ActorType.SYSTEM,
        actor_id="a",
        status=status,
        data=data or {},
    )


RUN = uuid.uuid4()


def test_graph_nodes_edges_and_status() -> None:
    root, child, grand = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    events = [
        _ev(1, root, None, EventType.RUN_STARTED, EventStatus.STARTED),
        _ev(2, child, root, EventType.TOOL_STARTED, EventStatus.STARTED),
        _ev(3, grand, child, EventType.LLM_STARTED, EventStatus.STARTED),
        _ev(4, child, root, EventType.TOOL_COMPLETED),
        _ev(5, root, None, EventType.RUN_COMPLETED),
    ]
    graph = build_graph(RUN, events)
    nodes = {n.span_id: n for n in graph.nodes}
    assert len(nodes) == 3
    assert nodes[root].label == "RUN_STARTED" and nodes[root].last_event_type == "RUN_COMPLETED"
    assert nodes[child].status == "completed" and nodes[child].event_count == 2
    assert nodes[grand].status == "started"
    assert {(e.source, e.target) for e in graph.edges} == {(root, child), (child, grand)}


def test_orphan_parent_produces_no_dangling_edge() -> None:
    graph = build_graph(RUN, [_ev(1, uuid.uuid4(), uuid.uuid4(), EventType.TOOL_STARTED)])
    assert graph.edges == []


def _node_events(
    seq: int, span: uuid.UUID, root: uuid.UUID, attempt: int, *, passed: bool
) -> list[EventEnvelope]:
    data = {"node": "understand", "stage": "UNDERSTAND", "attempt": attempt}
    return [
        _ev(seq, span, root, EventType.WORKFLOW_NODE_STARTED, EventStatus.STARTED, data),
        _ev(
            seq + 1,
            span,
            root,
            EventType.WORKFLOW_NODE_COMPLETED,
            EventStatus.COMPLETED if passed else EventStatus.FAILED,
            data,
        ),
    ]


def test_two_attempts_are_two_labelled_nodes_with_their_own_outcome() -> None:
    root, first, second = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    events = [
        _ev(1, root, None, EventType.RUN_STARTED, EventStatus.STARTED),
        _ev(2, root, None, EventType.GOAL_CLASSIFIED),
        _ev(3, root, None, EventType.AGENT_SELECTED),
        *_node_events(4, first, root, 1, passed=False),
        _ev(6, root, None, EventType.RETRY_STARTED),
        *_node_events(7, second, root, 2, passed=True),
        _ev(9, root, None, EventType.RUN_COMPLETED),
    ]
    graph = build_graph(RUN, events)
    nodes = {n.span_id: n for n in graph.nodes}
    assert len(nodes) == 3
    assert {(e.source, e.target) for e in graph.edges} == {(root, first), (root, second)}
    assert nodes[first].label == "UNDERSTAND/understand #1" and nodes[first].status == "failed"
    assert nodes[second].label == "UNDERSTAND/understand #2" and nodes[second].status == "completed"
    assert nodes[root].label == "RUN_STARTED" and nodes[root].status == "completed"


def test_an_open_root_stays_started_despite_informational_completed_events() -> None:
    root, attempt = uuid.uuid4(), uuid.uuid4()
    data = {"node": "design", "stage": "DESIGN", "attempt": 1}
    events = [
        _ev(1, root, None, EventType.RUN_STARTED, EventStatus.STARTED),
        _ev(2, root, None, EventType.GOAL_CLASSIFIED),
        _ev(3, root, None, EventType.AGENT_SELECTED),
        _ev(4, attempt, root, EventType.WORKFLOW_NODE_STARTED, EventStatus.STARTED, data),
    ]
    nodes = {n.span_id: n for n in build_graph(RUN, events).nodes}
    assert nodes[root].status == "started" and nodes[attempt].status == "started"


def test_a_failure_or_denial_is_not_overwritten_by_later_informational_events() -> None:
    root, failed, gate = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    events = [
        _ev(1, root, None, EventType.RUN_STARTED, EventStatus.STARTED),
        *_node_events(2, failed, root, 1, passed=False),
        _ev(4, failed, root, EventType.RETRY_STARTED),  # after the failure, same span
        _ev(5, gate, root, EventType.APPROVAL_REQUESTED, EventStatus.STARTED),
        _ev(6, gate, root, EventType.POLICY_DENIED, EventStatus.DENIED),
        _ev(7, gate, root, EventType.POLICY_ALLOWED),
    ]
    nodes = {n.span_id: n for n in build_graph(RUN, events).nodes}
    assert nodes[failed].status == "failed"
    assert nodes[gate].status == "denied"


def test_a_later_completion_closes_a_span_that_saw_a_denial() -> None:
    root, attempt = uuid.uuid4(), uuid.uuid4()
    data = {"node": "implement", "stage": "IMPLEMENT", "attempt": 1}
    events = [
        _ev(1, root, None, EventType.RUN_STARTED, EventStatus.STARTED),
        _ev(2, attempt, root, EventType.WORKFLOW_NODE_STARTED, EventStatus.STARTED, data),
        _ev(3, attempt, root, EventType.POLICY_DENIED, EventStatus.DENIED),
        _ev(4, attempt, root, EventType.WORKFLOW_NODE_COMPLETED, EventStatus.COMPLETED, data),
    ]
    nodes = {n.span_id: n for n in build_graph(RUN, events).nodes}
    assert nodes[attempt].status == "completed"
