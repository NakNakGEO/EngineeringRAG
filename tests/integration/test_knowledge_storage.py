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


async def test_low_confidence_finding_only_reaches_observed(
    db: AsyncEngine, tmp_path: Path
) -> None:
    svc = _service(db)
    pipeline = EvidencePipeline(EvidenceRepository(db), svc, LocalBlobStore(tmp_path))
    ev = await pipeline.ingest_raw(b"x")
    obs = await pipeline.observe(ev.id, "maybe", confidence=0.3, actor="a")
    finding = await pipeline.conclude([obs.id], "possibly slow", confidence=0.4, actor="a")
    item = await pipeline.promote_finding(
        finding.id, vault=Vault.PROJECT, project_id=A, title="t", created_by="a"
    )
    assert item.trust is Trust.OBSERVED


async def test_pipeline_rejects_dangling_references(db: AsyncEngine, tmp_path: Path) -> None:
    pipeline = EvidencePipeline(EvidenceRepository(db), _service(db), LocalBlobStore(tmp_path))
    with pytest.raises(NotFoundError):
        await pipeline.observe(uuid.uuid4(), "s", confidence=0.5, actor="a")
    with pytest.raises(NotFoundError):
        await pipeline.conclude([uuid.uuid4()], "s", confidence=0.5, actor="a")
    with pytest.raises(NotFoundError):
        await pipeline.conclude([], "s", confidence=0.5, actor="a")
    with pytest.raises(NotFoundError):
        await pipeline.promote_finding(
            uuid.uuid4(), vault=Vault.PROJECT, project_id=A, title="t", created_by="a"
        )


async def test_evidence_size_limit(db: AsyncEngine, tmp_path: Path) -> None:
    pipeline = EvidencePipeline(
        EvidenceRepository(db), _service(db), LocalBlobStore(tmp_path), max_evidence_bytes=10
    )
    with pytest.raises(DomainError, match="limit"):
        await pipeline.ingest_raw(b"x" * 11)


async def test_project_evidence_cannot_enter_default_vault_without_human_approval(
    db: AsyncEngine, tmp_path: Path
) -> None:
    svc = _service(db)
    pipeline = EvidencePipeline(EvidenceRepository(db), svc, LocalBlobStore(tmp_path))
    ev = await pipeline.ingest_raw(b"company data", vault=Vault.PROJECT, project_id=A)
    obs = await pipeline.observe(ev.id, "company fact", confidence=0.9, actor="a")
    finding = await pipeline.conclude([obs.id], "company fact", confidence=0.9, actor="a")
    with pytest.raises(DomainError, match="human approval"):
        await pipeline.promote_finding(
            finding.id, vault=Vault.DEFAULT, project_id=None, title="t", created_by="a"
        )
    approval = HumanApproval(approver="alice", scope="vault-move", reason="generic pattern")
    item = await pipeline.promote_finding(
        finding.id,
        vault=Vault.DEFAULT,
        project_id=None,
        title="t",
        created_by="a",
        approval=approval,
    )
    assert item.vault is Vault.DEFAULT
    sources = [p["source"] for p in await svc.knowledge.provenance(item.id)]
    assert f"approval:{approval.approval_id}" in sources


async def test_evidence_from_another_project_cannot_be_promoted(
    db: AsyncEngine, tmp_path: Path
) -> None:
    pipeline = EvidencePipeline(EvidenceRepository(db), _service(db), LocalBlobStore(tmp_path))
    ev = await pipeline.ingest_raw(b"A data", vault=Vault.PROJECT, project_id=A)
    obs = await pipeline.observe(ev.id, "s", confidence=0.9, actor="a")
    finding = await pipeline.conclude([obs.id], "s", confidence=0.9, actor="a")
    with pytest.raises(DomainError, match="another project"):
        await pipeline.promote_finding(
            finding.id, vault=Vault.PROJECT, project_id=B, title="t", created_by="a"
        )


async def test_move_to_default_vault_requires_project_item_and_approval(
    db: AsyncEngine,
) -> None:
    svc = _service(db)
    item = await svc.add_item(_manual("p", "project fact", vault=Vault.PROJECT, project_id=A))
    approval = HumanApproval(approver="alice", scope="vault-move")
    moved = await svc.knowledge.move_to_default_vault(item.id, approval)
    assert moved.vault is Vault.DEFAULT and moved.project_id is None
    with pytest.raises(DomainError, match="only project vault"):
        await svc.knowledge.move_to_default_vault(item.id, approval)
    with pytest.raises(NotFoundError):
        await svc.knowledge.move_to_default_vault(uuid.uuid4(), approval)


