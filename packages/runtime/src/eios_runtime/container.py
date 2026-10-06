"""Explicit dependency container shared by API, worker and MCP (no module-level globals)."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Coroutine
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncEngine

from eios_capability import (
    AdapterCatalog,
    CapabilityRouter,
    HealthChecker,
    ProviderRow,
    RegistryService,
    RegistryStore,
    SyncReport,
    ToolManifest,
    load_directory,
)
from eios_core.logging import get_logger
from eios_core.settings import Settings
from eios_domain.events import ActorType
from eios_domain.policy import PolicyEffect, PolicyRequest
from eios_domain.registry import Origin
from eios_governance import GovernanceService, GovernanceStore
from eios_governance.decisions import DecisionLifecycle
from eios_governance.evaluation import EvaluationService
from eios_governance.maintenance import MaintenanceService
from eios_governance.retention import RetentionPolicy, RetentionService
from eios_jobs import PostgresJobQueue
from eios_knowledge import (
    DecisionRepository,
    EvidencePipeline,
    EvidenceRepository,
    HashingEmbedder,
    KnowledgeRepository,
    KnowledgeService,
    LocalBlobStore,
    MemoryRepository,
)
from eios_llm import (
    AnthropicProvider,
    LLMGateway,
    LLMProvider,
    LocalOpenAIProvider,
    OpenAICompatibleProvider,
)
from eios_observability import (
    EventHub,
    PostgresEventStore,
    PostgresRunRepository,
    RunRecorder,
)
from eios_policy import (
    PolicyEngine,
    PostgresApprovalStore,
    PostgresAuditLog,
    SecretsBroker,
    SubprocessSandbox,
    ToolRuntime,
    load_root_policy,
)
from eios_project_intelligence import (
    ApprovedWorkspaces,
    ProjectIndexer,
    ProjectService,
    ProjectStore,
    SyncResult,
)
from eios_retrieval import ContextGovernor
from eios_retrieval.impact import ImpactAnalyzer
from eios_runtime.builtin_adapters import register_builtin_adapters
from eios_workflow import AgentInfo, WorkflowEngine, WorkflowStore, load_definitions
from eios_workshop import GapResolver, Workshop, WorkshopStore

_log = get_logger("eios.runtime")


class BackgroundTasks:
    """Tracks fire-and-forget tasks so they are logged on failure and cancelled on shutdown."""

    def __init__(self) -> None:
        self._tasks: set[asyncio.Task[Any]] = set()

    def spawn(self, coro: Coroutine[Any, Any, Any], *, name: str) -> asyncio.Task[Any]:
        task = asyncio.create_task(coro, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._done)
        return task

    def _done(self, task: asyncio.Task[Any]) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and (exc := task.exception()) is not None:
            _log.error("background_task_failed", task=task.get_name(), error=repr(exc))

    async def shutdown(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)


@dataclass
class Container:
    settings: Settings
    engine: AsyncEngine
    runs: PostgresRunRepository
    events: PostgresEventStore
    hub: EventHub
    recorder: RunRecorder
    blobs: LocalBlobStore
    knowledge: KnowledgeService
    evidence: EvidenceRepository
    pipeline: EvidencePipeline
    queue: PostgresJobQueue
    workspaces: ApprovedWorkspaces
    projects: ProjectService
    governor: ContextGovernor
    impact: ImpactAnalyzer
    adapters: AdapterCatalog
    registry: RegistryService
    router: CapabilityRouter
    health: HealthChecker
    policy: PolicyEngine
    approvals: PostgresApprovalStore
    audit: PostgresAuditLog
    secrets: SecretsBroker
    tools: ToolRuntime
    workflows: WorkflowEngine
    llm: LLMGateway
    governance: GovernanceService
    decisions: DecisionLifecycle
    evaluation: EvaluationService
    maintenance: MaintenanceService
    resolver: GapResolver
    retention: RetentionService
    workshop: Workshop
    background: BackgroundTasks = field(default_factory=BackgroundTasks)

    async def sync_registries(self) -> SyncReport:
        """Load builtin (and optional plugin) manifests, register them, refresh health."""
        report = await self.registry.sync(
            load_directory(self.settings.manifests_dir), origin=Origin.BUILTIN
        )
        if self.settings.plugins_dir is not None:
            plugin = await self.registry.sync(
                load_directory(self.settings.plugins_dir), origin=Origin.PLUGIN
            )
            report.added += plugin.added
            report.updated += plugin.updated
            report.unchanged += plugin.unchanged
            report.rejected += plugin.rejected
        await self.health.run_all()
        for source, reason in report.rejected:
            _log.warning("manifest_rejected", source=source, reason=reason)
        return report


def build_llm_gateway(settings: Settings, policy: PolicyEngine) -> LLMGateway:
    """Providers exist only when configured; every call is checked by the Policy Engine."""
    providers: dict[str, LLMProvider] = {}
    if settings.llm_local_base_url and settings.llm_local_model:
        providers["local"] = LocalOpenAIProvider(
            base_url=settings.llm_local_base_url, default_model=settings.llm_local_model
        )
    if settings.llm_openai_api_key and settings.llm_openai_model:
        providers["openai"] = OpenAICompatibleProvider(
            name="openai",
            base_url=settings.llm_openai_base_url,
            api_key=settings.llm_openai_api_key.get_secret_value(),
            default_model=settings.llm_openai_model,
        )
    if settings.llm_anthropic_api_key and settings.llm_anthropic_model:
        providers["anthropic"] = AnthropicProvider(
            api_key=settings.llm_anthropic_api_key.get_secret_value(),
            default_model=settings.llm_anthropic_model,
        )

    async def guard(name: str, locality: str, classification: str) -> tuple[bool, str]:
        decision = await policy.evaluate(
            PolicyRequest(
                actor_type=ActorType.SYSTEM,
                actor_id="llm_gateway",
                action="llm.call",
                target=name,
                attributes={
                    "configured": name in providers,
                    "locality": locality,
                    "classification": classification,
                },
            )
        )
        return decision.effect is PolicyEffect.ALLOW, "; ".join(decision.reasons)

    return LLMGateway(providers, default=settings.llm_default_provider, guard=guard)


async def _agent_infos(store: RegistryStore) -> dict[str, AgentInfo]:
    """Registered agent roles as the team selector sees them (routable = usable now)."""
    from eios_domain.registry import ROUTABLE_STATES, RegistryState

    infos: dict[str, AgentInfo] = {}
    for row in await store.list_agents():
        routable = RegistryState(row["state"]) in ROUTABLE_STATES
        infos[row["id"]] = AgentInfo(
            id=row["id"],
            can_write=bool(row["can_write"]),
            routable=routable,
            capabilities=tuple(row["manifest"].get("capabilities", [])),
        )
    return infos


def build_container(settings: Settings, engine: AsyncEngine) -> Container:
    hub = EventHub()
    runs = PostgresRunRepository(engine)
    events = PostgresEventStore(engine)
    blobs = LocalBlobStore(settings.blob_dir)
    knowledge = KnowledgeService(
        KnowledgeRepository(engine),
        MemoryRepository(engine),
        DecisionRepository(engine),
        HashingEmbedder(),
    )
    evidence = EvidenceRepository(engine)
    queue = PostgresJobQueue(engine)
    recorder = RunRecorder(runs, events, hub)
    workspaces = ApprovedWorkspaces(settings.workspace_root_paths)
    store = ProjectStore(engine)
    indexer = ProjectIndexer(
        store,
        workspaces,
        max_file_bytes=settings.max_indexed_file_bytes,
        overlay_ttl=timedelta(seconds=settings.overlay_ttl_seconds),
    )
    projects_service = ProjectService(store, indexer, queue, recorder)
    registry_store = RegistryStore(engine)
    root_policy = load_root_policy(settings.root_policy_path)  # fail closed on any mismatch
    approvals = PostgresApprovalStore(engine, ttl=timedelta(seconds=settings.approval_ttl_seconds))
    audit = PostgresAuditLog(engine)
    policy = PolicyEngine(
        root_policy,
        audit=audit,
        approvals=approvals,
        workspace_roots=settings.workspace_root_paths,
        sandbox_output_dir=settings.sandbox_output_dir,
    )
    router = CapabilityRouter(registry_store)
    registry = RegistryService(registry_store)
    governance_store = GovernanceStore(engine)
    adapters = AdapterCatalog()
    sandbox = SubprocessSandbox(
        root_policy, require_network_isolation=settings.sandbox_require_network_isolation
    )
    tools = ToolRuntime(
        router=router,
        store=registry_store,
        adapters=adapters,
        policy=policy,
        sandbox=sandbox,
        call_timeout_seconds=settings.tool_call_timeout_seconds,
    )

    async def committed_files(project_id: uuid.UUID) -> dict[str, str] | None:
        project = await store.get_project(project_id)
        if project is None or project.last_branch is None:
            return None
        files = await store.committed_files(project_id, project.last_branch)
        return {path: content_hash for path, (_, content_hash) in files.items()}

    governance = GovernanceService(
        governance_store, knowledge.knowledge, evidence, committed_files=committed_files
    )

    async def on_synced(result: SyncResult) -> None:
        if result.changed or result.removed:
            await governance.invalidate_for_changes(
                result.project_id, changed=result.changed, removed=result.removed
            )

    projects_service.add_sync_listener(on_synced)

    async def run_provider(provider: ProviderRow, arguments: dict[str, Any]) -> dict[str, Any]:
        return await tools.execute_provider(
            ToolManifest.model_validate(provider.manifest), arguments
        )

    evaluation = EvaluationService(engine, registry, run_provider)

    container = Container(
        settings=settings,
        engine=engine,
        runs=runs,
        events=events,
        hub=hub,
        recorder=recorder,
        blobs=blobs,
        knowledge=knowledge,
        evidence=evidence,
        pipeline=EvidencePipeline(
            evidence,
            knowledge,
            blobs,
            max_evidence_bytes=settings.max_evidence_bytes,
            default_ttl=timedelta(seconds=settings.ephemeral_ttl_seconds),
        ),
        queue=queue,
        workspaces=workspaces,
        projects=projects_service,
        governor=ContextGovernor(
            projects_service, knowledge, knowledge.memory, knowledge.decisions
        ),
        impact=ImpactAnalyzer(projects_service, knowledge, knowledge.decisions),
        adapters=adapters,
        registry=registry,
        router=router,
        health=HealthChecker(registry_store, adapters),
        policy=policy,
        approvals=approvals,
        audit=audit,
        secrets=SecretsBroker(),
        tools=tools,
        llm=build_llm_gateway(settings, policy),
        governance=governance,
        decisions=DecisionLifecycle(knowledge.decisions, governance_store, engine),
        evaluation=evaluation,
        maintenance=MaintenanceService(engine, governance),
        retention=RetentionService(
            engine,
            blobs,
            RetentionPolicy(
                event_days=settings.retention_event_days,
                audit_days=settings.retention_audit_days,
                job_days=settings.retention_job_days,
            ),
        ),
        resolver=GapResolver(registry_store, router, policy),
        workshop=Workshop(
            store=WorkshopStore(engine),
            registry=registry,
            policy=policy,
            sandbox=sandbox,
            evaluation=evaluation,
            directory=settings.workshop_dir,
        ),
        workflows=WorkflowEngine(
            store=WorkflowStore(engine),
            definitions=load_definitions(settings.workflows_dir)[0],
            recorder=recorder,
            events=events,
            policy=policy,
            agents=lambda: _agent_infos(registry_store),
            evidence=evidence.get_many,
        ),
    )
    register_builtin_adapters(adapters, container)
    return container
