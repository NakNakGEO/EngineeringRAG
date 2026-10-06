"""Knowledge and evidence endpoints.

Callers of this API are LLM clients, which are untrusted for provenance: whatever they submit is
recorded as ``llm`` source (trust ceiling OBSERVED) and cannot claim human origin or verification.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, Query, Response, status
from pydantic import BaseModel, Field

from eios_api.deps import ContainerDep
from eios_domain.knowledge import (
    EvidenceRecord,
    Health,
    KnowledgeItem,
    KnowledgeItemCreate,
    ProvenanceInput,
    SourceKind,
    Trust,
)
from eios_domain.vault import Vault
from eios_knowledge import SearchScope
from eios_knowledge.blob_store import BlobError

router = APIRouter(tags=["knowledge"])
MAX_CONTENT_RESPONSE_BYTES = 10_000_000


class CreateKnowledgeRequest(BaseModel):
    vault: Vault = Vault.PROJECT
    project_id: uuid.UUID | None = None
    kind: str = "note"
    title: str = Field(min_length=1, max_length=300)
    content: str = Field(min_length=1, max_length=200_000)
    tags: list[str] = Field(default_factory=list, max_length=50)
    trust: Literal["RAW", "OBSERVED"] = "RAW"
    confidence: float = Field(default=0.5, ge=0, le=1)
    created_by: str = Field(min_length=1, max_length=200)
    provenance: list[ProvenanceInput] = Field(min_length=1)
    subject_key: str | None = None
    limitations: str = ""
    version_ref: str | None = None
    ttl_seconds: int | None = Field(default=None, ge=60, description="required for ephemeral")


class KnowledgeDetail(BaseModel):
    item: KnowledgeItem
    provenance: list[dict[str, Any]]


class SearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=2000)
    project_id: uuid.UUID | None = None
    mode: Literal["text", "vector"] = "text"
    limit: int = Field(default=10, ge=1, le=100)
    include_ephemeral: bool = True


class SearchHit(BaseModel):
    item: KnowledgeItem
    score: float


@router.post("/knowledge", status_code=status.HTTP_201_CREATED, response_model=KnowledgeItem)
async def create_knowledge(body: CreateKnowledgeRequest, container: ContainerDep) -> KnowledgeItem:
    from datetime import timedelta

    from eios_domain.ids import utcnow

    expires_at = utcnow() + timedelta(seconds=body.ttl_seconds) if body.ttl_seconds else None
    if body.vault is Vault.EPHEMERAL and expires_at is None:
        expires_at = utcnow() + timedelta(seconds=container.settings.ephemeral_ttl_seconds)
    try:
        create = KnowledgeItemCreate(
            vault=body.vault,
            project_id=body.project_id,
            kind=body.kind,
            title=body.title,
            content=body.content,
            tags=body.tags,
            trust=Trust(body.trust),
            health=Health.UNVERIFIED,
            confidence=body.confidence,
            source_kind=SourceKind.LLM,  # never trust a client's claim of human origin
            created_by=body.created_by,
            expires_at=expires_at,
            subject_key=body.subject_key,
            limitations=body.limitations,
            version_ref=body.version_ref,
            provenance=body.provenance,
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return await container.knowledge.add_item(create)


@router.get("/knowledge/{item_id}", response_model=KnowledgeDetail)
async def get_knowledge(item_id: uuid.UUID, container: ContainerDep) -> KnowledgeDetail:
    item = await container.knowledge.knowledge.get(item_id)
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "knowledge item not found")
    provenance = await container.knowledge.knowledge.provenance(item_id)
    return KnowledgeDetail(item=item, provenance=provenance)


@router.post("/knowledge/search", response_model=list[SearchHit])
async def search_knowledge(body: SearchRequest, container: ContainerDep) -> list[SearchHit]:
    vaults = {Vault.DEFAULT, Vault.PROJECT}
    if body.include_ephemeral:
        vaults.add(Vault.EPHEMERAL)
    scope = SearchScope(project_id=body.project_id, vaults=frozenset(vaults))
    search = (
        container.knowledge.search_text
        if body.mode == "text"
        else container.knowledge.search_vector
    )
    hits = await search(body.query, scope, limit=body.limit)
    return [SearchHit(item=i, score=s) for i, s in hits]


@router.get("/evidence/{evidence_id}", response_model=EvidenceRecord)
async def get_evidence(evidence_id: uuid.UUID, container: ContainerDep) -> EvidenceRecord:
    record = await container.evidence.get(evidence_id)
    if record is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "evidence not found")
    return record


@router.get("/evidence/{evidence_id}/content")
async def get_evidence_content(
    evidence_id: uuid.UUID,
    container: ContainerDep,
    max_bytes: Annotated[int, Query(ge=1, le=MAX_CONTENT_RESPONSE_BYTES)] = 1_000_000,
) -> Response:
    record = await container.evidence.get(evidence_id)
    if record is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "evidence not found")
    try:
        data = container.blobs.get(record.content_hash)
    except BlobError as exc:
        raise HTTPException(
            status.HTTP_410_GONE, "evidence content is no longer available"
        ) from exc
    headers = {"X-Content-Truncated": "true"} if len(data) > max_bytes else {}
    return Response(data[:max_bytes], media_type="application/octet-stream", headers=headers)
