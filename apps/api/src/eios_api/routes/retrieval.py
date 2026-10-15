"""Retrieval, context building (Context Governor) and impact analysis."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, Field

from eios_api.deps import ContainerDep
from eios_domain.errors import NotFoundError
from eios_retrieval import ContextLevel, ContextPack, RetrievalQuery
from eios_retrieval.impact import ImpactReport
from eios_retrieval.models import ContextItem

router = APIRouter(tags=["retrieval"])


class SearchRequest(RetrievalQuery):
    level: ContextLevel = ContextLevel.L1
    limit: int = Field(default=20, ge=1, le=200)


class SearchResponse(BaseModel):
    items: list[ContextItem]
    produced: dict[str, int]
    redundant_ids: list[str]
    errors: list[str]


class ContextBuildRequest(RetrievalQuery):
    token_budget: int = Field(default=12_000, ge=500, le=500_000)
    min_level: ContextLevel | None = None
    max_level: ContextLevel = ContextLevel.L4
    record_run: bool = True


class ContextBuildResponse(BaseModel):
    pack: ContextPack
    run_id: uuid.UUID | None = None


def _query(body: RetrievalQuery) -> RetrievalQuery:
    return RetrievalQuery.model_validate(body.model_dump(include=set(RetrievalQuery.model_fields)))


@router.post("/retrieval/search", response_model=SearchResponse)
async def search(body: SearchRequest, container: ContainerDep) -> SearchResponse:
    """One retrieval pass: scope, retrieve, fuse, graph-expand, dedupe, rerank (no expansion)."""
    try:
        result = await container.governor.search(_query(body), level=body.level, limit=body.limit)
    except NotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    return SearchResponse(
        items=result.items[: body.limit],
        produced=result.produced,
        redundant_ids=result.redundant_ids,
        errors=result.errors,
    )


@router.post("/context/build", response_model=ContextBuildResponse)
async def build_context(body: ContextBuildRequest, container: ContainerDep) -> ContextBuildResponse:
    """Build a context pack. Expands automatically while coverage is inadequate and reports
    exactly what is still missing (``gap``) instead of claiming confidence it does not have."""
    query = _query(body)
    if body.min_level and body.min_level.rank > body.max_level.rank:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "min_level exceeds max_level")
    try:
        if body.record_run:
            async with container.recorder.run(
                kind="context_build", goal=query.text[:500], project_id=query.project_id
            ) as ctx:
                pack = await container.governor.build(
                    query,
                    token_budget=body.token_budget,
                    min_level=body.min_level,
                    max_level=body.max_level,
                    ctx=ctx,
                )
            return ContextBuildResponse(pack=pack, run_id=ctx.run_id)
        pack = await container.governor.build(
            query,
            token_budget=body.token_budget,
            min_level=body.min_level,
            max_level=body.max_level,
        )
    except NotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    return ContextBuildResponse(pack=pack)


@router.get("/projects/{project_id}/impact", response_model=ImpactReport)
async def project_impact(
    project_id: uuid.UUID,
    container: ContainerDep,
    path: Annotated[list[str] | None, Query(max_length=50)] = None,
    symbol: Annotated[list[str] | None, Query(max_length=50)] = None,
    branch: str | None = None,
    depth: Annotated[int, Query(ge=1, le=5)] = 3,
    include_overlay: bool = True,
) -> ImpactReport:
    if not path and not symbol:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "give at least one path or symbol"
        )
    try:
        return await container.impact.analyze(
            project_id,
            paths=path,
            symbols=symbol,
            branch=branch,
            depth=depth,
            include_overlay=include_overlay,
        )
    except NotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
