"""Policy Engine: every consequential request is evaluated here, default deny.

Rule order is fixed and documented (first match wins):

1. ``root.external_database``  - database actions/clients/ports/capabilities: always DENY.
2. ``root.forbidden_capability`` - capabilities on the Root Policy list: always DENY.
3. action-specific rules (approval-gated git actions, filesystem, network, process, secrets,
   capability invocation).
4. anything not recognised: DENY (``default.deny``).

REQUIRE_APPROVAL resolves to ALLOW only by consuming a matching, human-approved, single-use
approval; the engine can *request* approvals but has no way to grant them.
"""

from __future__ import annotations

import ipaddress
import os
from collections.abc import Iterable
from pathlib import PurePosixPath
from urllib.parse import urlsplit

from eios_core.database_policy import ExternalDatabaseForbiddenError, assert_internal_database_url
from eios_domain.events import ActorType, EventStatus, EventType
from eios_domain.policy import PolicyDecision, PolicyEffect, PolicyRequest, Risk
from eios_domain.registry import RegistryState
from eios_observability.recorder import RunContext
from eios_policy.approvals import ApprovalGate
from eios_policy.audit import AuditSink
from eios_policy.root_policy import RootPolicy

_RISK_ORDER = {Risk.LOW: 0, Risk.MEDIUM: 1, Risk.HIGH: 2, Risk.CRITICAL: 3}
_METADATA_NETS = (ipaddress.ip_network("169.254.0.0/16"), ipaddress.ip_network("fe80::/10"))


def _host_port(target: str) -> tuple[str, int | None]:
    parsed = urlsplit(target if "//" in target else f"//{target}")
    try:
        port = parsed.port
    except ValueError:
        port = None
    return (parsed.hostname or "").lower(), port


def host_allowed(host: str, allowed: Iterable[str]) -> bool:
    for entry in allowed:
        e = entry.lower()
        if e == host or (e.startswith("*.") and host.endswith(e[1:]) and host != e[2:]):
            return True
    return False


