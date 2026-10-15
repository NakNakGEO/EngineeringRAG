"""Invariants of the shipped workflow definitions: one writer, independent verification.

The engine enforces ``NodeDef.roles`` (see tests/integration/test_workflow_engine.py); these
tests make sure the definitions that ship with the product cannot quietly lose the rule.
"""

from __future__ import annotations

from pathlib import Path

from eios_domain.workflow import REVIEWER_ROLE, WRITER_ROLE, Stage, WorkflowDefinition
from eios_workflow import load_definitions

REPO = Path(__file__).resolve().parents[2]


def _definitions() -> dict[str, WorkflowDefinition]:
    definitions, errors = load_definitions(REPO / "workflows")
    assert errors == []
    return definitions


def test_every_verify_node_requires_a_reviewer_who_is_not_the_writer() -> None:
    for definition in _definitions().values():
        verify = [n for n in definition.nodes if n.stage is Stage.VERIFY]
        assert verify, f"{definition.id} has no VERIFY node"
        for node in verify:
            assert REVIEWER_ROLE in node.roles, f"{definition.id}/{node.id}"
            assert WRITER_ROLE not in node.roles, f"{definition.id}/{node.id}"
            assert not node.writer_allowed, f"{definition.id}/{node.id}"


def test_only_implementation_nodes_may_change_files() -> None:
    for definition in _definitions().values():
        for node in definition.nodes:
            if node.writer_allowed:
                assert node.stage is Stage.IMPLEMENT, f"{definition.id}/{node.id}"


def test_every_verify_node_checks_a_result_not_just_a_summary() -> None:
    for definition in _definitions().values():
        for node in definition.nodes:
            if node.stage is Stage.VERIFY:
                kinds = {c.kind for c in node.criteria}
                assert kinds & {"equals", "evidence", "capability"}, f"{definition.id}/{node.id}"


def test_the_default_definition_still_demands_real_evidence_at_verify() -> None:
    verify = _definitions()["engineering.default"].node("verify")
    assert any(c.kind == "evidence" and c.min_count >= 1 for c in verify.criteria)
