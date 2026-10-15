"""Phase 2 exit criterion: project data is excluded from portable export."""

from __future__ import annotations

import json
import uuid
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from eios_domain.ids import utcnow
from eios_domain.knowledge import (
    DecisionCreate,
    KnowledgeItemCreate,
    MemoryCreate,
    ProvenanceInput,
    SourceKind,
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
)
from eios_portability import ExportBundle, ExportLeakError, assert_clean, collect_default_vault

pytestmark = pytest.mark.integration

PROJECT = uuid.uuid4()
COMPANY_SECRET = "ACME-INTERNAL-PRICING-ALGORITHM"


async def test_export_contains_only_default_vault_data(db: AsyncEngine, tmp_path: Path) -> None:
    svc = KnowledgeService(
        KnowledgeRepository(db), MemoryRepository(db), DecisionRepository(db), HashingEmbedder()
    )
    pipeline = EvidencePipeline(EvidenceRepository(db), svc, LocalBlobStore(tmp_path))

    def item(title: str, content: str, vault: Vault, **kw: object) -> KnowledgeItemCreate:
        return KnowledgeItemCreate(
            vault=vault,
            title=title,
            content=content,
            source_kind=SourceKind.MANUAL,
            created_by="me",
            **kw,  # type: ignore[arg-type]
        )

    await svc.add_item(item("generic pattern", "retry with jitter", Vault.DEFAULT))
    await svc.add_item(item("acme pricing", COMPANY_SECRET, Vault.PROJECT, project_id=PROJECT))
    await svc.add_item(
        item("scratch", COMPANY_SECRET, Vault.EPHEMERAL, expires_at=utcnow() + timedelta(days=1))
    )
    await svc.add_memory(MemoryCreate(vault=Vault.DEFAULT, content="likes ADRs", created_by="u"))
    await svc.add_memory(
        MemoryCreate(
            vault=Vault.PROJECT, project_id=PROJECT, content=COMPANY_SECRET, created_by="u"
        )
    )
    await svc.record_decision(
        DecisionCreate(vault=Vault.DEFAULT, question="generic?", decider="me")
    )
    await svc.record_decision(
        DecisionCreate(
            vault=Vault.PROJECT, project_id=PROJECT, question=COMPANY_SECRET, decider="me"
        )
    )
    await pipeline.ingest_raw(b"public research", vault=Vault.DEFAULT)
    project_ev = await pipeline.ingest_raw(
        COMPANY_SECRET.encode(), vault=Vault.PROJECT, project_id=PROJECT
    )
    await pipeline.ingest_raw(COMPANY_SECRET.encode(), tool_id="scan")  # ephemeral

    # A default-vault item whose provenance points at project evidence (e.g. after approval)
    shared = await svc.add_item(
        item("shared", "approved generic insight", Vault.DEFAULT).model_copy(
            update={
                "provenance": [
                    ProvenanceInput(
                        source="evidence",
                        actor="a",
                        evidence_id=project_ev.id,
                        evidence_hash=project_ev.content_hash,
                    )
                ]
            }
        )
    )

    bundle = await collect_default_vault(db)
    dumped = json.dumps(bundle.to_dict())

    assert bundle.counts() == {
        "knowledge_items": 2, "provenance": 1, "memory_items": 1,
        "decisions": 1, "evidence_records": 1, "blobs": 1,
    }  # fmt: skip
    assert COMPANY_SECRET not in dumped
    assert str(PROJECT) not in dumped
    assert project_ev.content_hash not in dumped  # no hash of company evidence either
    assert str(project_ev.id) not in dumped
    assert '"embedding"' not in dumped and '"search_vector"' not in dumped
    [prov] = bundle.provenance
    assert prov["item_id"] == str(shared.id) and prov["evidence_id"] is None
    assert_clean(bundle)


async def test_empty_vaults_export_cleanly(db: AsyncEngine) -> None:
    bundle = await collect_default_vault(db)
    assert all(count == 0 for count in bundle.counts().values())


def test_assert_clean_rejects_any_leak() -> None:
    for field_name in ("knowledge_items", "memory_items", "decisions", "evidence_records"):
        for leaked in (
            {"vault": "project", "project_id": None},
            {"vault": "default", "project_id": str(PROJECT)},
            {"vault": "ephemeral"},
        ):
            bundle = ExportBundle()
            getattr(bundle, field_name).append(leaked)
            with pytest.raises(ExportLeakError):
                assert_clean(bundle)
