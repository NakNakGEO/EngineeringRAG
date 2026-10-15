"""Capability Workshop: DRAFT -> SANDBOXED -> TESTED -> EXPERIMENTAL -> VERIFIED -> TRUSTED.

Rules (master plan Phase 10, Root Rules):

* generated **skills** are tested automatically and become EXPERIMENTAL without a human;
* generated **agents** start EXPERIMENTAL, inherit Root Policy, and are never writers by default;
* generated **tools** run only in the sandbox, need tests, and need a human approval before they
  are ever routable; VERIFIED needs passing tests *and* a human; TRUSTED is human-only;
* Root Policy cannot be generated or modified; external-database capabilities are never generated;
* everything stores version, creator, prompt hash, tests, results, capability claims and approval.
"""

from __future__ import annotations

import hashlib
import json
import stat
import sys
import uuid
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from eios_capability import (
    AgentManifest,
    CapabilityManifest,
    LoadResult,
    RegistryService,
    SkillManifest,
    ToolManifest,
    manifest_hash,
)
from eios_capability.manifests import ManifestError, Permissions, parse_document
from eios_domain.errors import DomainError, NotFoundError
from eios_domain.events import ActorType, EventStatus, EventType
from eios_domain.knowledge import HumanApproval
from eios_domain.policy import PolicyEffect, PolicyRequest
from eios_domain.registry import (
    Origin,
    RegistryState,
    detects_external_database_intent,
    is_forbidden_capability,
)
from eios_governance.evaluation import EvalCase, EvalSuite, EvaluationService, check_expectation
from eios_observability.recorder import RunContext
from eios_policy import PolicyEngine, Sandbox, SandboxSpec, SandboxViolationError
from eios_workshop.models import Proposal, ProposalKind, ProposalState
from eios_workshop.scan import scan_python
from eios_workshop.store import ProposalConflictError, WorkshopStore

MIN_TESTS_FOR_VERIFIED = 3
ROOT_FORBIDDEN_ACTIONS = (
    "bypass or modify Root Policy",
    "connect to, read, write or execute against any external database",
    "push, merge, rebase or release without explicit human approval",
    "register or trust generated executable tools",
)
PLACEHOLDER_ENTRYPOINT = "subprocess:/workshop/pending/tool"
PLACEHOLDER_SHA = "0" * 64


class WorkshopError(Exception):
    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind


