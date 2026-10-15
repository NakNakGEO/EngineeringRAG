"""Capability router: capability + target + environment -> a registered, healthy provider.

The router never guesses. An unknown capability, or a known capability with no routable,
healthy, compatible provider, yields an explicit non-selection with reasons - the caller can
then escalate to the Capability Gap Resolver instead of improvising a tool.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from eios_capability.manifests import ToolManifest
from eios_capability.store import MetricRow, ProviderRow, RegistryStore
from eios_domain.events import EventStatus, EventType
from eios_domain.registry import RegistryState, is_forbidden_capability, is_routable
from eios_observability.recorder import RunContext

_STATE_SCORE = {
    RegistryState.TRUSTED: 30.0,
    RegistryState.VERIFIED: 20.0,
    RegistryState.EXPERIMENTAL: 5.0,
}
_MATURITY_SCORE = {"stable": 6.0, "beta": 3.0, "experimental": 0.0}
_TRUST_SCORE = {"owner": 6.0, "reviewed": 4.0, "community": 1.0, "unrated": 0.0}


class RoutingStatus(StrEnum):
    SELECTED = "selected"
    NO_PROVIDER = "no_provider"
    UNKNOWN_CAPABILITY = "unknown_capability"
    FORBIDDEN = "forbidden"


@dataclass(frozen=True)
class RoutingRequest:
    capability: str
    language: str | None = None
    extension: str | None = None
    kind: str | None = None
    path: str | None = None
    network_allowed: bool = False
    allow_experimental: bool = True
    require_provider: str | None = None  # pin a specific provider id (still must be routable)


@dataclass
class Candidate:
    provider: ProviderRow
    score: float
    reasons: list[str] = field(default_factory=list)


@dataclass
class RoutingDecision:
    status: RoutingStatus
    capability: str
    selected: Candidate | None = None
    alternatives: list[Candidate] = field(default_factory=list)
    rejected: list[tuple[str, str]] = field(default_factory=list)  # (provider@version, reason)
    message: str = ""

    def summary(self) -> dict[str, Any]:
        sel = self.selected
        return {
            "status": self.status.value,
            "capability": self.capability,
            "message": self.message,
            "selected": (
                {
                    "provider": sel.provider.id,
                    "version": sel.provider.version,
                    "state": sel.provider.state.value,
                    "score": round(sel.score, 2),
                    "reasons": sel.reasons,
                }
                if sel
                else None
            ),
            "alternatives": [
                {
                    "provider": c.provider.id,
                    "version": c.provider.version,
                    "score": round(c.score, 2),
                }
                for c in self.alternatives
            ],
            "rejected": [{"provider": p, "reason": r} for p, r in self.rejected],
        }


def _matches(pattern: str, req: RoutingRequest) -> bool:
    if pattern == "*":
        return True
    key, _, value = pattern.partition(":")
    if key == "language":
        return req.language is not None and req.language.lower() == value.lower()
    if key == "ext":
        return req.extension is not None and req.extension.lower().lstrip(
            "."
        ) == value.lower().lstrip(".")
    if key == "kind":
        return req.kind is not None and req.kind.lower() == value.lower()
    if key == "path":
        return req.path is not None and req.path.startswith(value)
    return False


def _specificity(manifest: ToolManifest, req: RoutingRequest) -> float:
    specific = [t for t in manifest.supported_targets if t != "*" and _matches(t, req)]
    return 8.0 if specific else 0.0


class CapabilityRouter:
    def __init__(self, store: RegistryStore) -> None:
        self._store = store

    async def resolve(
        self, request: RoutingRequest, ctx: RunContext | None = None
    ) -> RoutingDecision:
        if ctx is not None:
            await ctx.emit(
                EventType.CAPABILITY_REQUESTED,
                f"capability requested: {request.capability}",
                data={
                    "capability": request.capability,
                    "language": request.language,
                    "extension": request.extension,
                    "kind": request.kind,
                },
            )
        decision = await self._decide(request)
        if ctx is not None:
            if decision.status is RoutingStatus.SELECTED and decision.selected:
                await ctx.emit(
                    EventType.TOOL_SELECTED,
                    f"{decision.selected.provider.id} selected for {request.capability}",
                    data=decision.summary(),
                )
            else:
                await ctx.emit(
                    EventType.CAPABILITY_GAP_DETECTED,
                    decision.message or f"no provider for {request.capability}",
                    status=EventStatus.FAILED,
                    data=decision.summary(),
                )
        return decision

    async def _decide(self, req: RoutingRequest) -> RoutingDecision:
        if is_forbidden_capability(req.capability):
            return RoutingDecision(
                RoutingStatus.FORBIDDEN,
                req.capability,
                message="this capability is forbidden by Root Policy and cannot be routed",
            )
        if await self._store.get_capability(req.capability) is None:
            return RoutingDecision(
                RoutingStatus.UNKNOWN_CAPABILITY,
                req.capability,
                message=f"capability '{req.capability}' is not registered; it will not be guessed",
            )
        providers = await self._store.list_providers(capability_id=req.capability)
        metrics = await self._store.metrics_for(req.capability)
        candidates: list[Candidate] = []
        rejected: list[tuple[str, str]] = []
        for p in providers:
            label = f"{p.id}@{p.version}"
            reason = self._reject_reason(p, req)
            if reason:
                rejected.append((label, reason))
                continue
            candidates.append(self._score(p, req, metrics.get((p.id, p.version))))
        # Only the newest compatible version of a provider competes with other providers.
        newest: dict[str, Candidate] = {}
        for c in candidates:
            cur = newest.get(c.provider.id)
            if cur is None or _vkey(c.provider.version) > _vkey(cur.provider.version):
                if cur is not None:
                    rejected.append((f"{cur.provider.id}@{cur.provider.version}", "older version"))
                newest[c.provider.id] = c
            else:
                rejected.append((f"{c.provider.id}@{c.provider.version}", "older version"))
        ranked = sorted(newest.values(), key=lambda c: (-c.score, c.provider.id))
        if not ranked:
            return RoutingDecision(
                RoutingStatus.NO_PROVIDER,
                req.capability,
                rejected=rejected,
                message=f"no routable, healthy, compatible provider for '{req.capability}'",
            )
        return RoutingDecision(
            RoutingStatus.SELECTED,
            req.capability,
            selected=ranked[0],
            alternatives=ranked[1:4],
            rejected=rejected,
        )

    @staticmethod
    def _reject_reason(p: ProviderRow, req: RoutingRequest) -> str | None:
        if req.require_provider and p.id != req.require_provider:
            return "not the requested provider"
        if not is_routable(p.state, p.origin, approved=p.approved_by is not None):
            if p.state in _STATE_SCORE:
                return f"{p.origin.value} provider awaits human approval"
            return f"state {p.state.value}"
        if p.state is RegistryState.EXPERIMENTAL and not req.allow_experimental:
            return "experimental providers excluded by request"
        if p.health_status == "fail":
            return f"unhealthy: {p.health_detail or 'health check failed'}"
        manifest = ToolManifest.model_validate(p.manifest)
        if any(_matches(t, req) for t in manifest.unsupported_targets):
            return "target explicitly unsupported"
        if not any(_matches(t, req) for t in manifest.supported_targets):
            return "target not supported"
        if manifest.network_required and not req.network_allowed:
            return "requires network access which is not allowed here"
        return None

    @staticmethod
    def _score(p: ProviderRow, req: RoutingRequest, metric: MetricRow | None) -> Candidate:
        manifest = ToolManifest.model_validate(p.manifest)
        reasons: list[str] = []
        score = _STATE_SCORE.get(p.state, 0.0)
        reasons.append(f"state {p.state.value}")
        score += p.priority * 0.2
        score += _MATURITY_SCORE.get(p.maturity, 0.0) + _TRUST_SCORE.get(p.trust, 0.0)
        if (spec := _specificity(manifest, req)) > 0:
            score += spec
            reasons.append("specific target match")
        if p.health_status == "ok":
            score += 3.0
            reasons.append("healthy")
        if metric is not None and metric.samples >= 3 and metric.success_rate is not None:
            score += metric.success_rate * 15.0
            reasons.append(f"success rate {metric.success_rate:.0%} over {metric.samples} runs")
            if metric.avg_latency_ms is not None:
                score -= min(metric.avg_latency_ms / 1000.0, 5.0)
        if metric is not None and metric.eval_score is not None and metric.eval_samples >= 3:
            score += metric.eval_score * 10.0
            reasons.append(f"eval score {metric.eval_score:.2f}")
        return Candidate(p, score, reasons)


def _vkey(version: str) -> tuple[int, ...]:
    core = version.split("-", 1)[0].split("+", 1)[0]
    try:
        return tuple(int(x) for x in core.split("."))
    except ValueError:
        return (0,)
