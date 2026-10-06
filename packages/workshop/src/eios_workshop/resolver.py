"""Capability Gap Resolver.

Resolution order (master plan Phase 10), cheapest and safest first:
existing -> compose existing -> generate skill -> generate agent -> generate tool -> external
subsystem. A policy check comes *before* anything is proposed: needs that read as external
database access are BLOCKED_BY_POLICY and never reach generation.
"""

from __future__ import annotations

import re

from eios_capability import CapabilityRouter, RegistryStore, RoutingRequest, RoutingStatus
from eios_domain.events import ActorType, EventStatus, EventType
from eios_domain.policy import PolicyEffect, PolicyRequest
from eios_domain.registry import detects_external_database_intent, is_forbidden_capability
from eios_observability.recorder import RunContext
from eios_policy import PolicyEngine
from eios_workshop.models import GapRequest, GapResolution, GapStatus, ProposalKind, ResolutionStep

_STOP = frozenset(
    [
        "a",
        "an",
        "the",
        "and",
        "or",
        "of",
        "to",
        "for",
        "in",
        "on",
        "with",
        "from",
        "by",
        "is",
        "are",
        "be",
        "it",
        "this",
        "that",
        "as",
        "at",
        "into",
        "over",
        "using",
        "use",
        "need",
        "needs",
        "want",
        "can",
        "able",
        "please",
        "new",
    ]
)
_EXECUTABLE = re.compile(
    r"\b(?:parse|decode|convert|compile|transpile|decompile|extract|render|run|execute|call|fetch|"
    r"download|scan|lint|format|minify|diff|hash|checksum|validate against|binary|pdf|docx|xlsx|"
    r"image|executable|dll|assembly|edit(?:s|ing)?|modify|patch|files?|filesystem)\b",
    re.IGNORECASE,
)
_ROLE = re.compile(
    r"\b(?:act as|role|specialist|expert|persona|reviewer|advisor|mentor|perspective)\b",
    re.IGNORECASE,
)
_EXTERNAL = re.compile(
    r"\b(?:hosted service|saas|daemon|long[- ]running|server process|message broker|search cluster|"
    r"vector database service|cloud|kubernetes cluster)\b",
    re.IGNORECASE,
)
_PRIVILEGED = frozenset(
    {"privileged", "root", "kernel", "hardware", "gpu_passthrough", "host_network"}
)


def tokens(text: str) -> set[str]:
    return {
        t
        for t in re.findall(r"[a-z][a-z0-9]{2,}", text.lower().replace("_", " "))
        if t not in _STOP
    }


