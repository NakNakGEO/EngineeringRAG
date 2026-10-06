# ADR 0001: Python-first

Status: Accepted (Phase 0)

## Context
The platform is the durable engineering layer under replaceable LLMs: RAG, embeddings, parsing,
agents, evaluation and tool integration. The Python ecosystem is the strongest for all of these, and
every LLM vendor and local runtime ships Python-first tooling.

## Decision
Python 3.12+ is the core language (`requires-python = ">=3.12"`; containers run 3.12). Baseline:
FastAPI, Pydantic v2, SQLAlchemy 2.x, Psycopg 3, Alembic, asyncio, pytest, ruff, mypy (`--strict`),
and uv for environments and the lockfile. Domain interfaces use strict typing and Pydantic at the
boundaries.

Non-Python tools (C#, Rust, Node, Go, Java) are integrated later through adapters behind the tool
runtime; they are not part of the core.

## Consequences
- One toolchain for API, worker, MCP server, migrations and tests.
- mypy picked over pyright: it is a single Python-native dependency that runs under `uv`.
- Python's GIL is acceptable: the work is I/O bound or delegated to PostgreSQL, subprocess tools and
  LLM endpoints; CPU-heavy work moves to worker processes.
