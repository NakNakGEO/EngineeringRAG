"""Derive the live execution graph (spans as nodes) from the event log."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel

from eios_domain.events import EventEnvelope, EventStatus


class GraphNode(BaseModel):
    span_id: uuid.UUID
    parent_span_id: uuid.UUID | None
    label: str  # type of the first event in the span
    actor_type: str
    actor_id: str
    status: str  # status of the latest event in the span
    first_seq: int
    last_seq: int
    event_count: int
    started_at: datetime
    last_event_at: datetime
    last_event_type: str


class GraphEdge(BaseModel):
    source: uuid.UUID
    target: uuid.UUID


class RunGraph(BaseModel):
    run_id: uuid.UUID
    nodes: list[GraphNode]
    edges: list[GraphEdge]
    truncated: bool = False


def build_graph(
    run_id: uuid.UUID, events: list[EventEnvelope], *, truncated: bool = False
) -> RunGraph:
    nodes: dict[uuid.UUID, GraphNode] = {}
    for e in events:
        node = nodes.get(e.span_id)
        if node is None:
            nodes[e.span_id] = GraphNode(
                span_id=e.span_id,
                parent_span_id=e.parent_span_id,
                label=str(e.type),
                actor_type=e.actor_type.value,
                actor_id=e.actor_id,
                status=EventStatus(e.status).value,
                first_seq=e.seq,
                last_seq=e.seq,
                event_count=1,
                started_at=e.timestamp,
                last_event_at=e.timestamp,
                last_event_type=str(e.type),
            )
        else:
            node.last_seq = e.seq
            node.event_count += 1
            node.status = EventStatus(e.status).value
            node.last_event_at = e.timestamp
            node.last_event_type = str(e.type)
    edges = [
        GraphEdge(source=n.parent_span_id, target=n.span_id)
        for n in nodes.values()
        if n.parent_span_id is not None and n.parent_span_id in nodes
    ]
    return RunGraph(run_id=run_id, nodes=list(nodes.values()), edges=edges, truncated=truncated)
