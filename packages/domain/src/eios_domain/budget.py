"""Execution budget primitives (the Execution Governor builds on these in Phase 7)."""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from eios_domain.errors import BudgetExceededError

BudgetKind = Literal[
    "llm_calls",
    "agent_calls",
    "tool_calls",
    "tokens",
    "output_bytes",
    "retries",
    "loop_iterations",
]


class ExecutionBudget(BaseModel):
    """Hard limits for one run. ``None`` means unlimited for that dimension."""

    model_config = ConfigDict(frozen=True)

    max_llm_calls: int | None = Field(default=200, ge=0)
    max_agent_calls: int | None = Field(default=50, ge=0)
    max_tool_calls: int | None = Field(default=500, ge=0)
    max_tokens: int | None = Field(default=2_000_000, ge=0)
    max_output_bytes: int | None = Field(default=50_000_000, ge=0)
    max_retries: int | None = Field(default=5, ge=0)
    max_loop_iterations: int | None = Field(default=100, ge=0)
    max_recursion_depth: int | None = Field(default=8, ge=0)
    max_wall_seconds: float | None = Field(default=3600.0, gt=0)
    max_concurrency: int = Field(default=4, ge=1)


_LIMIT_FIELD: dict[str, str] = {
    "llm_calls": "max_llm_calls",
    "agent_calls": "max_agent_calls",
    "tool_calls": "max_tool_calls",
    "tokens": "max_tokens",
    "output_bytes": "max_output_bytes",
    "retries": "max_retries",
    "loop_iterations": "max_loop_iterations",
}


class BudgetTracker:
    """Mutable usage counter enforcing an :class:`ExecutionBudget`.

    Single-writer (one asyncio task owns it); the clock is injectable for deterministic tests.
    """

    def __init__(
        self, budget: ExecutionBudget | None = None, *, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self.budget = budget or ExecutionBudget()
        self._clock = clock
        self._started = clock()
        self._used: dict[str, float] = dict.fromkeys(_LIMIT_FIELD, 0)
        self._depth = 0

    @property
    def usage(self) -> dict[str, float]:
        return {**self._used, "recursion_depth": self._depth, "wall_seconds": self.elapsed}

    @property
    def elapsed(self) -> float:
        return self._clock() - self._started

    def charge(self, kind: BudgetKind, amount: float = 1) -> None:
        """Record usage; raises :class:`BudgetExceededError` if it would pass the limit."""
        if amount < 0:
            raise ValueError("amount must be non-negative")
        self.check_wall_time()
        limit = getattr(self.budget, _LIMIT_FIELD[kind])
        new_total = self._used[kind] + amount
        if limit is not None and new_total > limit:
            raise BudgetExceededError(kind, limit, new_total)
        self._used[kind] = new_total

    def check_wall_time(self) -> None:
        limit = self.budget.max_wall_seconds
        if limit is not None and self.elapsed > limit:
            raise BudgetExceededError("wall_seconds", limit, self.elapsed)

    @contextmanager
    def nested(self) -> Iterator[int]:
        """Guard recursion depth: ``with tracker.nested(): ...``."""
        limit = self.budget.max_recursion_depth
        if limit is not None and self._depth + 1 > limit:
            raise BudgetExceededError("recursion_depth", limit, self._depth + 1)
        self._depth += 1
        try:
            yield self._depth
        finally:
            self._depth -= 1
