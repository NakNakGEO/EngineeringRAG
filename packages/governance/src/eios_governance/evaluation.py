"""Evaluation: golden-case suites for providers; results feed routing, never authority.

Evaluation can raise a provider's score (a routing input) and, when its suite passes, supply the
``verification_passed`` evidence for EXPERIMENTAL -> VERIFIED. It can never reach TRUSTED (a human
decision), never un-quarantine, and never make a forbidden, unapproved or demoted provider
routable: those gates live in the registry and the Policy Engine, which evaluation does not touch.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import sqlalchemy as sa
import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy.ext.asyncio import AsyncEngine

from eios_capability import ProviderRow, RegistryService
from eios_domain.events import EventType
from eios_domain.ids import new_id, utcnow
from eios_domain.registry import RegistryState, TransitionError
from eios_observability.recorder import RunContext
from eios_storage.tables.governance import evaluation_run as run_t

ProviderRunner = Callable[[ProviderRow, dict[str, Any]], Awaitable[dict[str, Any]]]
VERIFY_MIN_SAMPLES = 3


class EvalCase(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(min_length=1, max_length=60)
    arguments: dict[str, Any] = Field(default_factory=dict)
    expect: dict[str, Any] = Field(min_length=1)


class EvalSuite(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    provider: str
    capability: str
    cases: list[EvalCase] = Field(min_length=1, max_length=200)
    min_pass_rate: float = Field(default=1.0, ge=0.5, le=1.0)


class CaseResult(BaseModel):
    id: str
    passed: bool
    detail: str = ""


class EvalReport(BaseModel):
    run_id: uuid.UUID
    provider: str
    version: str
    capability: str
    score: float
    samples: int
    passed: int
    results: list[CaseResult]
    verified: bool = False
    note: str = ""


_OPS: dict[str, Callable[[Any, Any], bool]] = {
    "contains": lambda g, a: a in g,
    "gte": lambda g, a: g >= a,
    "lte": lambda g, a: g <= a,
    "in": lambda g, a: g in a,
    "length": lambda g, a: len(g) == a,
}


def lookup(output: Any, path: str) -> Any:
    cur = output
    for part in path.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        elif isinstance(cur, list) and part.isdigit() and int(part) < len(cur):
            cur = cur[int(part)]
        else:
            raise KeyError(path)
    return cur


def check_expectation(output: dict[str, Any], expect: dict[str, Any]) -> tuple[bool, str]:
    """``expect`` maps dotted paths to a literal or one operator: contains/gte/lte/in/length."""
    for path, want in expect.items():
        try:
            got = lookup(output, path)
        except KeyError:
            return False, f"'{path}' missing from output"
        if isinstance(want, dict) and len(want) == 1:
            ((op, arg),) = want.items()
            fn = _OPS.get(op)
            if fn is None:
                return False, f"unknown operator '{op}'"
            try:
                good = fn(got, arg)
            except TypeError:
                good = False
        else:
            good = got == want
        if not good:
            return False, f"'{path}': expected {want!r}, got {str(got)[:120]!r}"
    return True, ""


def load_suites(directory: Path) -> tuple[list[EvalSuite], list[str]]:
    suites: list[EvalSuite] = []
    errors: list[str] = []
    if not directory.is_dir():
        return suites, errors
    for path in sorted(directory.glob("*.y*ml")):
        if path.is_symlink() or path.stat().st_size > 500_000:
            errors.append(f"{path.name}: skipped")
            continue
        try:
            suites.append(
                EvalSuite.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
            )
        except (yaml.YAMLError, ValidationError, UnicodeDecodeError) as exc:
            errors.append(f"{path.name}: {str(exc)[:200]}")
    return suites, errors


class EvaluationService:
    def __init__(
        self, engine: AsyncEngine, registry: RegistryService, runner: ProviderRunner
    ) -> None:
        self._engine = engine
        self._registry = registry
        self._runner = runner

    async def run_suite(
        self,
        suite: EvalSuite,
        *,
        actor: str = "evaluation",
        ctx: RunContext | None = None,
        promote: bool = False,
    ) -> EvalReport:
        provider = await self._registry.store.get_provider(suite.provider)
        if provider is None:
            raise LookupError(f"provider '{suite.provider}' is not registered")
        if suite.capability not in provider.capabilities:
            raise LookupError(f"provider '{suite.provider}' does not claim '{suite.capability}'")
        started = utcnow()
        t0 = time.monotonic()
        results: list[CaseResult] = []
        for case in suite.cases:
            try:
                output = await self._runner(provider, case.arguments)
                ok, detail = check_expectation(output, case.expect)
            except Exception as exc:
                ok, detail = False, f"{type(exc).__name__}: {str(exc)[:200]}"
            results.append(CaseResult(id=case.id, passed=ok, detail=detail))
        passed = sum(r.passed for r in results)
        score = passed / len(results)
        run_id = new_id()
        async with self._engine.begin() as conn:
            await conn.execute(
                sa.insert(run_t).values(
                    id=run_id,
                    kind="provider_suite",
                    subject=provider.id,
                    subject_version=provider.version,
                    capability_id=suite.capability,
                    score=score,
                    samples=len(results),
                    passed=passed,
                    details={
                        "results": [r.model_dump() for r in results],
                        "duration_ms": int((time.monotonic() - t0) * 1000),
                    },
                    evaluator=actor[:200],
                    started_at=started,
                    finished_at=utcnow(),
                )
            )
        await self._registry.store.record_evaluation(
            provider.id,
            provider.version,
            suite.capability,
            score=score,
            samples=len(results),
            weaknesses=[r.id for r in results if not r.passed],
        )
        report = EvalReport(
            run_id=run_id,
            provider=provider.id,
            version=provider.version,
            capability=suite.capability,
            score=score,
            samples=len(results),
            passed=passed,
            results=results,
        )
        if promote:
            await self._maybe_verify(provider, suite, report, actor)
        if ctx is not None:
            await ctx.emit(
                EventType.EVALUATION_RECORDED,
                f"{provider.id}: {passed}/{len(results)} passed",
                data={
                    "provider": provider.id,
                    "capability": suite.capability,
                    "score": score,
                    "samples": len(results),
                    "verified": report.verified,
                },
            )
        return report

    async def _maybe_verify(
        self, provider: ProviderRow, suite: EvalSuite, report: EvalReport, actor: str
    ) -> None:
        if provider.state is not RegistryState.EXPERIMENTAL:
            report.note = f"not promoted: provider is {provider.state.value}"
            return
        if provider.health_status == "fail":
            report.note = "not promoted: provider is unhealthy"
            return
        if report.samples < VERIFY_MIN_SAMPLES:
            report.note = f"not promoted: needs at least {VERIFY_MIN_SAMPLES} cases"
            return
        if report.score < suite.min_pass_rate:
            report.note = f"not promoted: pass rate {report.score:.0%} < {suite.min_pass_rate:.0%}"
            return
        try:
            await self._registry.transition(
                "provider",
                provider.id,
                provider.version,
                RegistryState.VERIFIED,
                actor=actor,
                reason=f"evaluation suite passed ({report.passed}/{report.samples})",
                verification_passed=True,
            )
        except TransitionError as exc:  # e.g. generated/downloaded artifacts need a human first
            report.note = f"not promoted: {exc}"
            return
        report.verified = True

    async def history(self, subject: str, limit: int = 20) -> list[dict[str, Any]]:
        async with self._engine.connect() as conn:
            rows = (
                await conn.execute(
                    sa.select(run_t)
                    .where(run_t.c.subject == subject)
                    .order_by(run_t.c.started_at.desc())
                    .limit(limit)
                )
            ).all()
        return [dict(r._mapping) for r in rows]