class Workshop:
    def __init__(
        self,
        *,
        store: WorkshopStore,
        registry: RegistryService,
        policy: PolicyEngine,
        sandbox: Sandbox,
        evaluation: EvaluationService,
        directory: Path,
    ) -> None:
        self._store = store
        self._registry = registry
        self._policy = policy
        self._sandbox = sandbox
        self._evaluation = evaluation
        self._dir = directory

    # -- create --
    async def create(
        self,
        *,
        kind: ProposalKind,
        spec: dict[str, Any],
        creator: str,
        creator_kind: str = "llm",
        source: str | None = None,
        tests: list[dict[str, Any]] | None = None,
        need: dict[str, Any] | None = None,
        new_capabilities: list[dict[str, Any]] | None = None,
        prompt: str | None = None,
        ctx: RunContext | None = None,
    ) -> Proposal:
        spec = dict(spec)
        spec["kind"] = kind.value
        name, version = str(spec.get("id", "")), str(spec.get("version", ""))
        claims = self._claims(kind, spec)
        text = f"{name} {spec.get('description', spec.get('goal', ''))} {json.dumps(need or {})}"
        blocked = any(is_forbidden_capability(c) for c in [name, *claims]) or (
            detects_external_database_intent(text)
        )
        verdict = await self._policy.evaluate(
            PolicyRequest(
                actor_type=ActorType.LLM if creator_kind == "llm" else ActorType.SYSTEM,
                actor_id=creator,
                action="workshop.generate",
                capability=name,
                run_id=ctx.run_id if ctx else None,
                attributes={"blocked_intent": blocked},
            ),
            ctx,
        )
        if blocked or verdict.effect is PolicyEffect.DENY:
            raise WorkshopError(
                "blocked_by_policy", "; ".join(verdict.reasons) or "forbidden by Root Policy"
            )
        self._validate_spec(kind, spec, source)
        extra_caps = []
        for raw in new_capabilities or []:
            try:
                cap = parse_document({**raw, "kind": "capability"}, "new_capability")
            except ManifestError as exc:
                raise WorkshopError("invalid_spec", str(exc)) from exc
            assert isinstance(cap, CapabilityManifest)  # noqa: S101
            extra_caps.append(cap.model_dump(mode="json"))
        spec_hash = manifest_hash_of(spec, source, extra_caps)
        try:
            pid = await self._store.create(
                {
                    "kind": kind.value,
                    "name": name,
                    "version": version,
                    "spec": spec,
                    "spec_hash": spec_hash,
                    "source": source,
                    "creator": creator[:200],
                    "creator_kind": creator_kind,
                    "prompt_hash": hashlib.sha256(prompt.encode()).hexdigest() if prompt else None,
                    "need": {**(need or {}), "new_capabilities": extra_caps},
                    "capability_claims": claims,
                    "tests": tests or [],
                },
                actor=creator,
            )
        except ProposalConflictError as exc:
            raise WorkshopError("conflict", str(exc)) from exc
        return await self._announce(pid, ctx, "created")

    @staticmethod
    def _claims(kind: ProposalKind, spec: dict[str, Any]) -> list[str]:
        if kind is ProposalKind.SKILL:
            return [str(c) for c in spec.get("required_capabilities", [])]
        return [str(c) for c in spec.get("capabilities", [])]

    def _validate_spec(self, kind: ProposalKind, spec: dict[str, Any], source: str | None) -> None:
        try:
            if kind is ProposalKind.TOOL:
                if not source or not source.strip():
                    raise WorkshopError("invalid_spec", "a tool proposal needs source code")
                draft = {
                    **spec,
                    "type": "subprocess",
                    "entrypoint": PLACEHOLDER_ENTRYPOINT,
                    "sha256": PLACEHOLDER_SHA,
                    "state": "EXPERIMENTAL",
                }
                tool = ToolManifest.model_validate(draft)
                if tool.permissions != Permissions() or tool.network_required:
                    raise WorkshopError(
                        "invalid_spec",
                        "generated tools run with no network, secrets or filesystem access",
                    )
            elif kind is ProposalKind.AGENT:
                agent = AgentManifest.model_validate({**spec, "state": "EXPERIMENTAL"})
                if agent.can_write:
                    raise WorkshopError("invalid_spec", "generated agents cannot be writers")
            else:
                SkillManifest.model_validate({**spec, "state": "EXPERIMENTAL"})
        except ValidationError as exc:
            detail = "; ".join(
                f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors()[:5]
            )
            raise WorkshopError("invalid_spec", detail) from exc

    # -- sandbox --
    async def sandbox(
        self, pid: uuid.UUID, *, actor: str, ctx: RunContext | None = None
    ) -> Proposal:
        p = await self._get(pid, ProposalState.DRAFT)
        fields: dict[str, Any] = {}
        reason = "nothing executable: structure validated"
        if p.kind is ProposalKind.TOOL:
            assert p.source is not None  # noqa: S101
            report = scan_python(p.source)
            fields["scan_report"] = {"ok": report.ok, "findings": report.findings}
            if not report.ok:
                await self._store.annotate(pid, fields)
                raise WorkshopError(
                    "scan_failed",
                    "static scan rejected the source: " + "; ".join(report.findings[:5]),
                )
            path = self._write_launcher(p)
            fields["artifact_path"] = str(path)
            fields["source_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
            reason = "source scanned and launcher written (hash pinned)"
        await self._advance(
            pid, ProposalState.DRAFT, ProposalState.SANDBOXED, actor, reason, fields
        )
        return await self._announce(pid, ctx, "sandboxed")

    def _write_launcher(self, p: Proposal) -> Path:
        assert p.source is not None  # noqa: S101
        directory = self._dir / str(p.id)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "tool"
        path.write_text(f"#!{sys.executable} -IS\n{p.source}\n", encoding="utf-8")
        path.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR | stat.S_IRGRP | stat.S_IXGRP)
        return path

    # -- test --
    async def test(self, pid: uuid.UUID, *, actor: str, ctx: RunContext | None = None) -> Proposal:
        p = await self._get(pid, ProposalState.SANDBOXED)
        results: list[dict[str, Any]] = []
        if p.kind is ProposalKind.TOOL:
            if not p.tests:
                raise WorkshopError("no_tests", "a tool needs at least one test case")
            results = await self._run_tool_tests(p)
        elif p.kind is ProposalKind.SKILL:
            results = await self._check_skill(p)
        else:
            results = await self._check_agent(p)
        passed = all(r["passed"] for r in results) and bool(results)
        summary = {"passed": passed, "results": results}
        if not passed:
            await self._store.annotate(pid, {"test_results": summary})
            return await self._announce(pid, ctx, "tests_failed")
        await self._advance(
            pid,
            ProposalState.SANDBOXED,
            ProposalState.TESTED,
            actor,
            f"{len(results)} checks passed",
            {"test_results": summary},
        )
        return await self._announce(pid, ctx, "tested")

    async def _run_tool_tests(self, p: Proposal) -> list[dict[str, Any]]:
        assert p.artifact_path is not None  # noqa: S101
        assert p.source_sha256 is not None  # noqa: S101
        results = []
        for i, raw in enumerate(p.tests):
            case = EvalCase.model_validate(
                {"id": raw.get("id", f"case{i + 1}"), **{k: v for k, v in raw.items() if k != "id"}}
            )
            try:
                out = await self._sandbox.run(
                    SandboxSpec(
                        argv=[p.artifact_path],
                        sha256=p.source_sha256,
                        stdin=json.dumps(case.arguments).encode(),
                        timeout_seconds=10,
                    )
                )
                if out.timed_out or out.exit_code != 0:
                    detail = (
                        "timed out"
                        if out.timed_out
                        else f"exit {out.exit_code}: {out.stderr[:200].decode(errors='replace')}"
                    )
                    results.append({"id": case.id, "passed": False, "detail": detail})
                    continue
                parsed = json.loads(out.stdout.decode(errors="replace") or "null")
                data = parsed if isinstance(parsed, dict) else {"result": parsed}
                ok, detail = check_expectation(data, case.expect)
                results.append({"id": case.id, "passed": ok, "detail": detail})
            except (SandboxViolationError, json.JSONDecodeError) as exc:
                results.append(
                    {
                        "id": case.id,
                        "passed": False,
                        "detail": f"{type(exc).__name__}: {str(exc)[:200]}",
                    }
                )
        return results

    async def _check_skill(self, p: Proposal) -> list[dict[str, Any]]:
        skill = SkillManifest.model_validate({**p.spec, "state": "EXPERIMENTAL"})
        new = {c["id"] for c in p.need.get("new_capabilities", [])}
        known = {c["id"] for c in await self._registry.store.list_capabilities()} | new
        results = [{"id": "manifest", "passed": True, "detail": ""}]
        for cap in skill.required_capabilities:
            results.append(
                {
                    "id": f"capability:{cap}",
                    "passed": cap in known,
                    "detail": "" if cap in known else "capability is not registered",
                }
            )
        return results

    async def _check_agent(self, p: Proposal) -> list[dict[str, Any]]:
        agent = AgentManifest.model_validate({**p.spec, "state": "EXPERIMENTAL"})
        new = {c["id"] for c in p.need.get("new_capabilities", [])}
        known = {c["id"] for c in await self._registry.store.list_capabilities()} | new
        results = [
            {
                "id": "read_only",
                "passed": not agent.can_write,
                "detail": "generated agents cannot write",
            },
        ]
        for cap in agent.capabilities:
            results.append(
                {
                    "id": f"capability:{cap}",
                    "passed": cap in known,
                    "detail": "" if cap in known else "capability is not registered",
                }
            )
        return results

    # -- register --
    async def register(
        self,
        pid: uuid.UUID,
        *,
        actor: str,
        human: HumanApproval | None = None,
        ctx: RunContext | None = None,
    ) -> Proposal:
        p = await self._get(pid, ProposalState.TESTED)
        if p.kind is ProposalKind.TOOL and human is None:
            raise WorkshopError(
                "approval_required", "registering a generated tool needs a human approval"
            )
        load = LoadResult()
        for raw in p.need.get("new_capabilities", []):
            cap = parse_document(raw, "workshop")
            load.manifests.append(("workshop:capability", cap))
        manifest = self._manifest(p)
        load.manifests.append((f"workshop:{p.kind.value}", manifest))
        report = await self._registry.sync(load, origin=Origin.GENERATED)
        if report.rejected:
            raise WorkshopError(
                "registration_rejected", "; ".join(f"{i}: {r}" for i, r in report.rejected)
            )
        item_kind = {"tool": "provider", "agent": "agent", "skill": "skill"}[p.kind.value]
        if human is not None:
            await self._registry.transition(
                item_kind,
                p.name,
                p.version,
                RegistryState.EXPERIMENTAL,
                actor=human.approver,
                reason=f"approved from workshop proposal {p.id}",
                human_approved=True,
            )
        if p.kind is ProposalKind.TOOL:
            await self._registry.store.set_provider_health(
                p.name, p.version, "ok", "workshop sandbox tested"
            )
        await self._advance(
            pid,
            ProposalState.TESTED,
            ProposalState.EXPERIMENTAL,
            human.approver if human else actor,
            "registered as a generated artifact",
            {
                "registered_ref": f"{p.name}@{p.version}",
                "approval": human.model_dump(mode="json") if human else None,
            },
        )
        return await self._announce(pid, ctx, "registered")

    def _manifest(self, p: Proposal) -> ToolManifest | AgentManifest | SkillManifest:
        spec = {**p.spec}
        if p.kind is ProposalKind.TOOL:
            spec.update(
                type="subprocess",
                entrypoint=f"subprocess:{p.artifact_path}",
                sha256=p.source_sha256,
                state="EXPERIMENTAL",
            )
            return ToolManifest.model_validate(spec)
        if p.kind is ProposalKind.AGENT:
            forbidden = list(
                dict.fromkeys([*spec.get("forbidden_actions", []), *ROOT_FORBIDDEN_ACTIONS])
            )
            return AgentManifest.model_validate(
                {**spec, "forbidden_actions": forbidden, "state": "EXPERIMENTAL"}
            )
        return SkillManifest.model_validate({**spec, "state": "EXPERIMENTAL"})

    # -- verify / trust / reject --
    async def verify(
        self, pid: uuid.UUID, *, human: HumanApproval, ctx: RunContext | None = None
    ) -> Proposal:
        p = await self._get(pid, ProposalState.EXPERIMENTAL)
        if p.kind is ProposalKind.TOOL:
            if len(p.tests) < MIN_TESTS_FOR_VERIFIED:
                raise WorkshopError(
                    "not_enough_tests",
                    f"VERIFIED needs at least {MIN_TESTS_FOR_VERIFIED} test cases",
                )
            cases = [
                EvalCase.model_validate(
                    {"id": t.get("id", f"case{i + 1}"), **{k: v for k, v in t.items() if k != "id"}}
                )
                for i, t in enumerate(p.tests)
            ]
            capability = p.capability_claims[0]
            report = await self._evaluation.run_suite(
                EvalSuite(provider=p.name, capability=capability, cases=cases),
                actor=human.approver,
                ctx=ctx,
            )
            if report.score < 1.0:
                raise WorkshopError(
                    "verification_failed", f"{report.passed}/{report.samples} cases passed"
                )
        item_kind = {"tool": "provider", "agent": "agent", "skill": "skill"}[p.kind.value]
        await self._registry.transition(
            item_kind,
            p.name,
            p.version,
            RegistryState.VERIFIED,
            actor=human.approver,
            reason="workshop verification (tests passed, human approved)",
            human_approved=True,
            verification_passed=True,
        )
        await self._advance(
            pid,
            ProposalState.EXPERIMENTAL,
            ProposalState.VERIFIED,
            human.approver,
            "verified by a human after passing tests",
            {"approval": human.model_dump(mode="json")},
        )
        return await self._announce(pid, ctx, "verified")

    async def trust(
        self, pid: uuid.UUID, *, human: HumanApproval, ctx: RunContext | None = None
    ) -> Proposal:
        p = await self._get(pid, ProposalState.VERIFIED)
        item_kind = {"tool": "provider", "agent": "agent", "skill": "skill"}[p.kind.value]
        await self._registry.transition(
            item_kind,
            p.name,
            p.version,
            RegistryState.TRUSTED,
            actor=human.approver,
            reason="workshop: promoted to TRUSTED by a human",
            human_approved=True,
        )
        await self._advance(
            pid,
            ProposalState.VERIFIED,
            ProposalState.TRUSTED,
            human.approver,
            "promoted to TRUSTED",
            {"approval": human.model_dump(mode="json")},
        )
        return await self._announce(pid, ctx, "trusted")

    async def reject(
        self, pid: uuid.UUID, *, actor: str, reason: str, ctx: RunContext | None = None
    ) -> Proposal:
        p = await self._store.get(pid)
        if p is None:
            raise NotFoundError(f"proposal {pid} not found")
        if p.state in {ProposalState.REJECTED}:
            raise WorkshopError("conflict", "proposal is already rejected")
        if not reason.strip():
            raise WorkshopError("invalid_request", "rejecting needs a reason")
        if p.registered_ref:
            item_kind = {"tool": "provider", "agent": "agent", "skill": "skill"}[p.kind.value]
            await self._registry.transition(
                item_kind,
                p.name,
                p.version,
                RegistryState.DISABLED,
                actor=actor,
                reason=f"proposal rejected: {reason}",
            )
        await self._advance(pid, p.state, ProposalState.REJECTED, actor, reason, {})
        return await self._announce(pid, ctx, "rejected")

    # -- queries / helpers --
    async def get(self, pid: uuid.UUID) -> Proposal:
        p = await self._store.get(pid)
        if p is None:
            raise NotFoundError(f"proposal {pid} not found")
        return p

    async def list_proposals(
        self, state: ProposalState | None = None, limit: int = 50
    ) -> list[Proposal]:
        return await self._store.list_proposals(state=state, limit=limit)

    async def history(self, pid: uuid.UUID) -> list[dict[str, Any]]:
        return await self._store.history(pid)

    async def _get(self, pid: uuid.UUID, expected: ProposalState) -> Proposal:
        p = await self.get(pid)
        if p.state is not expected:
            raise WorkshopError(
                "invalid_state", f"proposal is {p.state.value}; this step needs {expected.value}"
            )
        return p

    async def _advance(
        self,
        pid: uuid.UUID,
        old: ProposalState,
        new: ProposalState,
        actor: str,
        reason: str,
        fields: dict[str, Any],
    ) -> None:
        try:
            await self._store.advance(
                pid, expected=old, to=new, actor=actor, reason=reason, fields=fields
            )
        except ProposalConflictError as exc:
            raise WorkshopError("conflict", str(exc)) from exc

    async def _announce(self, pid: uuid.UUID, ctx: RunContext | None, what: str) -> Proposal:
        p = await self.get(pid)
        if ctx is not None:
            await ctx.emit(
                EventType.WORKSHOP_PROPOSAL_UPDATED,
                f"{p.kind.value} '{p.name}': {what}",
                status=EventStatus.FAILED if what == "tests_failed" else EventStatus.COMPLETED,
                data={
                    "proposal_id": str(p.id),
                    "kind": p.kind.value,
                    "name": p.name,
                    "version": p.version,
                    "state": p.state.value,
                    "what": what,
                },
            )
        return p


def manifest_hash_of(spec: dict[str, Any], source: str | None, caps: list[dict[str, Any]]) -> str:
    canonical = json.dumps(
        {"spec": spec, "source": source, "caps": caps}, sort_keys=True, default=str
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


__all__ = ["DomainError", "Workshop", "WorkshopError", "manifest_hash"]
