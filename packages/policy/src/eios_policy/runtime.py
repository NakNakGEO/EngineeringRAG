"""Tool runtime: route -> policy -> (approval) -> execute -> evidence/metrics, all observable.

This is the only execution path for capabilities. Nothing runs unless it is a registered,
routable provider, the Policy Engine allows it, and (for subprocess providers) the sandbox
contract holds. There is no code path that executes a string supplied by an LLM.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Literal

from eios_capability import (
    AdapterCatalog,
    CapabilityRouter,
    RegistryStore,
    RoutingDecision,
    RoutingRequest,
    RoutingStatus,
    ToolManifest,
)
from eios_domain.errors import BudgetExceededError
from eios_domain.events import ActorType, EventStatus, EventType
from eios_domain.policy import PolicyEffect, PolicyRequest, Risk
from eios_observability.recorder import RunContext
from eios_policy.engine import PolicyEngine
from eios_policy.sandbox import Sandbox, SandboxSpec, SandboxViolationError

Status = Literal["ok", "failed", "denied", "approval_required", "no_provider"]
_RISKS = {r.value for r in Risk}


@dataclass(frozen=True)
class ToolInvocation:
    capability: str
    arguments: dict[str, Any] = field(default_factory=dict)
    actor_type: ActorType = ActorType.LLM
    actor_id: str = "llm"
    routing: RoutingRequest | None = None
    actor_can_write: bool = False


@dataclass
class ToolResult:
    status: Status
    capability: str
    provider: str | None = None
    version: str | None = None
    output: dict[str, Any] | None = None
    error: str | None = None
    approval_id: uuid.UUID | None = None
    reasons: list[str] = field(default_factory=list)
    duration_ms: int = 0
    routing: dict[str, Any] | None = None

    def summary(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "capability": self.capability,
            "provider": self.provider,
            "version": self.version,
            "output": self.output,
            "error": self.error,
            "approval_id": str(self.approval_id) if self.approval_id else None,
            "reasons": self.reasons,
            "duration_ms": self.duration_ms,
        }


class ToolRuntime:
    def __init__(
        self,
        *,
        router: CapabilityRouter,
        store: RegistryStore,
        adapters: AdapterCatalog,
        policy: PolicyEngine,
        sandbox: Sandbox | None = None,
        call_timeout_seconds: float = 120.0,
        max_output_bytes: int = 5_000_000,
    ) -> None:
        self._router = router
        self._store = store
        self._adapters = adapters
        self._policy = policy
        self._sandbox = sandbox
        self._timeout = call_timeout_seconds
        self._max_output = max_output_bytes

    async def invoke(self, call: ToolInvocation, ctx: RunContext | None = None) -> ToolResult:
        routing_request = call.routing or RoutingRequest(capability=call.capability)
        decision = await self._router.resolve(routing_request, ctx)
        if decision.status is not RoutingStatus.SELECTED or decision.selected is None:
            if decision.status is RoutingStatus.FORBIDDEN:
                # the audit trail must show the attempt, not just the router's refusal
                await self._policy.evaluate(
                    PolicyRequest(
                        actor_type=call.actor_type, actor_id=call.actor_id,
                        action="capability.invoke", capability=call.capability,
                        run_id=ctx.run_id if ctx else None,
                    ),
                    ctx,
                )  # fmt: skip
            return ToolResult(
                "no_provider" if decision.status is not RoutingStatus.FORBIDDEN else "denied",
                call.capability,
                error=decision.message,
                routing=decision.summary(),
                reasons=[decision.message] if decision.message else [],
            )
        provider = decision.selected.provider
        manifest = ToolManifest.model_validate(provider.manifest)
        cap = await self._store.get_capability(call.capability)
        risk = str(cap["risk"]) if cap and cap["risk"] in _RISKS else "medium"
        writes = any(f.mode == "write" for f in manifest.permissions.filesystem)
        request = PolicyRequest(
            actor_type=call.actor_type,
            actor_id=call.actor_id,
            action="capability.invoke",
            capability=call.capability,
            target=f"{provider.id}@{provider.version}",
            run_id=ctx.run_id if ctx else None,
            attributes={
                "provider_state": provider.state.value,
                "provider_origin": provider.origin.value,
                "provider_approved": provider.approved_by is not None,
                "risk": risk,
                "writes": writes,
                "actor_can_write": call.actor_can_write,
            },
        )
        verdict = await self._policy.evaluate(request, ctx)
        result = ToolResult(
            "ok", call.capability, provider.id, provider.version, routing=decision.summary()
        )
        if verdict.effect is PolicyEffect.DENY:
            result.status, result.reasons, result.error = (
                "denied",
                verdict.reasons,
                "; ".join(verdict.reasons),
            )
            return result
        if verdict.effect is PolicyEffect.REQUIRE_APPROVAL:
            result.status, result.reasons = "approval_required", verdict.reasons
            result.approval_id = verdict.approval_id
            result.error = "human approval required"
            return result
        return await self._execute(
            call, provider.id, provider.version, manifest, decision, result, ctx
        )

    async def _execute(
        self,
        call: ToolInvocation,
        provider_id: str,
        version: str,
        manifest: ToolManifest,
        decision: RoutingDecision,
        result: ToolResult,
        ctx: RunContext | None,
    ) -> ToolResult:
        started = time.monotonic()
        try:
            if ctx is not None:
                await ctx.charge("tool_calls")
                await ctx.emit(
                    EventType.TOOL_STARTED, f"{provider_id} started",
                    status=EventStatus.STARTED,
                    data={"provider": provider_id, "version": version,
                          "capability": call.capability},
                    actor_type=ActorType.TOOL, actor_id=provider_id,
                )  # fmt: skip
            output = await asyncio.wait_for(self._run(call, manifest, ctx), self._timeout)
            encoded = json.dumps(output, default=str)
            if len(encoded.encode()) > self._max_output:
                raise ValueError(f"tool output exceeds {self._max_output} bytes")
            result.output = output
        except BudgetExceededError:
            raise
        except (SandboxViolationError, PermissionError) as exc:
            result.status, result.error = "denied", str(exc)
        except Exception as exc:
            result.status, result.error = "failed", f"{type(exc).__name__}: {exc}"[:500]
        result.duration_ms = int((time.monotonic() - started) * 1000)
        ok = result.status == "ok"
        await self._store.record_outcome(
            provider_id, version, call.capability, success=ok, latency_ms=result.duration_ms
        )
        if ctx is not None:
            await ctx.emit(
                EventType.TOOL_COMPLETED,
                f"{provider_id} {'completed' if ok else result.status}",
                status=EventStatus.COMPLETED if ok else EventStatus.FAILED,
                data={"provider": provider_id, "capability": call.capability,
                      "duration_ms": result.duration_ms, "error": result.error},
                actor_type=ActorType.TOOL, actor_id=provider_id,
            )  # fmt: skip
        return result

    async def execute_provider(
        self, manifest: ToolManifest, arguments: dict[str, Any], *, actor_id: str = "evaluation"
    ) -> dict[str, Any]:
        """Run a provider directly (no routing) through the same policy and sandbox path."""
        call = ToolInvocation(
            capability="", arguments=arguments, actor_type=ActorType.SYSTEM, actor_id=actor_id
        )
        return await asyncio.wait_for(self._run(call, manifest, None), self._timeout)

    async def _run(
        self, call: ToolInvocation, manifest: ToolManifest, ctx: RunContext | None
    ) -> dict[str, Any]:
        scheme, _, name = manifest.entrypoint.partition(":")
        if scheme in {"builtin", "adapter"}:
            return await self._adapters.invoke(manifest.entrypoint, call.arguments)
        if scheme == "subprocess":
            return await self._run_subprocess(call, manifest, name, ctx)
        raise SandboxViolationError(f"entrypoint scheme '{scheme}' is not executable here")

    async def _run_subprocess(
        self, call: ToolInvocation, manifest: ToolManifest, path: str, ctx: RunContext | None
    ) -> dict[str, Any]:
        if self._sandbox is None or manifest.sha256 is None:
            raise SandboxViolationError("subprocess tools need the sandbox and a pinned hash")
        verdict = await self._policy.evaluate(
            PolicyRequest(
                actor_type=call.actor_type, actor_id=call.actor_id, action="process.exec",
                capability=call.capability, target=path, run_id=ctx.run_id if ctx else None,
                attributes={"allowed_processes": [path], "registered": True, "argv": [path]},
            ),
            ctx,
        )  # fmt: skip
        if verdict.effect is not PolicyEffect.ALLOW:
            raise SandboxViolationError("; ".join(verdict.reasons))
        sandboxed = await self._sandbox.run(
            SandboxSpec(
                argv=[path],
                sha256=manifest.sha256,
                stdin=json.dumps(call.arguments).encode(),
            )
        )
        if sandboxed.timed_out:
            raise TimeoutError("tool timed out")
        if sandboxed.exit_code != 0:
            raise RuntimeError(
                f"tool exited with {sandboxed.exit_code}: "
                + sandboxed.stderr[:300].decode(errors="replace")
            )
        text = sandboxed.stdout.decode(errors="replace")
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return {"stdout": text, "truncated": sandboxed.truncated}
        return parsed if isinstance(parsed, dict) else {"result": parsed}