class GapResolver:
    def __init__(
        self, store: RegistryStore, router: CapabilityRouter, policy: PolicyEngine
    ) -> None:
        self._store = store
        self._router = router
        self._policy = policy

    async def resolve(self, request: GapRequest, ctx: RunContext | None = None) -> GapResolution:
        resolution = await self._resolve(request, ctx)
        if ctx is not None:
            resolved = resolution.status in {GapStatus.RESOLVED, GapStatus.COMPOSABLE}
            await ctx.emit(
                EventType.CAPABILITY_RESOLVED if resolved else EventType.CAPABILITY_GAP_DETECTED,
                f"{resolution.status.value}: {resolution.capability or request.description[:60]}",
                status=EventStatus.COMPLETED if resolved else EventStatus.FAILED,
                data=resolution.model_dump(mode="json", exclude={"steps"})
                | {"steps": [s.stage + ":" + s.outcome for s in resolution.steps]},
            )
        return resolution

    async def _resolve(self, req: GapRequest, ctx: RunContext | None) -> GapResolution:
        steps: list[ResolutionStep] = []
        text = f"{req.capability or ''} {req.description}".strip()

        # 0. policy first
        blocked = (
            (req.capability is not None and is_forbidden_capability(req.capability))
            or any(is_forbidden_capability(c) for c in req.components)
            or detects_external_database_intent(text)
        )
        decision = await self._policy.evaluate(
            PolicyRequest(
                actor_type=ActorType.LLM,
                actor_id="gap_resolver",
                action="workshop.generate",
                capability=req.capability,
                target=req.description[:200],
                run_id=ctx.run_id if ctx else None,
                attributes={"blocked_intent": blocked},
            ),
            ctx,
        )
        if blocked or decision.effect is PolicyEffect.DENY:
            steps.append(
                ResolutionStep(
                    stage="policy", outcome="blocked", detail="; ".join(decision.reasons)
                )
            )
            return GapResolution(
                status=GapStatus.BLOCKED_BY_POLICY,
                capability=req.capability,
                steps=steps,
                message=(
                    "Root Policy forbids this. External database access is never provided; "
                    "ask for SQL as text (analyze_sql / generate SQL for a human to run)."
                ),
            )
        steps.append(ResolutionStep(stage="policy", outcome="allowed"))
        if not text and not req.components:
            steps.append(ResolutionStep(stage="understand", outcome="empty"))
            return GapResolution(
                status=GapStatus.IMPOSSIBLE,
                steps=steps,
                message="Nothing to resolve: give a capability name or a description of the need.",
            )

        # 1. existing capability with a routable provider
        if req.capability:
            routed = await self._router.resolve(RoutingRequest(capability=req.capability))
            if routed.status is RoutingStatus.SELECTED and routed.selected is not None:
                steps.append(ResolutionStep(stage="existing", outcome="found"))
                p = routed.selected.provider
                return GapResolution(
                    status=GapStatus.RESOLVED,
                    capability=req.capability,
                    steps=steps,
                    provider={"id": p.id, "version": p.version, "state": p.state.value},
                    message="A registered provider already handles this.",
                )
            steps.append(ResolutionStep(stage="existing", outcome="none", detail=routed.message))
        if req.components and (plan := await self._compose(req, text)):
            steps.append(ResolutionStep(stage="compose", outcome="plan", detail=" -> ".join(plan)))
            return GapResolution(
                status=GapStatus.COMPOSABLE,
                capability=req.capability,
                steps=steps,
                plan=plan,
                message="Existing capabilities can be combined; consider a skill to sequence them",
                artifact_kind=ProposalKind.SKILL,
            )
        matched = await self._best_semantic_match(text)
        if matched is not None:
            cap_id, provider = matched
            steps.append(ResolutionStep(stage="existing", outcome="semantic_match", detail=cap_id))
            return GapResolution(
                status=GapStatus.RESOLVED,
                capability=cap_id,
                steps=steps,
                provider=provider,
                message=f"An existing capability ('{cap_id}') already covers this need.",
            )

        # 2. compose from existing capabilities
        plan = await self._compose(req, text)
        if plan:
            steps.append(ResolutionStep(stage="compose", outcome="plan", detail=" -> ".join(plan)))
            return GapResolution(
                status=GapStatus.COMPOSABLE,
                capability=req.capability,
                steps=steps,
                plan=plan,
                message="Existing capabilities can be combined; consider a skill to sequence them",
                artifact_kind=ProposalKind.SKILL,
            )
        steps.append(ResolutionStep(stage="compose", outcome="none"))

        # 3-6. generate, or escalate
        if set(req.requires) & _PRIVILEGED:
            steps.append(ResolutionStep(stage="feasibility", outcome="impossible"))
            return GapResolution(
                status=GapStatus.IMPOSSIBLE,
                capability=req.capability,
                steps=steps,
                message="Needs privileged host access, which the sandbox contract can never grant.",
            )
        kind = req.kind_hint
        if kind == "auto":
            kind = (
                "external_service"
                if _EXTERNAL.search(text)
                else "executable"
                if _EXECUTABLE.search(text)
                else "reasoning_role"
                if _ROLE.search(text)
                else "procedure"
            )
        if kind == "external_service":
            steps.append(ResolutionStep(stage="external", outcome="proposal"))
            return GapResolution(
                status=GapStatus.EXTERNAL_REQUIRED,
                capability=req.capability,
                steps=steps,
                needs_human=True,
                message="This needs a running service outside the platform; a human must decide "
                "whether to onboard one (it would be registered as a subsystem, never generated).",
            )
        artifact = {
            "procedure": ProposalKind.SKILL,
            "reasoning_role": ProposalKind.AGENT,
            "executable": ProposalKind.TOOL,
        }[kind]
        steps.append(ResolutionStep(stage="generate", outcome=artifact.value))
        return GapResolution(
            status=GapStatus.GENERATABLE,
            capability=req.capability,
            steps=steps,
            artifact_kind=artifact,
            needs_human=artifact is ProposalKind.TOOL,
            message={
                ProposalKind.SKILL: "Propose a skill (a reusable procedure); tested automatically.",
                ProposalKind.AGENT: "Propose an agent definition (EXPERIMENTAL, read-only).",
                ProposalKind.TOOL: "Propose a tool in the Workshop: tests, a sandbox run and "
                "human approval before it can be routed.",
            }[artifact],
        )

    async def _best_semantic_match(self, text: str) -> tuple[str, dict[str, str]] | None:
        need = tokens(text)
        if len(need) < 2:
            return None
        scored: list[tuple[float, str, bool]] = []
        for cap in await self._store.list_capabilities():
            cap_tokens = tokens(cap["id"]) | tokens(cap["description"])
            if not cap_tokens:
                continue
            id_tokens = tokens(cap["id"])
            strong = len(id_tokens) >= 2 and id_tokens <= need  # the need names its own words
            overlap = 1.0 if strong else len(need & cap_tokens) / min(len(need), len(cap_tokens))
            if overlap >= 0.6:
                scored.append((overlap, cap["id"], strong))
        if not scored:
            return None
        if sum(1 for _, _, strong in scored if strong) > 1:
            return None  # the need names several capabilities: that is a composition
        scored.sort(reverse=True)
        routed = await self._router.resolve(RoutingRequest(capability=scored[0][1]))
        if routed.status is RoutingStatus.SELECTED and routed.selected is not None:
            p = routed.selected.provider
            return scored[0][1], {"id": p.id, "version": p.version, "state": p.state.value}
        return None

    async def _compose(self, req: GapRequest, text: str) -> list[str]:
        async def routable(cap: str) -> bool:
            r = await self._router.resolve(RoutingRequest(capability=cap))
            return r.status is RoutingStatus.SELECTED

        if req.components:
            ok = [c for c in req.components if await routable(c)]
            return list(req.components) if len(ok) == len(req.components) else []
        need = tokens(text)
        if len(need) < 3:
            return []
        scored: list[tuple[float, str]] = []
        for cap in await self._store.list_capabilities():
            ct = tokens(cap["id"]) | tokens(cap["description"])
            hit = need & ct
            if len(hit) >= 2 and await routable(cap["id"]):
                scored.append((len(hit) / len(need), cap["id"]))
        scored.sort(reverse=True)
        chosen: list[str] = []
        covered: set[str] = set()
        for _, cap_id in scored[:4]:
            cap_tokens = need & (
                tokens(cap_id)
                | tokens((await self._store.get_capability(cap_id) or {}).get("description", ""))
            )
            if cap_tokens - covered:
                chosen.append(cap_id)
                covered |= cap_tokens
        return chosen if len(chosen) >= 2 and len(covered) / len(need) >= 0.6 else []
