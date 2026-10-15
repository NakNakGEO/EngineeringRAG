"""The content index: documents, configuration, SQL and migrations are retrievable (ADR 0018)."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine

from eios_domain.events import EventType
from eios_domain.policy import Risk
from eios_domain.project import FileScope
from eios_retrieval import ContextLevel, RetrievalQuery
from eios_retrieval.coverage import resolve_entities
from eios_retrieval.models import ContextItem, Source
from eios_runtime import Container, build_container
from eios_storage.tables.source import chunk as chunk_t
from eios_storage.tables.source import file as file_t
from tests.conftest import make_settings
from tests.gitfixtures import commit_all, make_repo, write

pytestmark = pytest.mark.integration

ADR_5 = """# ADR 0005: The ledger is append-only

Status: Accepted

## Decision
Ledger rows are never updated or deleted by application code. A database trigger rejects every
UPDATE and DELETE on the ledger table, so history cannot be rewritten by accident.

## Retention
The retention job still purges entries older than the retention window. It switches the
`eios.purge_mode` setting on inside its own transaction, which lets the purge through the trigger.

## Consequences
Corrections are new rows that reference the row they correct.
"""

ADR_12 = """# ADR 0012: Snapshot cadence

Status: Accepted

## Decision
Snapshots of the ledger are taken every night at 02:00 and kept for a fortnight.
"""

RUNBOOK = """# Runbook

## Restoring the ledger from a backup
1. Stop the writers.
2. Restore the most recent dump with pg_restore.
3. Replay the write-ahead log up to the incident.
"""

THREAT_MODEL = """# Threat model

## Tampering
An operator with database access could rewrite history; the append-only trigger and the
audit export are the controls.
"""

APP_YAML = """# Application settings
app:
  name: ledger-service
ledger:
  retention_days: 30
  flush_interval_seconds: 5
logging:
  level: INFO
"""

DOCKERFILE = """FROM python:3.12 AS builder
RUN pip install --no-cache-dir build
COPY . /src
RUN python -m build /src

