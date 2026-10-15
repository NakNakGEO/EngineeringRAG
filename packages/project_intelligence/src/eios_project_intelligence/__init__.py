"""Project identity, git state, inventory, symbols, dependency graph, incremental indexing."""

from eios_project_intelligence.indexer import BootstrapResult, ProjectIndexer, SyncResult
from eios_project_intelligence.insight import (
    Completion,
    CompletionError,
    FileInsight,
    Summary,
    SummaryState,
)
from eios_project_intelligence.service import SYNC_JOB, ProjectService
from eios_project_intelligence.store import Project, ProjectStore
from eios_project_intelligence.workspace import ApprovedWorkspaces, WorkspaceViolationError

__all__ = [
    "SYNC_JOB",
    "ApprovedWorkspaces",
    "BootstrapResult",
    "Completion",
    "CompletionError",
    "FileInsight",
    "Project",
    "ProjectIndexer",
    "ProjectService",
    "ProjectStore",
    "Summary",
    "SummaryState",
    "SyncResult",
    "WorkspaceViolationError",
]
