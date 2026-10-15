"""Domain error hierarchy."""

from __future__ import annotations


class DomainError(Exception):
    """Base class for violations of domain invariants."""


class InvalidTransitionError(DomainError):
    """An aggregate was asked to make a state transition that is not allowed."""


class NotFoundError(DomainError):
    """A referenced aggregate does not exist."""


def require_found[T](value: T | None, what: str) -> T:
    """Return ``value`` or raise :class:`NotFoundError` (never use ``assert`` for this)."""
    if value is None:
        raise NotFoundError(f"{what} not found")
    return value


class BudgetExceededError(DomainError):
    """An execution budget limit was reached."""

    def __init__(self, kind: str, limit: float, used: float) -> None:
        super().__init__(f"budget exceeded: {kind} (limit={limit}, used={used})")
        self.kind = kind
        self.limit = limit
        self.used = used
