from __future__ import annotations

import uuid
from datetime import timedelta
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from eios_domain.errors import DomainError, NotFoundError
from eios_domain.ids import utcnow
from eios_domain.knowledge import (
    DecisionCreate,
    DecisionStatus,
    Health,
    HumanApproval,
    KnowledgeItem,
    KnowledgeItemCreate,
    MemoryCreate,
    ProvenanceInput,
    SourceKind,
    Trust,
)
from eios_domain.vault import Vault
from eios_knowledge import (
    DecisionRepository,
    EvidencePipeline,
    EvidenceRepository,
    HashingEmbedder,
    KnowledgeRepository,
    KnowledgeService,
    LocalBlobStore,
    MemoryRepository,
    SearchScope,
    purge_expired,
)
from eios_observability import PostgresEventStore, PostgresRunRepository, RunRecorder

pytestmark = pytest.mark.integration

A, B = uuid.uuid4(), uuid.uuid4()


def _service(db: AsyncEngine) -> KnowledgeService:
    return KnowledgeService(
        KnowledgeRepository(db), MemoryRepository(db), DecisionRepository(db), HashingEmbedder()
    )


def _manual(title: str, content: str, **kw: object) -> KnowledgeItemCreate:
    base: dict[str, object] = {
        "vault": Vault.DEFAULT,
        "title": title,
        "content": content,
        "source_kind": SourceKind.MANUAL,
        "created_by": "tester",
    }
    base.update(kw)
    return KnowledgeItemCreate(**base)  # type: ignore[arg-type]


async def test_add_and_get_with_provenance_and_embedding(db: AsyncEngine) -> None:
    svc = _service(db)
    created = await svc.add_item(
        _manual(
            "Retry policy",
            "Payments are retried three times with backoff.",
            source_kind=SourceKind.CODE,
            trust=Trust.DERIVED,
            provenance=[
                ProvenanceInput(source="src/pay.py", source_version="abc123", actor="parser")
            ],
        )
    )
    assert created.trust is Trust.DERIVED and created.health is Health.UNVERIFIED
    prov = await svc.knowledge.provenance(created.id)
    assert [(p["source"], p["source_version"]) for p in prov] == [("src/pay.py", "abc123")]
    async with db.connect() as conn:
        dims, model = (
            await conn.execute(
                sa.text(
                    "SELECT vector_dims(embedding), embedding_model FROM knowledge.item WHERE id=:i"
                ),
                {"i": created.id},
            )
        ).one()
    assert (dims, model) == (256, "hashing-v1")


async def test_fts_and_vector_search_rank_relevant_first(db: AsyncEngine) -> None:
    svc = _service(db)
    await svc.add_item(_manual("Payment retry", "failed payments are retried three times"))
    await svc.add_item(_manual("TLS rotation", "kubernetes ingress certificates rotate monthly"))
    await svc.add_item(_manual("Naming", "use snake case for python modules"))
    scope = SearchScope()
    text_hits = await svc.search_text("payments retried", scope)
    assert [i.title for i, _ in text_hits] == ["Payment retry"]
    vec_hits = await svc.search_vector("how are failed payments retried", scope, limit=3)
    assert vec_hits[0][0].title == "Payment retry"
    assert vec_hits[0][1] > vec_hits[-1][1]


async def test_project_vault_isolation_between_projects(db: AsyncEngine) -> None:
    svc = _service(db)
    await svc.add_item(_manual("shared", "billing algorithm overview"))
    await svc.add_item(
        _manual("a-secret", "billing algorithm of company A", vault=Vault.PROJECT, project_id=A)
    )
    await svc.add_item(
        _manual("b-secret", "billing algorithm of company B", vault=Vault.PROJECT, project_id=B)
    )

    def titles(hits: list[tuple[KnowledgeItem, float]]) -> set[str]:
        return {i.title for i, _ in hits}

    assert titles(await svc.search_text("billing algorithm", SearchScope(project_id=A))) == {
        "shared",
        "a-secret",
    }
    assert titles(await svc.search_text("billing algorithm", SearchScope(project_id=B))) == {
        "shared",
        "b-secret",
    }
    # no project in scope: no project data at all
    assert titles(await svc.search_text("billing algorithm", SearchScope())) == {"shared"}
    assert titles(await svc.search_vector("billing algorithm", SearchScope(project_id=A))) == {
        "shared",
        "a-secret",
    }
    only_default = SearchScope(project_id=A, vaults=frozenset({Vault.DEFAULT}))
    assert titles(await svc.search_text("billing algorithm", only_default)) == {"shared"}


