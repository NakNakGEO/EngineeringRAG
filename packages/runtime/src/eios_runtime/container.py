"""Explicit dependency container shared by API, worker and MCP (no module-level globals)."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Coroutine, Sequence
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
from eios_llm import LLMError, LLMGateway, LLMMessage, LLMRequest
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
    Completion,
    CompletionError,
    FileInsight,
    ProjectIndexer,
    ProjectService,
    ProjectStore,
    SyncResult,
)
from eios_retrieval import ContextGovernor
from eios_retrieval.impact import ImpactAnalyzer, ImpactConfig
from eios_runtime.builtin_adapters import register_builtin_adapters
from eios_runtime.llm_settings import LlmSettings, providers_from
from eios_runtime.onboarding import Onboarding
from eios_runtime.web_research import WebSettings, make_host_gate
from eios_workflow import (
    AgentInfo,
    FactsProvider,
    ProjectFacts,
    WorkflowEngine,
    WorkflowStore,
    load_definitions,
)
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
    llm_settings: LlmSettings
    web: WebSettings
    onboarding: Onboarding
    insight: FileInsight
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

    async def guard(name: str, locality: str, classification: str) -> tuple[bool, str]:
        decision = await policy.evaluate(
            PolicyRequest(
                actor_type=ActorType.SYSTEM,
                actor_id="llm_gateway",
                action="llm.call",
                target=name,
                attributes={
                    "configured": name in gateway.names,  # live: the model can change at runtime
                    "locality": locality,
                    "classification": classification,
                },
            )
        )
        return decision.effect is PolicyEffect.ALLOW, "; ".join(decision.reasons)

    gateway = LLMGateway(
        providers_from(settings, None), default=settings.llm_default_provider, guard=guard
    )
    return gateway


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


def _project_facts(impact: ImpactAnalyzer, projects: ProjectService) -> FactsProvider:
    """Impact + coverage of the named targets, as the plain facts the workflow engine consumes."""

    async def provide(
        project_id: uuid.UUID, paths: Sequence[str], symbols: Sequence[str]
    ) -> ProjectFacts:
        report = await impact.analyze(project_id, paths=list(paths), symbols=list(symbols))
        coverage = await projects.semantic_coverage(project_id, branch=report.branch)
        return ProjectFacts(
            risk=report.risk,
            dependents=len(report.consumers),
            tests_missing=report.tests_missing,
            touches_sql=bool(report.db_scripts),
            coverage=coverage["symbol_coverage"],
            summary={
                "branch": report.branch,
                "risk": report.risk.value,
                "risk_factors": report.risk_factors[:10],
                "targets": report.targets[:20],
                "unresolved": report.unresolved[:10],
                "callers": len(report.callers),
                "dependents": len(report.dependents),
                "consumers": report.consumers[:20],
                "tests": report.tests[:10],
                "tests_missing": report.tests_missing,
                "db_scripts": report.db_scripts[:10],
                "confidence": report.confidence,
                "symbol_coverage": round(coverage["symbol_coverage"], 3),
            },
        )

    return provide


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
    impact = ImpactAnalyzer(
        projects_service, knowledge, knowledge.decisions, ImpactConfig.from_settings(settings)
    )
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

    llm = build_llm_gateway(settings, policy)
    llm_settings = LlmSettings(engine, settings, llm)
    web = WebSettings(engine, settings)
    onboarding = Onboarding(engine, make_host_gate(policy, web))

    async def complete_summary(system: str, user: str) -> Completion:
        """One short JSON answer for a file summary, with no "thinking" for models that think."""
        messages = [
            LLMMessage(role="system", content=system),
            LLMMessage(role="user", content=user),
        ]

        async def call(*, plain: bool, tokens: int) -> Any:
            return await llm.complete(
                LLMRequest(
                    messages=messages, max_tokens=tokens, temperature=0.0,
                    json_mode=not plain, reasoning=None if plain else "none",
                    data_classification="project",
                ),
                actor_id="file_insight",
            )  # fmt: skip

        try:
            try:
                reply = await call(plain=False, tokens=220)
            except LLMError as exc:
                if exc.status != 400:
                    raise
                reply = await call(plain=True, tokens=400)  # a server that does not know the extras
            if not reply.text.strip():
                reply = await call(plain=True, tokens=1500)  # it thought for the whole budget
        except LLMError as exc:
            raise CompletionError(str(exc)) from exc
        return Completion(
            text=reply.text, model=reply.model, provider=reply.provider,
            input_tokens=reply.usage.input_tokens, output_tokens=reply.usage.output_tokens,
        )  # fmt: skip

    insight = FileInsight(projects_service, complete_summary, lambda: bool(llm.names))
    refreshing: set[asyncio.Task[Any]] = set()

    async def refresh_summaries(result: SyncResult) -> None:
        """Files that already have a summary and just changed get a new one, in the background."""
        paths = list(
            dict.fromkeys(
                [*result.changed, *(p for p, c in result.overlay_changes if c != "removed")]
            )
        )
        await llm_settings.refresh()  # the model may have been changed in the UI (another process)
        if not paths or not llm.names:
            return
        task = asyncio.get_running_loop().create_task(
            insight.refresh_changed(result.project_id, paths)
        )
        refreshing.add(task)
        task.add_done_callback(refreshing.discard)

    projects_service.add_sync_listener(refresh_summaries)

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
        impact=impact,
        adapters=adapters,
        registry=registry,
        router=router,
        health=HealthChecker(registry_store, adapters),
        policy=policy,
        approvals=approvals,
        audit=audit,
        secrets=SecretsBroker(),
        tools=tools,
        llm=llm,
        llm_settings=llm_settings,
        web=web,
        onboarding=onboarding,
        insight=insight,
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
            facts=_project_facts(impact, projects_service),
        ),
    )
    register_builtin_adapters(adapters, container)
    return container
