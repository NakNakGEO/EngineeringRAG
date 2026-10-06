"""Phase 12 exit criteria: fresh-machine restore from the portable package only; no local data."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from sqlalchemy.ext.asyncio import AsyncEngine

from eios_domain.events import ActorType
from eios_domain.ids import utcnow
from eios_domain.knowledge import (
    DecisionCreate,
    KnowledgeItemCreate,
    MemoryCreate,
    SourceKind,
    Trust,
)
from eios_domain.policy import PolicyRequest
from eios_domain.vault import Vault
from eios_knowledge import HashingEmbedder, KnowledgeRepository, LocalBlobStore
from eios_portability import (
    ImportRejectedError,
    PackageError,
    export_default_vault,
    import_package,
    open_package,
    read_header,
)
from eios_portability.package import seal
from eios_runtime import Container, build_container
from eios_storage import build_async_engine, metadata
from tests.conftest import alembic_config, make_settings, throwaway_database

pytestmark = pytest.mark.integration
SECRET = "ACME-INTERNAL-PRICING-ALGORITHM"
PASS = "correct horse battery staple"
REPO = Path(__file__).resolve().parents[2]
PROJECT = uuid.uuid4()
COUNT_SQL = {
    "project.project": "select count(*) from project.project",
    "platform.run": "select count(*) from platform.run",
    "policy.audit_log": "select count(*) from policy.audit_log",
    "platform.job": "select count(*) from platform.job",
}


@pytest.fixture
async def source(db: AsyncEngine, tmp_path: Path) -> tuple[Container, Path]:
    c = build_container(
        make_settings(
            manifests_dir=REPO / "manifests",
            blob_dir=tmp_path / "src-blobs",
            workspace_roots=str(tmp_path / "ws"),
        ),
        db,
    )
    await c.sync_registries()
    k = c.knowledge

    def item(
        title: str,
        content: str,
        vault: Vault,
        *,
        trust: Trust = Trust.RAW,
        project_id: uuid.UUID | None = None,
        expires_at: datetime | None = None,
    ) -> KnowledgeItemCreate:
        return KnowledgeItemCreate(
            vault=vault,
            title=title,
            content=content,
            source_kind=SourceKind.MANUAL,
            created_by="me",
            trust=trust,
            project_id=project_id,
            expires_at=expires_at,
        )

    await k.add_item(
        item(
            "retry pattern",
            "retry with exponential backoff and jitter",
            Vault.DEFAULT,
            trust=Trust.APPROVED,
        )
    )
    await k.add_item(item("acme pricing", SECRET, Vault.PROJECT, project_id=PROJECT))
    await k.add_item(
        item("scratch", SECRET, Vault.EPHEMERAL, expires_at=utcnow() + timedelta(days=1))
    )
    await k.add_memory(
        MemoryCreate(vault=Vault.DEFAULT, content="prefers small ADRs", created_by="u")
    )
    await k.add_memory(
        MemoryCreate(vault=Vault.PROJECT, project_id=PROJECT, content=SECRET, created_by="u")
    )
    await k.record_decision(
        DecisionCreate(vault=Vault.DEFAULT, question="use jitter?", selected="yes", decider="me")
    )
    await k.record_decision(
        DecisionCreate(vault=Vault.PROJECT, project_id=PROJECT, question=SECRET, decider="me")
    )
    await c.pipeline.ingest_raw(b"public research notes", vault=Vault.DEFAULT, summary="research")
    await c.pipeline.ingest_raw(SECRET.encode(), vault=Vault.PROJECT, project_id=PROJECT)
    await c.pipeline.ingest_raw(SECRET.encode(), tool_id="scan")
    # a generated skill that earned a place in the library
    from eios_workshop import ProposalKind

    skill = {
        "id": "portable-skill",
        "version": "1.0.0",
        "goal": "Draft release notes from commits.",
        "trigger": "release prep",
        "required_capabilities": ["git_inspect"],
        "procedure": [
            {"id": "log", "instruction": "Read the commits", "capability": "git_inspect"}
        ],
        "completion_criteria": ["notes written"],
    }
    p = await c.workshop.create(kind=ProposalKind.SKILL, spec=skill, creator="m")
    for step in (c.workshop.sandbox, c.workshop.test, c.workshop.register):
        p = await step(p.id, actor="t")
    return c, tmp_path


@pytest.fixture
async def fresh() -> AsyncEngine:  # type: ignore[misc]
    """A brand-new empty, migrated database standing in for a different machine."""
    with throwaway_database() as url:
        command.upgrade(alembic_config(url), "head")
        engine = build_async_engine(make_settings(database_url=url))
        try:
            yield engine
        finally:
            await engine.dispose()


async def all_text(engine: AsyncEngine) -> str:
    chunks: list[str] = []
    async with engine.connect() as conn:
        for t in metadata.sorted_tables:
            rows = (
                await conn.execute(
                    sa.select(*[c for c in t.c if c.name not in {"embedding", "search_vector"}])
                )
            ).all()
            chunks.append(json.dumps([[str(v) for v in r] for r in rows]))
    return "\n".join(chunks)


async def project_refs(engine: AsyncEngine) -> int:
    n = 0
    async with engine.connect() as conn:
        for t in metadata.sorted_tables:
            if "project_id" in t.c and t.name not in {"project", "file"}:
                n += (
                    await conn.execute(
                        sa.select(sa.func.count()).select_from(t).where(t.c.project_id.is_not(None))
                    )
                ).scalar_one()
    return n


async def test_fresh_machine_restore_from_the_package_alone(
    source: tuple[Container, Path], fresh: AsyncEngine
) -> None:
    c, tmp = source
    data = await export_default_vault(c.engine, PASS, blobs=c.blobs, registry=c.registry.store)
    assert SECRET.encode() not in data and SECRET not in json.dumps(open_package(data, PASS))
    header = read_header(data)
    assert header["counts"]["knowledge_items"] == 1 and header["counts"]["memory_items"] == 1

    blobs = LocalBlobStore(tmp / "new-machine-blobs")  # nothing of the old machine's disk
    new = build_container(
        make_settings(manifests_dir=REPO / "manifests", blob_dir=tmp / "new-machine-blobs"), fresh
    )
    await new.sync_registries()
    report = await import_package(data, PASS, fresh, blobs=blobs, registry=new.registry)
    assert report.inserted["knowledge_items"] == 1 and report.inserted["decisions"] == 1
    assert "skill:portable-skill@1.0.0" in report.registered

    # the knowledge is usable on the new machine
    embedder = HashingEmbedder()
    [vec] = await embedder.embed(["retry with exponential backoff"])
    from eios_knowledge.scope import SearchScope

    found = await KnowledgeRepository(fresh).search_vector(
        vec, SearchScope(vaults=frozenset({Vault.DEFAULT})), limit=3
    )
    assert found and found[0][0].title == "retry pattern"
    item = found[0][0]
    assert item.source_kind is SourceKind.IMPORT and item.trust is Trust.DERIVED  # re-earns trust
    assert item.vault is Vault.DEFAULT and item.project_id is None
    skill = await new.registry.store.get_skill("portable-skill")
    assert skill is not None and skill["state"] == "EXPERIMENTAL" and skill["origin"] == "plugin"
    # evidence content travelled with its blob and verifies
    rec = await new.pipeline._evidence.get_many(
        [uuid.UUID(r["id"]) for r in open_package(data, PASS)["bundle"]["evidence_records"]]
    )
    assert blobs.get(rec[0].content_hash) == b"public research notes"

    # PROOF: nothing project/company-local exists on the new machine
    text = await all_text(fresh)
    assert SECRET not in text
    assert await project_refs(fresh) == 0
    async with fresh.connect() as conn:
        counts = {
            t: (await conn.execute(sa.text(COUNT_SQL[t]))).scalar_one()
            for t in ("project.project", "platform.run", "policy.audit_log", "platform.job")
        }
    assert (
        counts["project.project"] == 0
        and counts["policy.audit_log"] == 0
        and counts["platform.job"] == 0
    )


async def test_import_is_idempotent_and_dry_run_writes_nothing(
    source: tuple[Container, Path], fresh: AsyncEngine
) -> None:
    c, tmp = source
    data = await export_default_vault(c.engine, PASS, blobs=c.blobs)
    blobs = LocalBlobStore(tmp / "b2")
    dry = await import_package(data, PASS, fresh, blobs=blobs, dry_run=True)
    assert dry.dry_run and (await all_text(fresh)).count("retry pattern") == 0
    first = await import_package(data, PASS, fresh, blobs=blobs)
    again = await import_package(data, PASS, fresh, blobs=blobs)
    assert first.inserted["knowledge_items"] == 1 and again.inserted["knowledge_items"] == 0
    assert again.skipped_existing["knowledge_items"] == 1


async def test_wrong_passphrase_tampering_and_garbage_are_rejected(
    source: tuple[Container, Path], fresh: AsyncEngine
) -> None:
    c, tmp = source
    data = await export_default_vault(c.engine, PASS)
    blobs = LocalBlobStore(tmp / "b3")
    with pytest.raises(PackageError, match="passphrase"):
        await import_package(data, "not the passphrase!!", fresh, blobs=blobs)
    flipped = bytearray(data)
    flipped[-5] ^= 1
    with pytest.raises(PackageError, match="modified"):
        await import_package(bytes(flipped), PASS, fresh, blobs=blobs)
    header = read_header(data)
    forged = data.replace(b'"knowledge_items":1', b'"knowledge_items":9')
    assert forged != data and header
    with pytest.raises(PackageError):
        await import_package(forged, PASS, fresh, blobs=blobs)  # header is authenticated
    for junk in (
        b"",
        b"EIOSPKG1",
        b"EIOSPKG1\x00\x00\x00\x05abcde",
        b"x" * 100,
        data[: len(data) // 2],
    ):
        with pytest.raises(PackageError):
            await import_package(junk, PASS, fresh, blobs=blobs)
    with pytest.raises(PackageError, match="at least"):
        await export_default_vault(c.engine, "short")
    assert (await all_text(fresh)).count("retry pattern") == 0  # nothing leaked in on failure


async def test_hostile_kdf_parameters_are_refused() -> None:
    payload = {
        "format": "eios-default-vault",
        "version": 1,
        "created_at": "x",
        "bundle": {},
        "blob_data": {},
    }
    data = seal(payload, PASS, {})
    h = read_header(data)
    evil = data.replace(b'"n":32768', b'"n":99999999')
    assert evil != data and h
    with pytest.raises(PackageError):
        open_package(evil, PASS)


async def test_a_package_carrying_project_data_is_rejected_whole(
    fresh: AsyncEngine, tmp_path: Path
) -> None:
    row = {
        "id": str(uuid.uuid4()),
        "vault": "project",
        "project_id": str(PROJECT),
        "title": "x",
        "content": SECRET,
    }
    for bundle in (
        {"knowledge_items": [row]},
        {"memory_items": [{**row, "vault": "default"}]},
        {
            "evidence_records": [
                {**row, "vault": "default", "project_id": None, "run_id": str(uuid.uuid4())}
            ]
        },
    ):
        payload = {
            "format": "eios-default-vault",
            "version": 1,
            "created_at": "x",
            "bundle": bundle,
            "blob_data": {},
        }
        with pytest.raises(ImportRejectedError):
            await import_package(
                seal(payload, PASS, {}), PASS, fresh, blobs=LocalBlobStore(tmp_path / "b")
            )
    assert SECRET not in await all_text(fresh)


async def test_audit_and_runs_and_secrets_never_travel(source: tuple[Container, Path]) -> None:
    c, _ = source
    await c.policy.evaluate(
        PolicyRequest(actor_type=ActorType.LLM, actor_id="x", action="db.read", target=SECRET)
    )
    data = open_package(await export_default_vault(c.engine, PASS), PASS)
    dumped = json.dumps(data)
    assert SECRET not in dumped and "audit" not in set(data["bundle"])
    assert set(data["bundle"]) == {
        "knowledge_items",
        "provenance",
        "memory_items",
        "decisions",
        "evidence_records",
        "blobs",
    }
