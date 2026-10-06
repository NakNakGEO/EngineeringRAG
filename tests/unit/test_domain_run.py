from __future__ import annotations

import pytest

from eios_domain.errors import InvalidTransitionError
from eios_domain.run import Run, RunStatus


def test_new_run_is_pending_with_ids() -> None:
    run = Run(kind="demo", goal="g")
    assert run.status is RunStatus.PENDING
    assert run.id != run.trace_id
    assert run.created_at.tzinfo is not None


def test_happy_path_lifecycle() -> None:
    run = Run().start()
    assert run.status is RunStatus.RUNNING and run.started_at is not None
    done = run.complete()
    assert done.status is RunStatus.COMPLETED and done.finished_at is not None
    assert done.status.is_terminal


def test_failure_records_error_truncated() -> None:
    failed = Run().start().fail("x" * 5000)
    assert failed.status is RunStatus.FAILED
    assert failed.error is not None and len(failed.error) == 2000


@pytest.mark.parametrize(
    "build",
    [
        lambda: Run().complete(),  # pending -> completed
        lambda: Run().start().complete().start(),  # completed -> running
        lambda: Run().start().fail("e").complete(),
        lambda: Run().start().cancel().fail("e"),
        lambda: Run().start().start(),
    ],
)
def test_illegal_transitions_are_rejected(build) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(InvalidTransitionError):
        build()


def test_runs_are_immutable() -> None:
    run = Run()
    started = run.start()
    assert run.status is RunStatus.PENDING and started is not run
    with pytest.raises(Exception, match="frozen"):
        run.status = RunStatus.COMPLETED
