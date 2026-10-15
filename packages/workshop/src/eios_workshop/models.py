"""Gap resolution and workshop models."""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class GapStatus(StrEnum):
    RESOLVED = "RESOLVED"
    COMPOSABLE = "COMPOSABLE"
    GENERATABLE = "GENERATABLE"
    EXTERNAL_REQUIRED = "EXTERNAL_REQUIRED"
    IMPOSSIBLE = "IMPOSSIBLE"
    BLOCKED_BY_POLICY = "BLOCKED_BY_POLICY"


class ProposalKind(StrEnum):
    SKILL = "skill"
    AGENT = "agent"
    TOOL = "tool"


class ProposalState(StrEnum):
    DRAFT = "DRAFT"
    SANDBOXED = "SANDBOXED"
    TESTED = "TESTED"
    EXPERIMENTAL = "EXPERIMENTAL"
    VERIFIED = "VERIFIED"
    TRUSTED = "TRUSTED"
    REJECTED = "REJECTED"


class GapRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    capability: str | None = Field(default=None, max_length=100)
    description: str = Field(default="", max_length=4000)
    kind_hint: Literal["auto", "procedure", "reasoning_role", "executable", "external_service"] = (
        "auto"
    )
    components: list[str] = Field(default_factory=list, max_length=20)
    requires: list[str] = Field(default_factory=list, max_length=20)


class ResolutionStep(BaseModel):
    stage: str
    outcome: str
    detail: str = ""


class GapResolution(BaseModel):
    status: GapStatus
    capability: str | None = None
    message: str = ""
    steps: list[ResolutionStep] = Field(default_factory=list)
    provider: dict[str, str] | None = None
    plan: list[str] = Field(default_factory=list)
    artifact_kind: ProposalKind | None = None
    needs_human: bool = False


class Proposal(BaseModel):
    id: uuid.UUID
    kind: ProposalKind
    name: str
    version: str
    state: ProposalState
    spec: dict[str, Any]
    spec_hash: str
    source: str | None
    source_sha256: str | None
    creator: str
    creator_kind: str
    prompt_hash: str | None
    need: dict[str, Any]
    capability_claims: list[str]
    tests: list[dict[str, Any]]
    test_results: dict[str, Any] | None
    scan_report: dict[str, Any] | None
    approval: dict[str, Any] | None
    registered_ref: str | None
    artifact_path: str | None
    created_at: datetime
    updated_at: datetime
