from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from eios_capability import load_directory
from eios_domain.policy import Risk
from eios_domain.workflow import NodeDef, Stage, WorkflowDefinition
from eios_workflow import AgentInfo, TeamSignals, assert_one_writer, load_definitions, select_team

REPO = Path(__file__).resolve().parents[2]


def roster() -> dict[str, AgentInfo]:
    agents: dict[str, AgentInfo] = {}
    for _, m in load_directory(REPO / "manifests" / "agents").manifests:
        assert m.kind == "agent"
        agents[m.id] = AgentInfo(m.id, m.can_write, True, tuple(m.capabilities))
    return agents


AGENTS = roster()


def pick(goal: str, **kw: object) -> object:
    return select_team(goal, TeamSignals(**kw), AGENTS)  # type: ignore[arg-type]


def test_plain_coding_goal_gets_software_engineer_alone_at_low_risk() -> None:
    team = select_team("Fix the off-by-one in pagination", TeamSignals(risk=Risk.LOW), AGENTS)
    assert team.primary_role == "software_engineer" and team.specialists == []
    assert team.writer == "software_engineer"
    assert "independent review before completion" in team.review_requirements
    assert team.skipped["security_engineer"] == "no trigger in the goal or impact signals"


def test_specialists_are_chosen_for_reasons_not_because_they_exist() -> None:
    team = select_team(
        "Add a migration that changes the ledger schema and a new index",
        TeamSignals(risk=Risk.MEDIUM, touches_sql=True),
        AGENTS,
    )
    assert "database_engineer" in team.specialists
    assert "SQL" in team.reasons["database_engineer"]
    assert "security_engineer" not in team.specialists and "ml_engineer" not in team.roles


def test_security_wording_and_critical_risk_bring_the_security_engineer() -> None:
    a = select_team("Rotate the JWT signing token handling", TeamSignals(risk=Risk.MEDIUM), AGENTS)
    b = select_team("Change payment rounding", TeamSignals(risk=Risk.CRITICAL), AGENTS)
    assert "security_engineer" in a.specialists and "security_engineer" in b.specialists


def test_word_boundaries_prevent_false_triggers() -> None:
    team = select_team("Credit the author in the footer", TeamSignals(risk=Risk.LOW), AGENTS)
    assert "security_engineer" not in team.specialists  # "author" is not "auth"
    assert "database_engineer" not in team.specialists


def test_one_writer_many_reviewers_and_no_second_writer() -> None:
    team = select_team(
        "Update the documentation and fix the bug and refactor the module",
        TeamSignals(risk=Risk.MEDIUM, paths=("README.md", "src/a.py")),
        AGENTS,
    )
    assert team.writer == team.primary_role
    writers = [r for r in team.roles if AGENTS[r].can_write]
    assert writers == [team.primary_role]
    assert_one_writer(team, AGENTS)


def test_docs_only_work_has_the_technical_writer_as_the_single_writer() -> None:
    team = select_team("Update the README installation section", TeamSignals(risk=Risk.LOW), AGENTS)
    assert team.primary_role == "technical_writer" and team.writer == "technical_writer"


def test_read_only_goals_need_no_writer() -> None:
    team = select_team(
        "Review the retry logic in PaymentGateway", TeamSignals(risk=Risk.LOW), AGENTS
    )
    assert team.primary_role == "code_reviewer" and team.writer is None
    assert "independent review before completion" not in team.review_requirements


def test_specialist_cap_and_reported_skips() -> None:
    goal = (
        "Optimize the slow SQL query, harden the token auth, redesign the module boundaries, "
        "fix the docker pipeline and investigate the legacy undocumented behaviour"
    )
    team = select_team(
        goal, TeamSignals(risk=Risk.HIGH, touches_sql=True, tests_missing=True), AGENTS
    )
    assert len(team.specialists) <= 3
    assert any("limit" in why for why in team.skipped.values())
    assert team.specialists[0] == "security_engineer"  # highest priority first


def test_unavailable_roles_are_reported_and_lower_confidence() -> None:
    thin = {k: v for k, v in AGENTS.items() if k != "database_engineer"}
    team = select_team(
        "Tune this SQL schema", TeamSignals(risk=Risk.MEDIUM, touches_sql=True), thin
    )
    assert "database_engineer" not in team.roles
    assert "not registered" in team.skipped["database_engineer"]
    full = select_team(
        "Tune this SQL schema", TeamSignals(risk=Risk.MEDIUM, touches_sql=True), AGENTS
    )
    assert team.confidence < full.confidence


def test_low_coverage_brings_the_archaeologist_only_without_explicit_targets() -> None:
    unfamiliar = select_team("Add a feature somewhere", TeamSignals(coverage=0.1), AGENTS)
    targeted = select_team(
        "Add a feature", TeamSignals(coverage=0.1, explicit_targets=True, paths=("a.py",)), AGENTS
    )
    assert "project_archaeologist" in unfamiliar.specialists
    assert "project_archaeologist" not in targeted.specialists


def test_default_workflow_definition_is_valid_and_has_the_five_stages() -> None:
    defs, errors = load_definitions(REPO / "workflows")
    assert errors == []
    d = defs["engineering.default"]
    assert [n.stage for n in d.nodes] == list(Stage)
    assert d.node("verify").on_failure.goto == "implement"


def test_definition_validation_rejects_broken_graphs() -> None:
    def node(i: str, nxt: str | None = None, **kw: object) -> NodeDef:
        return NodeDef(id=i, stage=Stage.UNDERSTAND, description="desc ok", next=nxt, **kw)  # type: ignore[arg-type]

    with pytest.raises(ValidationError, match="start node"):
        WorkflowDefinition(
            id="wf.x", version="1.0.0", description="desc ok", start="nope", nodes=[node("aa")]
        )
    with pytest.raises(ValidationError, match="unreachable"):
        WorkflowDefinition(
            id="wf.x",
            version="1.0.0",
            description="desc ok",
            start="aa",
            nodes=[node("aa"), node("bb")],
        )
    with pytest.raises(ValidationError, match="not defined"):
        WorkflowDefinition(
            id="wf.x", version="1.0.0", description="desc ok", start="aa", nodes=[node("aa", "zzz")]
        )
    with pytest.raises(ValidationError, match="terminal"):
        WorkflowDefinition(
            id="wf.x",
            version="1.0.0",
            description="desc ok",
            start="aa",
            nodes=[node("aa", "bb"), node("bb", "aa")],
        )
