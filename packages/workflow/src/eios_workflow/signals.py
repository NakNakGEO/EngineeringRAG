"""Project facts (impact analysis, coverage) folded into the signals that drive team selection.

The workflow package never imports the retrieval or project-intelligence packages: the runtime
container injects a :data:`FactsProvider` that does the analysis and returns plain
:class:`ProjectFacts`. Facts only ever *raise* what the caller said (risk, dependents, missing
tests, SQL); they never lower it.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

from eios_domain.policy import Risk
from eios_workflow.team import TeamSignals

RISK_RANK = {Risk.LOW: 0, Risk.MEDIUM: 1, Risk.HIGH: 2, Risk.CRITICAL: 3}


@dataclass(frozen=True)
class ProjectFacts:
    """What analysing the targets inside the project found."""

    risk: Risk = Risk.LOW
    dependents: int = 0  # distinct files that depend on the targets
    tests_missing: bool = False
    touches_sql: bool = False
    coverage: float | None = None  # fraction of the project's parsable files that is understood
    summary: dict[str, Any] = field(default_factory=dict)  # small and JSON-able; stored on the run


FactsProvider = Callable[[uuid.UUID, Sequence[str], Sequence[str]], Awaitable[ProjectFacts]]


def max_risk(*risks: Risk) -> Risk:
    return max(risks, key=RISK_RANK.__getitem__)


def merge_signals(base: TeamSignals, facts: ProjectFacts | None) -> TeamSignals:
    """The caller's signals raised by what the project analysis found (risk = max of both)."""
    explicit = base.explicit_targets or bool(base.paths or base.symbols)
    if facts is None:
        return replace(base, explicit_targets=explicit)
    return replace(
        base,
        risk=max_risk(base.risk, facts.risk),
        dependents=max(base.dependents, facts.dependents),
        tests_missing=base.tests_missing or facts.tests_missing,
        touches_sql=base.touches_sql or facts.touches_sql,
        coverage=base.coverage if base.coverage is not None else facts.coverage,
        explicit_targets=explicit,
    )


def snapshot(
    effective: TeamSignals,
    *,
    caller_risk: Risk,
    facts: ProjectFacts | None,
    error: str | None = None,
) -> dict[str, Any]:
    """The JSON stored on the run: the effective signals and where they came from."""
    return {
        "risk": effective.risk.value,
        "caller_risk": caller_risk.value,
        "paths": [p[:300] for p in effective.paths[:50]],
        "symbols": [s[:300] for s in effective.symbols[:50]],
        "languages": sorted(effective.languages),
        "dependents": effective.dependents,
        "tests_missing": effective.tests_missing,
        "touches_sql": effective.touches_sql,
        "coverage": effective.coverage,
        "explicit_targets": effective.explicit_targets,
        "impact": facts.summary if facts is not None else None,
        "impact_error": error,
    }
