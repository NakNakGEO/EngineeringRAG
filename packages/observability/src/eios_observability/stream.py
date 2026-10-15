"""Live event following: resumable by ``seq`` cursor, bounded batches, terminates with the run."""

from __future__ import annotations

import time
import uuid
from collections.abc import AsyncIterator

from eios_domain.events import EventEnvelope
from eios_observability.hub import EventHub
from eios_observability.store import EventStore, RunRepository


async def follow_events(
    *,
    events: EventStore,
    runs: RunRepository,
    hub: EventHub,
    run_id: uuid.UUID,
    after_seq: int = 0,
    poll_interval: float = 0.25,
    batch_size: int = 200,
    max_seconds: float | None = None,
    keepalive_seconds: float | None = None,
) -> AsyncIterator[EventEnvelope | None]:
    """Yield events of ``run_id`` with ``seq > after_seq`` as they are appended.

    Ends after the run reaches a terminal state and every event has been delivered, or after
    ``max_seconds``. Consumers resume after a disconnect by passing the last seq they saw.
    When ``keepalive_seconds`` is set, ``None`` is yielded after that long without any event so
    transports can send a keepalive.
    """
    started = time.monotonic()
    last_output = started
    cursor = after_seq
    while True:
        # Check terminal state BEFORE the final drain so no event can slip in unseen.
        run = await runs.get(run_id)
        terminal = run is not None and run.status.is_terminal
        page = await events.list_events(run_id, after_seq=cursor, limit=batch_size)
        for envelope in page.items:
            cursor = envelope.seq
            last_output = time.monotonic()
            yield envelope
        if page.has_more:
            continue  # backlog: keep draining without sleeping, in bounded batches
        if terminal or run is None:
            return
        if max_seconds is not None and time.monotonic() - started >= max_seconds:
            return
        if keepalive_seconds is not None and time.monotonic() - last_output >= keepalive_seconds:
            last_output = time.monotonic()
            yield None
        await hub.wait(run_id, poll_interval)
