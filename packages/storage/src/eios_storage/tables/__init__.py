"""Table definitions, one module per logical PostgreSQL schema (master plan section 7).

Importing this package registers every table on the shared ``metadata``.
"""

from eios_storage.tables import (
    evidence,
    graph,
    knowledge,
    memory,
    observability,
    platform,
    project,
    source,
)

__all__ = [
    "evidence",
    "graph",
    "knowledge",
    "memory",
    "observability",
    "platform",
    "project",
    "source",
]