FROM python:3.12-slim AS runtime
COPY --from=builder /src/dist /dist
RUN pip install /dist/*.whl
HEALTHCHECK CMD python -m app.health
CMD ["python", "-m", "app"]
"""

MIGRATION = '''"""Ledger table with an append-only trigger.

Revision ID: 0002
Revises: 0001
"""

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"


def upgrade() -> None:
    op.create_table(
        "ledger",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("run_id", sa.UUID(), nullable=False),
        sa.Column("seq", sa.BigInteger(), nullable=False),
        sa.UniqueConstraint("run_id", "seq", name="uq_ledger_run_seq"),
    )
    op.execute(
        """
        CREATE TRIGGER trg_ledger_no_update BEFORE UPDATE OR DELETE ON ledger
        FOR EACH ROW EXECUTE FUNCTION ledger.forbid_mutation()
        """
    )


def downgrade() -> None:
    op.drop_table("ledger")
'''

LEDGER_PY = '''"""The ledger."""


class Ledger:
    def append(self, entry: str) -> int:
        return len(entry)

    def purge_expired(self) -> int:
        return 0
'''


def _repo_files() -> dict[str, str | bytes]:
    lock = "version = 1\n" + "".join(f'[[package]]\nname = "dep{n}"\n' for n in range(3000))
    return {
        "app/__init__.py": "",
        "app/ledger.py": LEDGER_PY,
        "docs/adr/0005-ledger-append-only.md": ADR_5,
        "docs/adr/0012-snapshot-cadence.md": ADR_12,
        "docs/runbook.md": RUNBOOK,
        "docs/threat-model.md": THREAT_MODEL,
        "config/app.yaml": APP_YAML,
        "Dockerfile": DOCKERFILE,
        "migrations/versions/0002_ledger.py": MIGRATION,
        "uv.lock": lock,
        ".env": "LEDGER_PASSWORD=hunter2\n",
        "assets/logo.png": b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR",
        "tests/test_ledger.py": (
            "from app.ledger import Ledger\n\n\ndef test_append():\n"
            "    assert Ledger().append('x') == 1\n"
        ),
    }


class Ledger:
    def __init__(
        self, container: Container, engine: AsyncEngine, repo: Path, project_id: uuid.UUID
    ):
        self.container = container
        self.engine = engine
        self.repo = repo
        self.project_id = project_id

    async def sync(self) -> None:
        await self.container.projects.sync_now(self.project_id)

    def query(self, text: str, **kw: object) -> RetrievalQuery:
        kw.setdefault("risk", Risk.LOW)
        return RetrievalQuery(text=text, project_id=self.project_id, **kw)  # type: ignore[arg-type]

    async def search(self, text: str, **kw: object) -> list[ContextItem]:
        result = await self.container.governor.search(
            self.query(text, **kw), level=ContextLevel.L1, limit=10
        )
        return result.items

    async def paths(self, text: str, **kw: object) -> list[str]:
        return [i.candidate.path or "" for i in await self.search(text, **kw)]

    async def content_paths(self, text: str, **kw: object) -> list[str]:
        """Only what the content retriever itself surfaced (other retrievers may know a path)."""
        return [
            i.candidate.path or ""
            for i in await self.search(text, **kw)
            if Source.CONTENT.value in i.sources
        ]

    async def chunk_rows(self, path: str) -> list[Any]:
        async with self.engine.connect() as conn:
            return list(
                (
                    await conn.execute(
                        sa.select(
                            chunk_t.c.scope,
                            chunk_t.c.start_line,
                            chunk_t.c.end_line,
                            chunk_t.c.text,
                        )
                        .where(chunk_t.c.project_id == self.project_id, chunk_t.c.path == path)
                        .order_by(chunk_t.c.scope, chunk_t.c.ordinal)
                    )
                ).all()
            )


@pytest.fixture
async def ledger(db: AsyncEngine, tmp_path: Path) -> Ledger:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    container = build_container(
        make_settings(workspace_roots=str(workspace), blob_dir=tmp_path / "blobs"), db
    )
    repo = make_repo(workspace, "ledger", _repo_files())
    boot = await container.projects.bootstrap(repo)
    assert boot.project is not None
    await container.projects.sync_now(boot.project.id)
    return Ledger(container, db, repo, boot.project.id)


# -- natural-language questions reach documents, config, Dockerfiles and migrations -----------
async def test_a_why_question_is_answered_by_the_adr_not_by_code(ledger: Ledger) -> None:
    pack = await ledger.container.governor.build(
        ledger.query("Why is the ledger append-only, and how does retention still purge it?"),
        max_level=ContextLevel.L1,
    )
    adr = next(i for i in pack.items if i.candidate.path == "docs/adr/0005-ledger-append-only.md")
    assert Source.CONTENT.value in adr.sources
    c = adr.candidate
    # the snippet is exactly the chunk that matched, loaded from the file
    assert c.kind.value == "file" and c.id == "file:docs/adr/0005-ledger-append-only.md"
    assert c.start_line is not None and c.end_line is not None and c.start_line <= c.end_line
    assert "retention" in c.content.lower() and "eios.purge_mode" in c.content
    assert pack.gap.ready_to_act is True or not any(
        m.kind == "no_rationale_evidence" for m in pack.gap.missing_context
    )


async def test_config_dockerfile_and_migration_are_found_at_the_first_level(ledger: Ledger) -> None:
    cases = {
        "How many days of ledger history do we keep before purging?": "config/app.yaml",
        "Which Docker build stage runs the health check?": "Dockerfile",
        "Which migration creates the trigger that blocks ledger updates?": (
            "migrations/versions/0002_ledger.py"
        ),
        "Which unique constraint orders the ledger rows of a run?": (
            "migrations/versions/0002_ledger.py"
        ),
    }
    for question, expected in cases.items():
        pack = await ledger.container.governor.build(
            ledger.query(question), max_level=ContextLevel.L1
        )
        assert pack.levels_tried == [ContextLevel.L1], question
        found = {i.candidate.path or "": i for i in pack.items}
        assert expected in found, (question, sorted(found))
        assert Source.CONTENT.value in found[expected].sources


async def test_content_hits_are_observable_events(ledger: Ledger) -> None:
    run = await ledger.container.recorder.start_run(
        kind="context_build", project_id=ledger.project_id
    )
    await ledger.container.governor.build(
        ledger.query("Why is the ledger append-only, and how does retention still purge it?"),
        max_level=ContextLevel.L1,
        ctx=run,
    )
    events = (await ledger.container.events.list_events(run.run_id, limit=500)).items
    hits = [e for e in events if e.type == EventType.KNOWLEDGE_HIT]
    assert any(e.data["id"] == "file:docs/adr/0005-ledger-append-only.md" for e in hits)
    assert all("content" in e.data["sources"] or e.data["id"] for e in hits)


async def test_the_snippet_of_a_migration_hit_is_the_matching_statement(ledger: Ledger) -> None:
    pack = await ledger.container.governor.build(
        ledger.query("Which migration creates the trigger that blocks ledger updates?"),
        max_level=ContextLevel.L1,
    )
    item = next(i for i in pack.items if i.candidate.path == "migrations/versions/0002_ledger.py")
    assert "trg_ledger_no_update" in item.candidate.content


async def test_a_document_is_found_by_its_adr_number_or_its_name(ledger: Ledger) -> None:
    assert (await ledger.paths("What does ADR 0005 decide?"))[0] == (
        "docs/adr/0005-ledger-append-only.md"
    )
    assert "docs/adr/0012-snapshot-cadence.md" in (await ledger.paths("Summarise decision 12"))[:3]
    assert (await ledger.paths("adr-12"))[0] == "docs/adr/0012-snapshot-cadence.md"
    assert (await ledger.paths("What is in the threat model?"))[0] == "docs/threat-model.md"
    # no word of the file's body is in the question - the name alone finds it
    assert (await ledger.paths("open the runbook"))[0] == "docs/runbook.md"
    # the number picks the file: the other ADR is not what "ADR 0012" asks for
    assert (await ledger.paths("ADR 0012"))[0] == "docs/adr/0012-snapshot-cadence.md"


async def test_everyday_words_do_not_make_a_chunk_relevant(ledger: Ledger) -> None:
    # only generic words in the question: nothing is rare enough to count as a match
    assert await ledger.paths("what is the best way to do this thing today") == []


# -- a changed or deleted file updates its chunks -------------------------------------------
async def test_changing_a_file_replaces_its_chunks_and_deleting_it_removes_them(
    ledger: Ledger,
) -> None:
    path = "docs/runbook.md"
    assert any("pg_restore" in r.text for r in await ledger.chunk_rows(path))
    assert path in await ledger.paths("how do I restore the ledger with pg_restore")

    write(ledger.repo, {path: "# Runbook\n\n## Rotating the ledger keys\nUse the vault tool.\n"})
    commit_all(ledger.repo, "rewrite runbook")
    await ledger.sync()
    rows = await ledger.chunk_rows(path)
    assert rows and all("pg_restore" not in r.text for r in rows)
    assert any("vault tool" in r.text for r in rows)
    assert path not in await ledger.paths("how do I restore the ledger with pg_restore")
    assert path in await ledger.paths("how do we rotate the ledger keys with the vault tool")

    (ledger.repo / path).unlink()
    commit_all(ledger.repo, "drop runbook")
    await ledger.sync()
    assert await ledger.chunk_rows(path) == []
    assert path not in await ledger.paths("how do we rotate the ledger keys with the vault tool")


async def test_an_unchanged_file_keeps_its_chunk_rows_across_syncs(ledger: Ledger) -> None:
    before = await ledger.chunk_rows("docs/adr/0005-ledger-append-only.md")
    async with ledger.engine.connect() as conn:
        ids = {
            r[0]
            for r in (
                await conn.execute(
                    sa.select(chunk_t.c.id).where(chunk_t.c.path == "config/app.yaml")
                )
            ).all()
        }
    write(ledger.repo, {"docs/runbook.md": RUNBOOK + "\n4. Verify the row count.\n"})
    commit_all(ledger.repo, "touch one file")
    await ledger.sync()
    after = await ledger.chunk_rows("docs/adr/0005-ledger-append-only.md")
    assert [tuple(r) for r in before] == [tuple(r) for r in after]
    async with ledger.engine.connect() as conn:
        again = {
            r[0]
            for r in (
                await conn.execute(
                    sa.select(chunk_t.c.id).where(chunk_t.c.path == "config/app.yaml")
                )
            ).all()
        }
    assert ids == again  # not even re-inserted


# -- overlay: uncommitted work is the current truth, and never mixes with HEAD ----------------
async def test_an_uncommitted_edit_replaces_the_committed_text_in_search(ledger: Ledger) -> None:
    path = "docs/runbook.md"
    write(ledger.repo, {path: "# Runbook\n\n## Freezing the ledger\nPause the flush worker.\n"})
    await ledger.sync()

    rows = await ledger.chunk_rows(path)
    assert {r.scope for r in rows} == {"committed", "overlay"}
    items = await ledger.search("how do we freeze the ledger and pause the flush worker")
    hit = next(i for i in items if i.candidate.path == path)
    assert hit.candidate.scope == "overlay"
    assert (
        "Pause the flush worker"
        in (
            await ledger.container.projects.read_source(
                ledger.project_id,
                path,
                start_line=hit.candidate.start_line,
                end_line=hit.candidate.end_line,
            )
            or {}
        )["text"]
    )
    # the committed text of the same file is hidden while the working-tree version exists...
    assert path not in await ledger.content_paths("how do I restore with pg_restore")
    # ...and visible again when the caller asks for the committed view only
    assert path in await ledger.content_paths(
        "how do I restore with pg_restore", include_overlay=False
    )
    assert path not in await ledger.content_paths(
        "how do we freeze the ledger and pause the flush worker", include_overlay=False
    )


async def test_a_file_deleted_in_the_working_tree_is_hidden_from_content_search(
    ledger: Ledger,
) -> None:
    path = "docs/threat-model.md"
    (ledger.repo / path).unlink()
    await ledger.sync()
    assert path not in await ledger.content_paths("which controls stop tampering with history")
    assert path not in await ledger.content_paths("open the threat model")
    assert path in await ledger.content_paths("open the threat model", include_overlay=False)
    assert path in await ledger.content_paths(
        "which controls stop tampering with history", include_overlay=False
    )

    # restoring the file clears the tombstone: the committed chunks are visible again
    write(ledger.repo, {path: THREAT_MODEL})
    await ledger.sync()
    assert path in await ledger.content_paths("open the threat model")
    assert {r.scope for r in await ledger.chunk_rows(path)} == {"committed"}


async def test_an_untracked_new_file_is_searchable_until_it_is_removed(ledger: Ledger) -> None:
    write(
        ledger.repo, {"docs/notes.md": "# Notes\n\nThe zebra protocol governs ledger sharding.\n"}
    )
    await ledger.sync()
    assert "docs/notes.md" in await ledger.paths("what does the zebra protocol say about sharding")
    (ledger.repo / "docs/notes.md").unlink()
    await ledger.sync()
    assert await ledger.chunk_rows("docs/notes.md") == []


async def test_expired_overlay_chunks_are_purged_with_their_files(ledger: Ledger) -> None:
    write(ledger.repo, {"docs/notes.md": "# Notes\n\nThe zebra protocol governs sharding.\n"})
    await ledger.sync()
    assert await ledger.chunk_rows("docs/notes.md")
    async with ledger.engine.begin() as conn:
        await conn.execute(
            sa.update(file_t)
            .where(file_t.c.scope == "overlay")
            .values(expires_at=sa.text("now() - interval '1 hour'"))
        )
    assert await ledger.container.projects.store.purge_expired_overlays() >= 1
    assert await ledger.chunk_rows("docs/notes.md") == []


# -- what is never indexed as text ------------------------------------------------------------
async def test_lockfiles_secrets_and_binaries_are_not_indexed_as_text(ledger: Ledger) -> None:
    lock = await ledger.chunk_rows("uv.lock")
    assert len(lock) == 1 and lock[0].end_line <= 12 and "dep2999" not in lock[0].text
    assert await ledger.chunk_rows(".env") == []
    assert await ledger.chunk_rows("assets/logo.png") == []
    assert "hunter2" not in " ".join(r.text for r in await ledger.chunk_rows("config/app.yaml"))
    assert ".env" not in await ledger.paths("what is the ledger password hunter2")


# -- entity fallback: names only a migration, document or config file carries ----------------
async def test_a_constraint_name_resolves_to_the_migration_that_defines_it(ledger: Ledger) -> None:
    q = ledger.query("What does the `uq_ledger_run_seq` constraint guarantee?")
    rc, _ = await ledger.container.governor._prepare(q)
    resolved = await resolve_entities(ledger.container.projects, rc)
    assert resolved["uq_ledger_run_seq"] == ["file:migrations/versions/0002_ledger.py"]

    pack = await ledger.container.governor.build(q, max_level=ContextLevel.L1)
    exact = [i for i in pack.items if Source.EXACT.value in i.sources]
    assert [i.candidate.id for i in exact] == ["file:migrations/versions/0002_ledger.py"]
    assert exact[0].critical and "uq_ledger_run_seq" in exact[0].candidate.content
    kinds = {m.kind for m in pack.gap.missing_context}
    assert "unresolved_entity" not in kinds and "entity_not_included" not in kinds
    assert pack.coverage.entities_found == pack.coverage.entities_total >= 1


async def test_trigger_guc_and_yaml_key_names_resolve_to_their_files(ledger: Ledger) -> None:
    q = ledger.query(
        "Which `trg_ledger_no_update`, `eios.purge_mode` and `flush_interval_seconds` apply?"
    )
    rc, _ = await ledger.container.governor._prepare(q)
    resolved = await resolve_entities(ledger.container.projects, rc)
    assert resolved["trg_ledger_no_update"] == ["file:migrations/versions/0002_ledger.py"]
    assert resolved["eios.purge_mode"] == ["file:docs/adr/0005-ledger-append-only.md"]
    assert resolved["flush_interval_seconds"] == ["file:config/app.yaml"]


async def test_the_exact_retriever_and_the_coverage_check_agree(ledger: Ledger) -> None:
    for text in (
        "What does `uq_ledger_run_seq` do?",
        "Explain `eios.purge_mode` and `NoSuchThing_xyz`",
        "Where is `trg_ledger_no_update` and the `Ledger.append` method used",
    ):
        q = ledger.query(text)
        rc, _ = await ledger.container.governor._prepare(q)
        resolved = await resolve_entities(ledger.container.projects, rc)
        candidates = await ledger.container.governor._pipeline._retrievers["exact"].retrieve(rc, 10)
        ids = {c.id for c in candidates}
        for entity, targets in resolved.items():
            if targets:
                assert set(targets) & ids, (text, entity, targets, ids)


async def test_a_name_that_exists_nowhere_is_still_unresolved(ledger: Ledger) -> None:
    pack = await ledger.container.governor.build(
        ledger.query("What does `uq_no_such_constraint` enforce?", risk=Risk.MEDIUM),
        max_level=ContextLevel.L1,
    )
    gap = [m for m in pack.gap.missing_context if m.kind == "unresolved_entity"]
    assert gap and gap[0].blocking and gap[0].subject == "uq_no_such_constraint"
    assert not pack.ready_to_act


async def test_a_name_the_request_introduces_is_not_looked_up_in_text(ledger: Ledger) -> None:
    q = ledger.query("Add a new `flush_interval_seconds` column to the ledger table")
    rc, _ = await ledger.container.governor._prepare(q)
    assert next(e for e in rc.entities if e.text == "flush_interval_seconds").new
    assert (await resolve_entities(ledger.container.projects, rc))["flush_interval_seconds"] == []


async def test_a_word_in_prose_is_not_a_name_but_a_name_in_many_files_needs_a_hint(
    ledger: Ledger,
) -> None:
    # plain prose words never fall back to text, even though "retention" is all over the docs
    q = ledger.query("What is the retention policy of the Ledger?")
    rc, _ = await ledger.container.governor._prepare(q)
    resolved = await resolve_entities(ledger.container.projects, rc)
    assert all(not ids or ids[0].startswith("symbol:") for ids in resolved.values())

    # a snake_case name in more than 12 files is common vocabulary; only an explicit one resolves
    many: dict[str, str | bytes] = {
        f"docs/topic{n:02d}.md": f"# Topic {n}\n\nthe shared_vocabulary_term appears\n"
        for n in range(14)
    }
    write(ledger.repo, many)
    commit_all(ledger.repo, "docs")
    await ledger.sync()
    prose = ledger.query("Explain shared_vocabulary_term please")
    rc, _ = await ledger.container.governor._prepare(prose)
    assert (await resolve_entities(ledger.container.projects, rc))["shared_vocabulary_term"] == []
    explicit = ledger.query("Explain it", symbols=["shared_vocabulary_term"])
    rc, _ = await ledger.container.governor._prepare(explicit)
    found = (await resolve_entities(ledger.container.projects, rc))["shared_vocabulary_term"]
    assert 1 <= len(found) <= 3 and all(f.startswith("file:docs/topic") for f in found)


# -- coverage and the upgrade path -------------------------------------------------------------
async def test_coverage_reports_the_content_index(ledger: Ledger) -> None:
    cov = await ledger.container.projects.semantic_coverage(ledger.project_id)
    assert cov["content_chunks"] > 10
    assert 0 < cov["files_with_content"] <= cov["content_files"]
    assert cov["content_files"] == cov["files_with_content"] + 1  # .env is skipped, not indexed
    assert cov["content_coverage"] == cov["files_with_content"] / cov["content_files"]
    assert cov["files_with_knowledge"] == 0  # the knowledge-based numbers are unchanged


async def test_a_project_indexed_before_the_content_index_is_backfilled_by_the_next_sync(
    ledger: Ledger,
) -> None:
    async with ledger.engine.begin() as conn:  # what an upgraded database looks like
        await conn.execute(sa.delete(chunk_t))
        await conn.execute(
            sa.update(file_t).values(metadata=file_t.c.metadata.op("-")("chunk_version"))
        )
    assert (await ledger.container.projects.semantic_coverage(ledger.project_id))[
        "content_chunks"
    ] == 0
    result, _ = await ledger.container.projects.sync_now(ledger.project_id)
    assert result.changed == [] and result.reindexed > 5  # re-read, but not reported as changed
    assert result.chunks_total > 10
    assert "docs/adr/0005-ledger-append-only.md" in await ledger.paths(
        "why is the ledger append-only"
    )
    again, _ = await ledger.container.projects.sync_now(ledger.project_id)
    assert again.reindexed == 0  # once


async def test_overlay_scope_is_counted_apart_from_committed(ledger: Ledger) -> None:
    store = ledger.container.projects.store
    committed = await store.count_chunks(ledger.project_id, "main", FileScope.COMMITTED)
    assert (
        committed > 10
        and await store.count_chunks(ledger.project_id, "main", FileScope.OVERLAY) == 0
    )
    write(ledger.repo, {"docs/runbook.md": RUNBOOK + "\n5. Announce.\n"})
    await ledger.sync()
    assert await store.count_chunks(ledger.project_id, "main", FileScope.OVERLAY) >= 1
    assert await store.count_chunks(ledger.project_id, "main", FileScope.COMMITTED) == committed
