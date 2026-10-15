"""The universal evidence pipeline: RAW RESULT -> EVIDENCE -> OBSERVATION -> FINDING -> KNOWLEDGE.

External tool output never writes directly to trusted knowledge. Evidence is always RAW; knowledge
produced from it can reach at most DERIVED here. Higher trust needs verification (Phase 9).
"""

from __future__ import annotations

import uuid
from datetime import timedelta

from eios_domain.errors import DomainError, NotFoundError
from eios_domain.events import EventType
from eios_domain.ids import utcnow
from eios_domain.knowledge import (
    EvidenceCreate,
    EvidenceRecord,
    Finding,
    Health,
    HumanApproval,
    KnowledgeItem,
    KnowledgeItemCreate,
    Observation,
    ProvenanceInput,
    SourceKind,
    Trust,
)
from eios_domain.vault import Vault
from eios_knowledge.blob_store import BlobStore
from eios_knowledge.evidence_repo import EvidenceRepository
from eios_knowledge.service import KnowledgeService
from eios_observability import RunContext

DERIVED_MIN_CONFIDENCE = 0.7


class EvidencePipeline:
    def __init__(
        self,
        evidence: EvidenceRepository,
        knowledge: KnowledgeService,
        blobs: BlobStore,
        *,
        max_evidence_bytes: int = 50_000_000,
        default_ttl: timedelta = timedelta(days=7),
    ) -> None:
        self._evidence = evidence
        self._knowledge = knowledge
        self._blobs = blobs
        self._max_bytes = max_evidence_bytes
        self._default_ttl = default_ttl

    async def ingest_raw(
        self,
        data: bytes,
        *,
        vault: Vault = Vault.EPHEMERAL,
        project_id: uuid.UUID | None = None,
        run_id: uuid.UUID | None = None,
        source_kind: SourceKind = SourceKind.TOOL,
        tool_id: str | None = None,
        summary: str = "",
        media_type: str = "text/plain",
        ttl: timedelta | None = None,
        ctx: RunContext | None = None,
    ) -> EvidenceRecord:
        """Store a raw result as RAW evidence (ephemeral by default, with a TTL)."""
        if len(data) > self._max_bytes:
            raise DomainError(f"evidence exceeds the {self._max_bytes} byte limit")
        expires_at = utcnow() + (ttl or self._default_ttl) if vault is Vault.EPHEMERAL else None
        create = EvidenceCreate(
            vault=vault,
            project_id=project_id,
            run_id=run_id or (ctx.run_id if ctx else None),
            source_kind=source_kind,
            tool_id=tool_id,
            summary=summary,
            media_type=media_type,
            expires_at=expires_at,
        )
        blob = self._blobs.put(data)
        record = await self._evidence.create_record(create, blob)
        if ctx is not None:
            await ctx.emit(
                EventType.EVIDENCE_CREATED,
                summary or f"evidence from {tool_id or source_kind.value}",
                data={
                    "evidence_id": str(record.id),
                    "content_hash": record.content_hash,
                    "size_bytes": record.size_bytes,
                    "vault": record.vault.value,
                    "trust": record.trust.value,
                },
            )
        return record

    async def observe(
        self,
        evidence_id: uuid.UUID,
        statement: str,
        *,
        confidence: float,
        actor: str,
    ) -> Observation:
        if await self._evidence.get(evidence_id) is None:
            raise NotFoundError(f"evidence {evidence_id} not found")
        return await self._evidence.add_observation(evidence_id, statement, confidence, actor)

    async def conclude(
        self,
        observation_ids: list[uuid.UUID],
        statement: str,
        *,
        confidence: float,
        limitations: str = "",
        actor: str,
        ctx: RunContext | None = None,
    ) -> Finding:
        found = await self._evidence.get_observations(observation_ids)
        if not observation_ids or len(found) != len(set(observation_ids)):
            raise NotFoundError("a finding must reference existing observations")
        finding = await self._evidence.add_finding(
            list(observation_ids), statement, confidence, limitations, actor
        )
        if ctx is not None:
            await ctx.emit(
                EventType.FINDING_CREATED,
                statement[:200],
                data={"finding_id": str(finding.id), "confidence": confidence},
            )
        return finding

    async def promote_finding(
        self,
        finding_id: uuid.UUID,
        *,
        vault: Vault,
        project_id: uuid.UUID | None,
        title: str,
        created_by: str,
        source_kind: SourceKind = SourceKind.TOOL,
        kind: str = "finding",
        tags: list[str] | None = None,
        subject_key: str | None = None,
        version_ref: str | None = None,
        approval: HumanApproval | None = None,
        ctx: RunContext | None = None,
    ) -> KnowledgeItem:
        """Turn a finding into knowledge (trust <= DERIVED, health UNVERIFIED, full provenance).

        Knowledge built from Project Vault evidence stays in that project's vault: placing it in
        the Default Vault (which is portable) needs an explicit :class:`HumanApproval`.
        """
        finding = await self._evidence.get_finding(finding_id)
        if finding is None:
            raise NotFoundError(f"finding {finding_id} not found")
        observations = await self._evidence.get_observations(finding.observation_ids)
        records = await self._evidence.get_many([o.evidence_id for o in observations])
        if (
            vault is Vault.DEFAULT
            and approval is None
            and any(r.vault is Vault.PROJECT for r in records)
        ):
            raise DomainError(
                "knowledge derived from project vault evidence cannot enter the default vault "
                "without explicit human approval"
            )
        if any(r.project_id not in (None, project_id) for r in records) and project_id is not None:
            raise DomainError("evidence from another project cannot be promoted into this project")

        trust = Trust.DERIVED if finding.confidence >= DERIVED_MIN_CONFIDENCE else Trust.OBSERVED
        by_id = {r.id: r for r in records}
        provenance = [
            ProvenanceInput(
                source=f"evidence:{o.evidence_id}",
                source_version=version_ref,
                actor=created_by,
                evidence_id=o.evidence_id,
                evidence_hash=by_id[o.evidence_id].content_hash if o.evidence_id in by_id else None,
                notes=f"observation {o.id}: {o.statement[:200]}",
            )
            for o in observations
        ]
        if approval is not None:
            provenance.append(
                ProvenanceInput(
                    source=f"approval:{approval.approval_id}",
                    actor=approval.approver,
                    notes=approval.reason,
                )
            )
        item = await self._knowledge.add_item(
            KnowledgeItemCreate(
                vault=vault,
                project_id=project_id,
                kind=kind,
                title=title,
                content=finding.statement,
                tags=tags or [],
                trust=trust,
                health=Health.UNVERIFIED,
                confidence=finding.confidence,
                source_kind=source_kind,
                created_by=created_by,
                subject_key=subject_key,
                limitations=finding.limitations,
                version_ref=version_ref,
                provenance=provenance,
                from_pipeline=True,
                expires_at=utcnow() + timedelta(days=7) if vault is Vault.EPHEMERAL else None,
            ),
            ctx,
        )
        return item
