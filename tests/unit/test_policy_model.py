from __future__ import annotations

import pytest

from eios_domain.events import ActorType
from eios_domain.policy import PolicyDecision, PolicyEffect, PolicyRequest


def _req() -> PolicyRequest:
    return PolicyRequest(actor_type=ActorType.AGENT, actor_id="a", action="capability.invoke")


def test_allow_and_deny_semantics() -> None:
    allow = PolicyDecision(effect=PolicyEffect.ALLOW, rule_id="r1", request=_req())
    deny = PolicyDecision(effect=PolicyEffect.DENY, rule_id="root.x", request=_req())
    approval = PolicyDecision(effect=PolicyEffect.REQUIRE_APPROVAL, rule_id="r2", request=_req())
    assert allow.allowed and not deny.allowed and not approval.allowed


def test_decisions_are_immutable() -> None:
    d = PolicyDecision(effect=PolicyEffect.DENY, rule_id="r", request=_req())
    with pytest.raises(Exception, match="frozen"):
        d.effect = PolicyEffect.ALLOW
