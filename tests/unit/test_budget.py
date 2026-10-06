from __future__ import annotations

import pytest

from eios_domain.budget import BudgetTracker, ExecutionBudget
from eios_domain.errors import BudgetExceededError


def test_charges_accumulate_and_enforce_limit() -> None:
    tracker = BudgetTracker(ExecutionBudget(max_tool_calls=3))
    for _ in range(3):
        tracker.charge("tool_calls")
    with pytest.raises(BudgetExceededError) as info:
        tracker.charge("tool_calls")
    assert (info.value.kind, info.value.limit, info.value.used) == ("tool_calls", 3, 4)
    assert tracker.usage["tool_calls"] == 3  # the failed charge is not recorded


def test_unlimited_dimension() -> None:
    tracker = BudgetTracker(ExecutionBudget(max_tokens=None))
    tracker.charge("tokens", 10**9)


def test_negative_charge_rejected() -> None:
    with pytest.raises(ValueError, match="non-negative"):
        BudgetTracker().charge("tokens", -1)


def test_wall_clock_limit_uses_injected_clock() -> None:
    now = [0.0]
    tracker = BudgetTracker(ExecutionBudget(max_wall_seconds=10), clock=lambda: now[0])
    tracker.charge("llm_calls")
    now[0] = 11.0
    with pytest.raises(BudgetExceededError) as info:
        tracker.charge("llm_calls")
    assert info.value.kind == "wall_seconds"


def test_recursion_depth_guard() -> None:
    tracker = BudgetTracker(ExecutionBudget(max_recursion_depth=2))
    with tracker.nested(), tracker.nested():
        assert tracker.usage["recursion_depth"] == 2
        with pytest.raises(BudgetExceededError), tracker.nested():
            pass
    assert tracker.usage["recursion_depth"] == 0


def test_zero_limit_blocks_everything() -> None:
    with pytest.raises(BudgetExceededError):
        BudgetTracker(ExecutionBudget(max_llm_calls=0)).charge("llm_calls")
