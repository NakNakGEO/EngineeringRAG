"""Explicit dependency container shared by API, worker and MCP (no module-level globals)."""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncEngine

from eios_capability import (
    AdapterCatalog,
    CapabilityRouter,
    HealthChecker,
    RegistryService,
    RegistryStore,
    SyncReport,
    load_directory,
)
from eios_core.logging import get_logger
from eios_core.settings import Settings
from eios_domain.registry import Origin
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
)
from eios_retrieval import ContextGovernor
from eios_retrieval.impact import ImpactAnalyzer
from eios_runtime.builtin_adapters import register_builtin_adapters
from eios_workflow import AgentInfo, WorkflowEngine, WorkflowStore, load_definitions

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


async def _agent_infos(store: RegistryStore) -> dict[str, AgentInfo]:
    """Registered agent roles as the team selector sees them (routable = usable now)."""
    from eios_domain.registry import Origin, RegistryState, is_routable

    infos: dict[str, AgentInfo] = {}
    for row in await store.list_agents():
        routable = is_routable(
            RegistryState(row["state"]),
            Origin(row["origin"]),
            approved=row["approved_by"] is not None,
        )
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
    adapters = AdapterCatalog()
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
        registry=RegistryService(registry_store),
        router=router,
        health=HealthChecker(registry_store, adapters),
        policy=policy,
        approvals=approvals,
        audit=audit,
        secrets=SecretsBroker(),
        tools=ToolRuntime(
            router=router,
            store=registry_store,
            adapters=adapters,
            policy=policy,
            sandbox=SubprocessSandbox(
                root_policy,
                require_network_isolation=settings.sandbox_require_network_isolation,
            ),
            call_timeout_seconds=settings.tool_call_timeout_seconds,
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
