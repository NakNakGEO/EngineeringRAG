"""Domain error hierarchy."""

from __future__ import annotations


class DomainError(Exception):
    """Base class for violations of domain invariants."""


class InvalidTransitionError(DomainError):
    """An aggregate was asked to make a state transition that is not allowed."""


class NotFoundError(DomainError):
    """A referenced aggregate does not exist."""


class BudgetExceededError(DomainError):
    """An execution budget limit was reached."""

    def __init__(self, kind: str, limit: float, used: float) -> None:
        super().__init__(f"budget exceeded: {kind} (limit={limit}, used={used})")
        self.kind = kind
        self.limit = limit
        self.used = used
