"""In-process wake-up so same-process streams react immediately; others fall back to polling."""

from __future__ import annotations

import asyncio
import contextlib
import uuid


class EventHub:
    def __init__(self) -> None:
        self._waiters: dict[uuid.UUID, set[asyncio.Event]] = {}

    def notify(self, run_id: uuid.UUID) -> None:
        for waiter in self._waiters.get(run_id, ()):
            waiter.set()

    async def wait(self, run_id: uuid.UUID, wait_seconds: float) -> None:
        """Return when notified for ``run_id`` or after ``wait_seconds``."""
        waiter = asyncio.Event()
        self._waiters.setdefault(run_id, set()).add(waiter)
        try:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(waiter.wait(), wait_seconds)
        finally:
            waiters = self._waiters.get(run_id)
            if waiters is not None:
                waiters.discard(waiter)
                if not waiters:
                    self._waiters.pop(run_id, None)
