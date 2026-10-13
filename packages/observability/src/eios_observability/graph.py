"""Derive the live execution graph (spans as nodes) from the event log."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel

from eios_domain.events import EventEnvelope, EventStatus


class GraphNode(BaseModel):
    span_id: uuid.UUID
    parent_span_id: uuid.UUID | None
    label: str  # "STAGE/node #attempt" for workflow node spans, else the first event's type
    actor_type: str
    actor_id: str
    status: str  # outcome of the span: its closing event, or "started" while it is open
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


def _closes_span(e: EventEnvelope) -> bool:
    """A failure or denial, or a ``*_COMPLETED`` event, decides the outcome of its span.

    Informational events (GOAL_CLASSIFIED, AGENT_SELECTED, POLICY_ALLOWED...) are also
    ``completed`` but say nothing about whether the span as a whole has finished.
    """
    status = EventStatus(e.status)
    return status in (EventStatus.FAILED, EventStatus.DENIED) or (
        status is EventStatus.COMPLETED and str(e.type).endswith("_COMPLETED")
    )


def _node_label(e: EventEnvelope) -> str | None:
    node, stage = e.data.get("node"), e.data.get("stage")
    if not node or not stage:
        return None
    attempt = e.data.get("attempt")
    return f"{stage}/{node}" + (f" #{attempt}" if attempt else "")


def build_graph(
    run_id: uuid.UUID, events: list[EventEnvelope], *, truncated: bool = False
) -> RunGraph:
    nodes: dict[uuid.UUID, GraphNode] = {}
    outcome: dict[uuid.UUID, str] = {}  # span -> status of its latest closing event
    opened: set[uuid.UUID] = set()  # spans that contain a STARTED event
    labelled: set[uuid.UUID] = set()
    for e in events:
        node = nodes.get(e.span_id)
        status = EventStatus(e.status).value
        if node is None:
            node = nodes[e.span_id] = GraphNode(
                span_id=e.span_id,
                parent_span_id=e.parent_span_id,
                label=str(e.type),
                actor_type=e.actor_type.value,
                actor_id=e.actor_id,
                status=status,
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
            node.status = status
            node.last_event_at = e.timestamp
            node.last_event_type = str(e.type)
        if status == EventStatus.STARTED.value:
            opened.add(e.span_id)
        if _closes_span(e):
            outcome[e.span_id] = status
        if e.span_id not in labelled and (label := _node_label(e)) is not None:
            node.label = label
            labelled.add(e.span_id)
    for span_id, node in nodes.items():
        if span_id in outcome:
            node.status = outcome[span_id]
        elif span_id in opened:
            node.status = EventStatus.STARTED.value
    edges = [
        GraphEdge(source=n.parent_span_id, target=n.span_id)
        for n in nodes.values()
        if n.parent_span_id is not None and n.parent_span_id in nodes
    ]
    return RunGraph(run_id=run_id, nodes=list(nodes.values()), edges=edges, truncated=truncated)