async def test_memory_and_decision_ledger(db: AsyncEngine) -> None:
    svc = _service(db)
    mem = await svc.add_memory(
        MemoryCreate(vault=Vault.PROJECT, project_id=A, content="prefers small PRs", created_by="u")
    )
    hits = await svc.memory.search_text("small PRs", SearchScope(project_id=A))
    assert [m.id for m, _ in hits] == [mem.id]
    assert await svc.memory.search_text("small PRs", SearchScope(project_id=B)) == []

    d1 = await svc.record_decision(
        DecisionCreate(
            project_id=A,
            question="Which queue?",
            selected="Postgres",
            rationale="no new infra",
            decider="alice",
            alternatives=[{"name": "Redis"}],
        )
    )
    d2 = await svc.record_decision(
        DecisionCreate(
            project_id=A, question="Which queue? (revisited)", selected="NATS", decider="alice"
        )
    )
    accepted = await svc.decisions.set_status(d1.id, DecisionStatus.ACCEPTED)
    assert accepted.status is DecisionStatus.ACCEPTED
    superseded = await svc.decisions.supersede(d1.id, d2.id)
    assert superseded.status is DecisionStatus.SUPERSEDED and superseded.superseded_by == d2.id
    with pytest.raises(DomainError):
        await svc.decisions.set_status(d1.id, DecisionStatus.ACCEPTED)  # history is immutable
    with pytest.raises(DomainError):
        await svc.decisions.supersede(d2.id, d2.id)
    found = await svc.decisions.search_text("queue", SearchScope(project_id=A))
    assert {d.id for d, _ in found} == {d1.id, d2.id}
    assert await svc.decisions.search_text("queue", SearchScope()) == []


async def test_purge_expired_removes_ephemeral_data_and_orphan_blobs(
    db: AsyncEngine, tmp_path: Path
) -> None:
    blobs = LocalBlobStore(tmp_path)
    svc = _service(db)
    pipeline = EvidencePipeline(EvidenceRepository(db), svc, blobs)
    short = await pipeline.ingest_raw(b"temporary", ttl=timedelta(seconds=60))
    keep = await pipeline.ingest_raw(b"keep me", vault=Vault.DEFAULT)
    eph_item = await svc.add_item(
        _manual(
            "tmp", "scratch", vault=Vault.EPHEMERAL, expires_at=utcnow() + timedelta(seconds=60)
        )
    )
    durable = await svc.add_item(_manual("durable", "stays"))
    await svc.add_memory(
        MemoryCreate(
            vault=Vault.EPHEMERAL,
            content="scratch",
            created_by="u",
            expires_at=utcnow() + timedelta(seconds=60),
        )
    )

    nothing = await purge_expired(db, blobs)
    assert (nothing.knowledge_items, nothing.evidence_records, nothing.blobs) == (0, 0, 0)

    result = await purge_expired(db, blobs, now=utcnow() + timedelta(hours=1))
    assert (result.knowledge_items, result.memory_items) == (1, 1)
    assert (result.evidence_records, result.blobs) == (1, 1)
    assert await svc.knowledge.get(eph_item.id) is None
    assert await svc.knowledge.get(durable.id) is not None
    evidence = EvidenceRepository(db)
    assert await evidence.get(short.id) is None and await evidence.get(keep.id) is not None
    assert not blobs.exists(short.content_hash) and blobs.exists(keep.content_hash)


async def test_shared_blob_is_kept_while_referenced(db: AsyncEngine, tmp_path: Path) -> None:
    blobs = LocalBlobStore(tmp_path)
    pipeline = EvidencePipeline(EvidenceRepository(db), _service(db), blobs)
    a = await pipeline.ingest_raw(b"same bytes", ttl=timedelta(seconds=60))
    b = await pipeline.ingest_raw(b"same bytes", vault=Vault.DEFAULT)
    assert a.content_hash == b.content_hash and a.blob_id == b.blob_id
    result = await purge_expired(db, blobs, now=utcnow() + timedelta(hours=1))
    assert result.evidence_records == 1 and result.blobs == 0 and blobs.exists(b.content_hash)