class PolicyEngine:
    def __init__(
        self,
        root: RootPolicy,
        *,
        audit: AuditSink,
        approvals: ApprovalGate,
        workspace_roots: Iterable[str | os.PathLike[str]] = (),
        sandbox_output_dir: str | os.PathLike[str] | None = None,
    ) -> None:
        self.root = root
        self._audit = audit
        self._approvals = approvals
        self._roots = [os.path.realpath(r) for r in workspace_roots]
        self._sandbox_out = os.path.realpath(sandbox_output_dir) if sandbox_output_dir else None

    # -- public -----------------------------------------------------------------------------
    async def evaluate(
        self, request: PolicyRequest, ctx: RunContext | None = None, *, dry_run: bool = False
    ) -> PolicyDecision:
        decision = self._decide(request)
        if decision.effect is PolicyEffect.REQUIRE_APPROVAL and not dry_run:
            consumed = await self._approvals.consume_approved(request)
            if consumed is not None:
                decision = self._make(
                    request, PolicyEffect.ALLOW, "approval.consumed",
                    [f"human approval {consumed.id} by {consumed.decided_by} consumed"],
                    decision.risk, approval_id=consumed.id,
                )  # fmt: skip
            else:
                pending = await self._approvals.request_approval(
                    request, "; ".join(decision.reasons)
                )
                decision = decision.model_copy(update={"approval_id": pending.id})
                if ctx is not None:
                    await ctx.emit(
                        EventType.APPROVAL_REQUESTED,
                        f"approval requested for {request.action}",
                        status=EventStatus.STARTED,
                        data={"approval_id": str(pending.id), "action": request.action,
                              "target": request.target, "capability": request.capability},
                    )  # fmt: skip
        if not dry_run:
            await self._audit.record(decision)
            if ctx is not None:
                allowed = decision.effect is PolicyEffect.ALLOW
                await ctx.emit(
                    EventType.POLICY_ALLOWED if allowed else EventType.POLICY_DENIED,
                    f"{decision.effect.value}: {request.action} ({decision.rule_id})",
                    status=EventStatus.COMPLETED if allowed else EventStatus.DENIED,
                    data={"rule_id": decision.rule_id, "reasons": decision.reasons,
                          "action": request.action, "capability": request.capability,
                          "target": request.target, "risk": decision.risk.value,
                          "effect": decision.effect.value},
                    actor_type=ActorType.SYSTEM, actor_id="policy",
                )  # fmt: skip
        return decision

    # -- rules ------------------------------------------------------------------------------
    def _make(
        self,
        request: PolicyRequest,
        effect: PolicyEffect,
        rule: str,
        reasons: list[str],
        risk: Risk = Risk.LOW,
        *,
        approval_id: object | None = None,
    ) -> PolicyDecision:
        return PolicyDecision(
            effect=effect, rule_id=rule, reasons=reasons, request=request, risk=risk,
            root_policy_version=f"{self.root.version}:{self.root.digest[:12]}",
            approval_id=approval_id,  # type: ignore[arg-type]
        )  # fmt: skip

    def _deny(
        self, req: PolicyRequest, rule: str, reason: str, risk: Risk = Risk.HIGH
    ) -> PolicyDecision:
        return self._make(req, PolicyEffect.DENY, rule, [reason], risk)

    def _allow(
        self, req: PolicyRequest, rule: str, reason: str, risk: Risk = Risk.LOW
    ) -> PolicyDecision:
        return self._make(req, PolicyEffect.ALLOW, rule, [reason], risk)

    def _decide(self, req: PolicyRequest) -> PolicyDecision:
        root = self.root
        action = req.action
        # 1. external databases: structural, unconditional
        if action.startswith("db.") or "database" in action:
            return self._decide_database(req)
        if req.capability and root.is_forbidden_capability(req.capability):
            return self._deny(
                req, "root.forbidden_capability",
                f"capability '{req.capability}' is forbidden by Root Policy", Risk.CRITICAL,
            )  # fmt: skip
        if action in root.approval_required_actions:
            return self._make(
                req, PolicyEffect.REQUIRE_APPROVAL, "root.human_approval",
                [f"'{action}' always requires explicit human approval"], Risk.HIGH,
            )  # fmt: skip
        if action == "workflow.gate":
            return self._make(
                req, PolicyEffect.REQUIRE_APPROVAL, "workflow.approval_gate",
                ["this workflow node is gated on a human decision"],
                Risk(str(req.attributes.get("risk", "medium"))),
            )  # fmt: skip
        if action == "llm.call":
            return self._decide_llm(req)
        if action == "capability.invoke":
            return self._decide_invoke(req)
        if action in {"fs.read", "fs.write"}:
            return self._decide_fs(req)
        if action == "net.connect":
            return self._decide_net(req)
        if action == "process.exec":
            return self._decide_process(req)
        if action == "secret.read":
            return self._decide_secret(req)
        return self._deny(req, "default.deny", f"unknown action '{action}' is denied by default")

    def _decide_llm(self, req: PolicyRequest) -> PolicyDecision:
        """Model calls made by the platform. Project-vault content never goes to a remote model
        without a human decision; unconfigured providers are never called."""
        attrs = req.attributes
        if not attrs.get("configured", False):
            return self._deny(req, "llm.unconfigured", "no such LLM provider is configured")
        classification = str(attrs.get("classification", "project"))  # unknown = most sensitive
        remote = str(attrs.get("locality", "remote")) != "local"
        if remote and classification == "project":
            return self._make(
                req, PolicyEffect.REQUIRE_APPROVAL, "llm.project_data_remote",
                ["project/company data would leave this machine for a remote model"], Risk.HIGH,
            )  # fmt: skip
        return self._allow(req, "llm.allowed", "provider configured and data class permitted")

    def _decide_database(self, req: PolicyRequest) -> PolicyDecision:
        if req.action == "db.connect" and req.target:
            try:
                assert_internal_database_url(req.target)
            except ExternalDatabaseForbiddenError as exc:
                return self._deny(req, "root.external_database", str(exc), Risk.CRITICAL)
            if req.actor_type is not ActorType.SYSTEM:
                return self._deny(
                    req, "root.external_database",
                    "only the platform itself may open its own database; tools and agents may not",
                    Risk.CRITICAL,
                )  # fmt: skip
            return self._allow(req, "db.internal", "platform connection to its own database")
        return self._deny(
            req, "root.external_database",
            "database access is not available through the supported runtime", Risk.CRITICAL,
        )  # fmt: skip

    def _decide_invoke(self, req: PolicyRequest) -> PolicyDecision:
        attrs = req.attributes
        state = str(attrs.get("provider_state", ""))
        approved = bool(attrs.get("provider_approved", False))
        origin = str(attrs.get("provider_origin", ""))
        risk = Risk(str(attrs.get("risk", "low")))
        if state not in {
            RegistryState.EXPERIMENTAL.value,
            RegistryState.VERIFIED.value,
            RegistryState.TRUSTED.value,
        }:
            return self._deny(
                req, "capability.state", f"provider state '{state}' is not routable", risk
            )
        if origin in {"generated", "downloaded"} and not approved:
            return self._deny(
                req, "capability.unapproved",
                f"{origin} executables are not runnable before human approval", Risk.HIGH,
            )  # fmt: skip
        threshold = _RISK_ORDER[self.root.experimental_approval_risk]
        if state == RegistryState.EXPERIMENTAL.value and _RISK_ORDER[risk] >= threshold:
            return self._make(
                req, PolicyEffect.REQUIRE_APPROVAL, "capability.experimental_risk",
                [f"EXPERIMENTAL provider for {risk.value}-risk capability needs approval"], risk,
            )  # fmt: skip
        if (
            req.actor_type is ActorType.LLM
            and attrs.get("writes", False)
            and not attrs.get("actor_can_write", False)
        ):
            return self._deny(req, "capability.writer", "actor has no write permission", risk)
        return self._allow(req, "capability.allowed", "registered, routable and permitted", risk)

    def _path_denied(self, resolved: str) -> str | None:
        p = PurePosixPath(resolved)
        lowered = resolved.lower()
        for fragment in self.root.denied_path_fragments:
            if fragment.lower() in lowered + "/":
                return f"path matches denied fragment '{fragment}'"
        if p.name.lower() in {n.lower() for n in self.root.denied_file_names}:
            return f"file name '{p.name}' is denied"
        return None

    def _decide_fs(self, req: PolicyRequest) -> PolicyDecision:
        if not req.target:
            return self._deny(req, "fs.no_target", "filesystem request without a target")
        if "\x00" in req.target:
            return self._deny(req, "fs.invalid", "invalid path")
        resolved = os.path.realpath(req.target)  # follows symlinks: traversal and links resolved
        if (reason := self._path_denied(resolved)) is not None:
            return self._deny(req, "fs.denied_path", reason)
        writing = req.action == "fs.write"
        if self._sandbox_out and _within(resolved, self._sandbox_out):
            return self._allow(req, "fs.sandbox_output", "sandbox output directory")
        for root in self._roots:
            if _within(resolved, root):
                if not writing:
                    return self._allow(req, "fs.read_workspace", "inside an approved workspace")
                if "/.git/" in resolved + "/" or resolved.endswith("/.git"):
                    return self._deny(
                        req, "fs.git_internals", "writes to .git internals are denied"
                    )
                if not req.attributes.get("actor_can_write", False):
                    return self._deny(req, "fs.write_forbidden", "actor has no write permission")
                return self._allow(
                    req, "fs.write_workspace", "write inside approved workspace", Risk.MEDIUM
                )
        return self._deny(req, "fs.outside_scope", "path is outside every approved scope")

    def _decide_net(self, req: PolicyRequest) -> PolicyDecision:
        host, port = _host_port(req.target or "")
        if not host:
            return self._deny(req, "net.invalid", "network request without a host")
        if port is not None and port in self.root.database_ports:
            return self._deny(
                req, "root.external_database", f"port {port} is a database port; connection denied",
                Risk.CRITICAL,
            )  # fmt: skip
        if host in self.root.internal_hosts:
            return self._deny(
                req, "net.internal", "platform-internal hosts are not reachable by tools"
            )
        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            ip = None
        if ip is not None and (
            any(ip in net for net in _METADATA_NETS) or ip.is_loopback or ip.is_private
        ):
            return self._deny(
                req, "net.private_address", "private or metadata addresses are denied"
            )
        if not req.attributes.get("network_allowed", False):
            return self._deny(req, "net.disabled", "network access is not granted to this actor")
        allowed = [str(h) for h in req.attributes.get("allowed_hosts", [])]
        if not host_allowed(host, allowed):
            return self._deny(req, "net.not_allowlisted", f"host '{host}' is not on the allowlist")
        return self._allow(req, "net.allowlisted", f"host '{host}' is allowlisted", Risk.MEDIUM)

    def _decide_process(self, req: PolicyRequest) -> PolicyDecision:
        target = req.target or ""
        name = os.path.basename(target).lower()
        if name in {b.lower() for b in self.root.database_client_binaries}:
            return self._deny(
                req, "root.external_database", f"'{name}' is a database client and may never run",
                Risk.CRITICAL,
            )  # fmt: skip
        for arg in req.attributes.get("argv", []):
            if os.path.basename(str(arg)).lower() in {
                b.lower() for b in self.root.database_client_binaries
            }:
                return self._deny(
                    req, "root.external_database",
                    "database client referenced in arguments (indirect execution)", Risk.CRITICAL,
                )  # fmt: skip
        allowed = {str(b) for b in req.attributes.get("allowed_processes", [])}
        if target not in allowed:
            return self._deny(
                req, "process.not_allowlisted", f"'{target}' is not an allowed process"
            )
        if not req.attributes.get("registered", False):
            return self._deny(req, "process.unregistered", "unregistered executables never run")
        return self._allow(
            req, "process.allowlisted", "registered, hash-pinned process", Risk.MEDIUM
        )

    def _decide_secret(self, req: PolicyRequest) -> PolicyDecision:
        allowed = {str(s) for s in req.attributes.get("allowed_secrets", [])}
        if not req.target or req.target not in allowed:
            return self._deny(req, "secret.not_granted", "secret is not granted to this tool")
        return self._allow(req, "secret.granted", "secret granted by manifest", Risk.MEDIUM)


def _within(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)
