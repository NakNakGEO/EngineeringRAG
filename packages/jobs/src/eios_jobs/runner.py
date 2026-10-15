"""Worker-side job execution: claim, run with a heartbeat, complete or fail with backoff."""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from eios_core.logging import get_logger
from eios_jobs.queue import Job, JobQueue

JobHandler = Callable[[Job], Awaitable[dict[str, Any] | None]]
_log = get_logger("eios.jobs")


class PermanentJobError(Exception):
    """Raise from a handler when retrying cannot help (job goes straight to ``dead``)."""


class JobRunner:
    def __init__(
        self,
        queue: JobQueue,
        handlers: dict[str, JobHandler],
        *,
        worker_id: str | None = None,
        lease_seconds: float = 30.0,
        base_retry_seconds: float = 2.0,
    ) -> None:
        self._queue = queue
        self._handlers = handlers
        self.worker_id = worker_id or f"worker-{uuid.uuid4().hex[:8]}"
        self._lease = lease_seconds
        self._base_retry = base_retry_seconds

    def register(self, type_: str, handler: JobHandler) -> None:
        self._handlers[type_] = handler

    async def run_once(self) -> bool:
        """Process at most one job. Returns True if a job was claimed."""
        if not self._handlers:
            return False
        await self._queue.reclaim_expired()
        job = await self._queue.claim(
            self.worker_id, types=list(self._handlers), lease_seconds=self._lease
        )
        if job is None:
            return False
        await self._execute(job)
        return True

    async def _heartbeat_loop(self, job: Job) -> None:
        interval = max(self._lease / 3, 0.05)
        while True:
            await asyncio.sleep(interval)
            if not await self._queue.heartbeat(job.id, self.worker_id, self._lease):
                _log.warning("job_lease_lost", job_id=str(job.id))
                return

    async def _execute(self, job: Job) -> None:
        handler = self._handlers[job.type]
        beat = asyncio.create_task(self._heartbeat_loop(job))
        try:
            result = await handler(job)
        except PermanentJobError as exc:
            await self._queue.fail(job.id, self.worker_id, str(exc), permanent=True)
            _log.error("job_failed_permanently", job_id=str(job.id), type=job.type, error=str(exc))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            delay = self._base_retry * (2 ** (job.attempts - 1))
            failed = await self._queue.fail(
                job.id,
                self.worker_id,
                f"{type(exc).__name__}: {exc}",
                retry_delay_seconds=delay,
            )
            _log.warning(
                "job_failed",
                job_id=str(job.id),
                type=job.type,
                attempt=job.attempts,
                status=failed.status if failed else "unknown",
                error=repr(exc),
            )
        else:
            if not await self._queue.complete(job.id, self.worker_id, result):
                _log.warning("job_completion_rejected", job_id=str(job.id))
        finally:
            beat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await beat

    async def run_forever(self, stop: asyncio.Event, *, poll_interval: float = 1.0) -> None:
        while not stop.is_set():
            try:
                worked = await self.run_once()
            except Exception as exc:  # the loop must survive transient database errors
                _log.error("job_runner_error", error=repr(exc))
                worked = False
            if not worked:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), poll_interval)