async def test_health_and_expiry_filters(db: AsyncEngine) -> None:
    svc = _service(db)
    live = await svc.add_item(_manual("live", "gateway timeout settings"))
    quarantined = await svc.add_item(_manual("bad", "gateway timeout settings"))
    expired = await svc.add_item(
        _manual(
            "old",
            "gateway timeout settings",
            vault=Vault.EPHEMERAL,
            expires_at=utcnow() + timedelta(seconds=1),
        )
    )
    async with db.begin() as conn:
        await conn.execute(
            sa.text("UPDATE knowledge.item SET health='QUARANTINED' WHERE id=:i"),
            {"i": quarantined.id},
        )
        await conn.execute(
            sa.text(
                "UPDATE knowledge.item SET expires_at = now() - interval '1 minute' WHERE id=:i"
            ),
            {"i": expired.id},
        )
    hits = await svc.search_text("gateway timeout", SearchScope())
    assert [i.id for i, _ in hits] == [live.id]
    with_all = SearchScope(exclude_health=frozenset(), include_expired=True)
    assert len(await svc.search_text("gateway timeout", with_all)) == 3


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE knowledge.item SET vault='project', project_id=NULL",
        "UPDATE knowledge.item SET vault='default', project_id=gen_random_uuid()",
        "UPDATE knowledge.item SET vault='ephemeral', expires_at=NULL",
        "UPDATE knowledge.item SET vault='secret'",
        "UPDATE knowledge.item SET trust='GODLIKE'",
        "UPDATE knowledge.item SET confidence=1.5",
    ],
)
async def test_database_itself_rejects_inconsistent_vault_data(db: AsyncEngine, sql: str) -> None:
    """Defence in depth: even raw SQL (bypassing the domain layer) cannot misclassify data."""
    await _service(db).add_item(_manual("x", "y"))
    with pytest.raises(IntegrityError):
        async with db.begin() as conn:
            await conn.execute(sa.text(sql))


async def test_raw_evidence_rows_can_only_be_raw_trust(db: AsyncEngine, tmp_path: Path) -> None:
    pipeline = EvidencePipeline(EvidenceRepository(db), _service(db), LocalBlobStore(tmp_path))
    rec = await pipeline.ingest_raw(b"output", tool_id="t")
    with pytest.raises(IntegrityError):
        async with db.begin() as conn:
            await conn.execute(
                sa.text("UPDATE evidence.record SET trust='VERIFIED' WHERE id=:i"), {"i": rec.id}
            )


async def test_pipeline_end_to_end_and_provenance_chain(db: AsyncEngine, tmp_path: Path) -> None:
    runs, events = PostgresRunRepository(db), PostgresEventStore(db)
    recorder = RunRecorder(runs, events)
    ctx = await recorder.start_run(kind="t")
    svc = _service(db)
    pipeline = EvidencePipeline(EvidenceRepository(db), svc, LocalBlobStore(tmp_path))

    ev = await pipeline.ingest_raw(
        b"FATAL: connection pool exhausted at 12:01", tool_id="log-scan", summary="app log", ctx=ctx
    )
    assert ev.trust is Trust.RAW and ev.vault is Vault.EPHEMERAL and ev.expires_at is not None
    obs = await pipeline.observe(ev.id, "pool exhausted at 12:01", confidence=0.9, actor="log-scan")
    finding = await pipeline.conclude(
        [obs.id], "The pool is too small under load", confidence=0.8, actor="llm", ctx=ctx
    )
    item = await pipeline.promote_finding(
        finding.id,
        vault=Vault.PROJECT,
        project_id=A,
        title="Pool sizing",
        created_by="llm",
        source_kind=SourceKind.LLM,
        ctx=ctx,
    )
    assert item.trust is Trust.DERIVED and item.health is Health.UNVERIFIED
    prov = await svc.knowledge.provenance(item.id)
    assert prov[0]["evidence_id"] == ev.id
    assert prov[0]["evidence_hash"] == ev.content_hash

    types = [e.type for e in (await events.list_events(ctx.run_id)).items]
    wanted = {"EVIDENCE_CREATED", "FINDING_CREATED", "KNOWLEDGE_UPDATED"}
    assert [t for t in types if t in wanted] == [
        "EVIDENCE_CREATED",
        "FINDING_CREATED",
        "KNOWLEDGE_UPDATED",
    ]
