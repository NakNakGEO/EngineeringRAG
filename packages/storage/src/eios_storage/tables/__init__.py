"""Table definitions, one module per logical PostgreSQL schema (master plan section 7).

Importing this package registers every table on the shared ``metadata``.
"""

from eios_storage.tables import evidence, knowledge, memory, observability, platform

__all__ = ["evidence", "knowledge", "memory", "observability", "platform"]
