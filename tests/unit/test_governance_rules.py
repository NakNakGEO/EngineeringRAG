from __future__ import annotations

import pytest

from eios_domain.governance import EvidenceFact, check_health_change, check_trust_change
from eios_domain.knowledge import Health, HumanApproval, SourceKind, Trust, TrustViolationError
from eios_governance.evaluation import check_expectation

HUMAN = HumanApproval(approver="human:alice", scope="test")
TOOL = EvidenceFact(SourceKind.TOOL, in_scope=True)
CODE = EvidenceFact(SourceKind.CODE, in_scope=True)
LLM = EvidenceFact(SourceKind.LLM, in_scope=True)
FOREIGN_TOOL = EvidenceFact(SourceKind.TOOL, in_scope=False)


def test_an_assertion_cannot_verify_itself() -> None:
    for evidence in ([], [LLM], [LLM, LLM], [FOREIGN_TOOL]):
        with pytest.raises(TrustViolationError, match="VERIFIED"):
            check_trust_change(Trust.DERIVED, Trust.VERIFIED, evidence=evidence)
    check_trust_change(Trust.DERIVED, Trust.VERIFIED, evidence=[TOOL])
    check_trust_change(Trust.DERIVED, Trust.VERIFIED, evidence=[CODE])
    check_trust_change(Trust.DERIVED, Trust.VERIFIED, human=HUMAN)
    check_trust_change(Trust.RAW, Trust.VERIFIED, evidence=[LLM, TOOL])


def test_approved_is_human_only_and_demotion_is_always_allowed() -> None:
    with pytest.raises(TrustViolationError):
        check_trust_change(Trust.VERIFIED, Trust.APPROVED, evidence=[TOOL, CODE])
    check_trust_change(Trust.VERIFIED, Trust.APPROVED, human=HUMAN)
    check_trust_change(Trust.APPROVED, Trust.RAW)
    check_trust_change(Trust.VERIFIED, Trust.VERIFIED)


def test_lower_levels_need_real_evidence() -> None:
    with pytest.raises(TrustViolationError):
        check_trust_change(Trust.RAW, Trust.DERIVED, evidence=[LLM])
    with pytest.raises(TrustViolationError):
        check_trust_change(Trust.RAW, Trust.OBSERVED)
    check_trust_change(Trust.RAW, Trust.OBSERVED, evidence=[LLM])
    check_trust_change(Trust.OBSERVED, Trust.DERIVED, evidence=[TOOL])


def test_health_rules() -> None:
    with pytest.raises(TrustViolationError, match="successor"):
        check_health_change(Health.CURRENT, Health.SUPERSEDED, Trust.DERIVED, reason="x")
    with pytest.raises(TrustViolationError, match="quarantine needs"):
        check_health_change(Health.CURRENT, Health.QUARANTINED, Trust.DERIVED, reason=" ")
    with pytest.raises(TrustViolationError, match="human"):
        check_health_change(Health.QUARANTINED, Health.CURRENT, Trust.DERIVED, reason="ok")
    check_health_change(
        Health.QUARANTINED, Health.UNVERIFIED, Trust.DERIVED, reason="ok", human=HUMAN
    )
    with pytest.raises(TrustViolationError, match="RAW"):
        check_health_change(Health.UNVERIFIED, Health.CURRENT, Trust.RAW, reason="x")
    with pytest.raises(TrustViolationError, match="contradiction"):
        check_health_change(Health.CONTRADICTED, Health.CURRENT, Trust.DERIVED, reason="x")
    with pytest.raises(TrustViolationError, match="historical"):
        check_health_change(Health.HISTORICAL, Health.CURRENT, Trust.DERIVED, reason="x")
    with pytest.raises(TrustViolationError, match="needs a reason"):
        check_health_change(Health.CURRENT, Health.STALE, Trust.DERIVED, reason="")
    check_health_change(Health.CURRENT, Health.STALE, Trust.DERIVED, reason="file changed")


def test_expectation_operators() -> None:
    out = {"a": {"b": [1, 2, 3]}, "n": 5, "s": "hello world", "z": None}
    assert check_expectation(out, {"a.b.0": 1, "n": {"gte": 5}, "s": {"contains": "world"}})[0]
    assert check_expectation(out, {"a.b": {"length": 3}, "z": None, "n": {"in": [4, 5]}})[0]
    assert not check_expectation(out, {"n": {"lte": 4}})[0]
    assert not check_expectation(out, {"missing": 1})[0]
    assert not check_expectation(out, {"n": {"nonsense": 1}})[0]
    assert not check_expectation(out, {"n": {"contains": "x"}})[0]  # type mismatch is a failure
