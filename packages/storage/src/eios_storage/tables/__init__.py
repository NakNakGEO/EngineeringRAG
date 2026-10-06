"""Table definitions, one module per logical PostgreSQL schema (master plan section 7).

Importing this package registers every table on the shared ``metadata``.
"""

from eios_storage.tables import (
    agent,
    capability,
    evidence,
    graph,
    knowledge,
    memory,
    observability,
    platform,
    policy,
    project,
    skill,
    source,
    workflow,
)

__all__ = [
    "agent",
    "capability",
    "evidence",
    "graph",
    "knowledge",
    "memory",
    "observability",
    "platform",
    "policy",
    "project",
    "skill",
    "source",
    "workflow",
]
