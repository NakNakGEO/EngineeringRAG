"""Policy decision model: produced by the Policy Engine (Phase 6), consumed everywhere."""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from eios_domain.events import ActorType
from eios_domain.ids import new_id, utcnow


class PolicyEffect(StrEnum):
    ALLOW = "allow"
    DENY = "deny"
    REQUIRE_APPROVAL = "require_approval"


class Risk(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class PolicyRequest(BaseModel):
    """A question put to the Policy Engine: may this actor do this?"""

    model_config = ConfigDict(frozen=True)

    actor_type: ActorType
    actor_id: str
    action: str = Field(description="e.g. capability.invoke, fs.read, fs.write, net.connect")
    capability: str | None = None
    target: str | None = None
    run_id: uuid.UUID | None = None
    attributes: dict[str, Any] = Field(default_factory=dict)


class PolicyDecision(BaseModel):
    """The outcome. A deny is final; REQUIRE_APPROVAL means a human must decide."""

    model_config = ConfigDict(frozen=True)

    decision_id: uuid.UUID = Field(default_factory=new_id)
    effect: PolicyEffect
    rule_id: str
    reasons: list[str] = Field(default_factory=list)
    request: PolicyRequest
    risk: Risk = Risk.LOW
    root_policy_version: str | None = None
    decided_at: datetime = Field(default_factory=utcnow)

    @property
    def allowed(self) -> bool:
        return self.effect is PolicyEffect.ALLOW
