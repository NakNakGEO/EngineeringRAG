"""Registry service: validated registration, state transitions, compact catalog views.

Rules enforced here (on top of the strict manifest models):

* every tool/agent/skill may only reference capabilities that are registered;
* a manifest can never claim more trust than its origin allows (``clamp_state``);
* versions of non-builtin artifacts are immutable - changed content needs a new version;
* generated/downloaded artifacts, and writer agents from non-builtin origins, are not routable
  until a human approved them;
* re-syncing never resets a state that a human or a health check changed.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from eios_capability.manifests import (
    AgentManifest,
    CapabilityManifest,
    LoadResult,
    SkillManifest,
    ToolManifest,
    manifest_hash,
)
from eios_capability.store import ProviderRow, RegistryStore
from eios_domain.registry import (
    APPROVAL_REQUIRED_ORIGINS,
    Origin,
    RegistryState,
    check_transition,
    clamp_state,
)

_DEMOTED = frozenset({RegistryState.DISABLED, RegistryState.BROKEN, RegistryState.QUARANTINED})


@dataclass
class SyncReport:
    added: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    rejected: list[tuple[str, str]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.rejected

    def summary(self) -> dict[str, Any]:
        return {
            "added": len(self.added),
            "updated": len(self.updated),
            "unchanged": len(self.unchanged),
            "rejected": [{"item": i, "reason": r} for i, r in self.rejected],
        }

    def _note(self, outcome: str, label: str) -> None:
        {"added": self.added, "updated": self.updated, "unchanged": self.unchanged}[outcome].append(
            label
        )


class RegistryService:
    def __init__(self, store: RegistryStore) -> None:
        self.store = store

    # -- registration -----------------------------------------------------------------------
    async def sync(self, loaded: LoadResult, *, origin: Origin = Origin.BUILTIN) -> SyncReport:
        """Register everything in ``loaded``; invalid or disallowed items are rejected."""
        report = SyncReport()
        for err in loaded.errors:
            report.rejected.append((err.source, err.message))
        by_kind: dict[str, list[tuple[str, Any]]] = {
            "capability": [],
            "tool": [],
            "agent": [],
            "skill": [],
        }
        for source, manifest in loaded.manifests:
            by_kind[manifest.kind].append((source, manifest))
        for source, cap in by_kind["capability"]:
            await self._register_capability(source, cap, report)
        known = {c["id"] for c in await self.store.list_capabilities()}
        for source, tool in by_kind["tool"]:
            await self._register_tool(source, tool, origin, known, report)
        for source, agent in by_kind["agent"]:
            await self._register_agent(source, agent, origin, known, report)
        for source, skill in by_kind["skill"]:
            await self._register_skill(source, skill, origin, known, report)
        return report

    async def _register_capability(
        self, source: str, cap: CapabilityManifest, report: SyncReport
    ) -> None:
        outcome = await self.store.upsert_capability(cap, manifest_hash(cap))
        report._note(outcome, f"capability:{cap.id}")

    @staticmethod
    def _unknown(capabilities: list[str], known: set[str]) -> list[str]:
        return sorted(c for c in set(capabilities) if c not in known)

    async def _register_tool(
        self,
        source: str,
        tool: ToolManifest,
        origin: Origin,
        known: set[str],
        report: SyncReport,
    ) -> None:
        label = f"tool:{tool.id}@{tool.version}"
        if missing := self._unknown(tool.capabilities, known):
            report.rejected.append((label, f"unknown capabilities: {', '.join(missing)}"))
            return
        digest = manifest_hash(tool)
        existing = await self.store.get_provider(tool.id, tool.version)
        if existing is not None:
            if existing.manifest_hash == digest:
                report.unchanged.append(label)
                return
            if origin is not Origin.BUILTIN or existing.origin is not Origin.BUILTIN:
                report.rejected.append(
                    (label, "versions are immutable: publish a new version for changed content")
                )
                return
        state = clamp_state(tool.state, origin)
        if existing is not None and existing.state in _DEMOTED:
            state = existing.state  # a re-sync never un-quarantines or un-breaks
        outcome = await self.store.upsert_provider(tool, digest, origin=origin, state=state)
        report._note(outcome, label)

    async def _register_agent(
        self,
        source: str,
        agent: AgentManifest,
        origin: Origin,
        known: set[str],
        report: SyncReport,
    ) -> None:
        label = f"agent:{agent.id}@{agent.version}"
        if missing := self._unknown(agent.capabilities, known):
            report.rejected.append((label, f"unknown capabilities: {', '.join(missing)}"))
            return
        await self._register_definition("agent", label, agent, origin, report)

    async def _register_skill(
        self,
        source: str,
        skill: SkillManifest,
        origin: Origin,
        known: set[str],
        report: SyncReport,
    ) -> None:
        label = f"skill:{skill.id}@{skill.version}"
        if missing := self._unknown(skill.required_capabilities, known):
            report.rejected.append((label, f"unknown capabilities: {', '.join(missing)}"))
            return
        await self._register_definition("skill", label, skill, origin, report)

    async def _register_definition(
        self,
        kind: str,
        label: str,
        manifest: AgentManifest | SkillManifest,
        origin: Origin,
        report: SyncReport,
    ) -> None:
        digest = manifest_hash(manifest)
        getter = self.store.get_agent if kind == "agent" else self.store.get_skill
        existing = await getter(manifest.id, manifest.version)
        if existing is not None:
            if existing["manifest_hash"] == digest:
                report.unchanged.append(label)
                return
            if origin is not Origin.BUILTIN or existing["origin"] != Origin.BUILTIN.value:
                report.rejected.append(
                    (label, "versions are immutable: publish a new version for changed content")
                )
                return
        state = clamp_state(manifest.state, origin)
        if (
            isinstance(manifest, AgentManifest)
            and manifest.can_write
            and origin is not Origin.BUILTIN
        ):
            state = RegistryState.DISABLED  # a writer from outside the release waits for a human
        elif origin in APPROVAL_REQUIRED_ORIGINS:
            state = RegistryState.EXPERIMENTAL  # registered; routability gated on approval
        if existing is not None and RegistryState(existing["state"]) in _DEMOTED:
            state = RegistryState(existing["state"])
        if kind == "agent":
            assert isinstance(manifest, AgentManifest)  # noqa: S101 - narrowing for the type checker
            outcome = await self.store.upsert_agent(manifest, digest, origin=origin, state=state)
        else:
            assert isinstance(manifest, SkillManifest)  # noqa: S101
            outcome = await self.store.upsert_skill(manifest, digest, origin=origin, state=state)
        report._note(outcome, label)

    # -- transitions ------------------------------------------------------------------------
    async def transition(
        self,
        kind: str,
        item_id: str,
        version: str,
        target: RegistryState,
        *,
        actor: str,
        reason: str,
        human_approved: bool = False,
        verification_passed: bool = False,
        approval_id: uuid.UUID | None = None,
    ) -> RegistryState:
        """Move an item to ``target`` if the rules allow it; records history. Returns old state."""
        current_state, origin = await self._state_of(kind, item_id, version)
        check_transition(
            current_state,
            target,
            origin,
            human_approved=human_approved,
            verification_passed=verification_passed,
        )
        return await self.store.set_state(
            kind,
            item_id,
            version,
            target,
            actor=actor,
            reason=reason,
            approved_by=actor if human_approved else None,
            approval_id=approval_id if human_approved else None,
        )

    async def _state_of(
        self, kind: str, item_id: str, version: str
    ) -> tuple[RegistryState, Origin]:
        if kind == "provider":
            row = await self.store.get_provider(item_id, version)
            if row is None:
                raise LookupError(f"provider {item_id}@{version} is not registered")
            return row.state, row.origin
        getter = self.store.get_agent if kind == "agent" else self.store.get_skill
        d = await getter(item_id, version)
        if d is None:
            raise LookupError(f"{kind} {item_id}@{version} is not registered")
        return RegistryState(d["state"]), Origin(d["origin"])

    # -- compact views (what an LLM may see by default) ------------------------------------------
    async def capability_summaries(self) -> list[dict[str, Any]]:
        caps = await self.store.list_capabilities()
        providers = await self.store.list_providers()
        counts: dict[str, int] = {}
        for p in providers:
            if is_provider_routable(p):
                for c in p.capabilities:
                    counts[c] = counts.get(c, 0) + 1
        return [
            {
                "id": c["id"],
                "description": c["description"],
                "risk": c["risk"],
                "providers": counts.get(c["id"], 0),
            }
            for c in caps
        ]

    async def capability_detail(self, capability_id: str) -> dict[str, Any] | None:
        cap = await self.store.get_capability(capability_id)
        if cap is None:
            return None
        providers = await self.store.list_providers(capability_id=capability_id)
        metrics = await self.store.metrics_for(capability_id)
        return {
            "id": cap["id"],
            "description": cap["description"],
            "risk": cap["risk"],
            "inputs": cap["inputs"],
            "outputs": cap["outputs"],
            "allowed_scopes": cap["allowed_scopes"],
            "tags": cap["tags"],
            "providers": [
                {
                    "id": p.id,
                    "version": p.version,
                    "state": p.state.value,
                    "origin": p.origin.value,
                    "routable": is_provider_routable(p),
                    "health": p.health_status,
                    "metrics": (
                        {
                            "samples": m.samples,
                            "success_rate": m.success_rate,
                            "avg_latency_ms": m.avg_latency_ms,
                        }
                        if (m := metrics.get((p.id, p.version)))
                        else None
                    ),
                }
                for p in providers
            ],
        }

    async def provider_summaries(self) -> list[dict[str, Any]]:
        return [
            {
                "id": p.id,
                "version": p.version,
                "type": p.type,
                "state": p.state.value,
                "origin": p.origin.value,
                "capabilities": p.capabilities,
                "health": p.health_status,
                "routable": is_provider_routable(p),
            }
            for p in await self.store.list_providers()
        ]

    async def agent_summaries(self) -> list[dict[str, Any]]:
        return [
            {
                "id": a["id"],
                "version": a["version"],
                "role": a["role"],
                "state": a["state"],
                "can_write": a["can_write"],
                "description": a["manifest"]["description"],
            }
            for a in await self.store.list_agents()
        ]

    async def skill_summaries(self) -> list[dict[str, Any]]:
        return [
            {
                "id": s["id"],
                "version": s["version"],
                "state": s["state"],
                "goal": s["manifest"]["goal"],
                "trigger": s["manifest"]["trigger"],
            }
            for s in await self.store.list_skills()
        ]


def is_provider_routable(provider: ProviderRow) -> bool:
    from eios_domain.registry import is_routable

    return is_routable(provider.state, provider.origin, approved=provider.approved_by is not None)
