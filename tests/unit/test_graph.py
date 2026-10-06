from __future__ import annotations

import uuid
from datetime import UTC, datetime

from eios_domain.events import ActorType, EventEnvelope, EventStatus, EventType
from eios_observability.graph import build_graph


def _ev(
    seq: int,
    span: uuid.UUID,
    parent: uuid.UUID | None,
    type_: EventType,
    status: EventStatus = EventStatus.COMPLETED,
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
