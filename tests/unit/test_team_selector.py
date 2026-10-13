from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest
from pydantic import ValidationError

from eios_capability import load_directory
from eios_domain.policy import Risk
from eios_domain.workflow import NodeDef, Stage, WorkflowDefinition
from eios_workflow import AgentInfo, TeamSignals, assert_one_writer, load_definitions, select_team
from eios_workflow.team import MAX_SPECIALISTS, path_roles

REPO = Path(__file__).resolve().parents[2]


def roster() -> dict[str, AgentInfo]:
    agents: dict[str, AgentInfo] = {}
    for _, m in load_directory(REPO / "manifests" / "agents").manifests:
        assert m.kind == "agent"
        agents[m.id] = AgentInfo(m.id, m.can_write, True, tuple(m.capabilities))
    return agents


AGENTS = roster()


@dataclass(frozen=True)
class DogfoodCase:
    """A goal from the dogfood run (T1-T8 and the probes in the findings) and the roles it needs."""

    id: str
    goal: str
    risk: Risk = Risk.MEDIUM
    signals: dict[str, object] = field(default_factory=dict)
    primary: str | None = None
    writer: bool = True  # False: read-only goal, no writer at all
    only: bool = False  # the team is the primary role alone
    includes: frozenset[str] = frozenset()
    excludes: frozenset[str] = frozenset()


def _case(id_: str, goal: str, risk: Risk = Risk.MEDIUM, **kw: object) -> DogfoodCase:
    signals = kw.pop("signals", {})
    assert isinstance(signals, dict)
    return DogfoodCase(id_, goal, risk, signals, **kw)  # type: ignore[arg-type]


def _roles(*roles: str) -> frozenset[str]:
    return frozenset(roles)


DOGFOOD = [
    _case(
        "T1 readme typo",
        "Fix a typo in the README",
        Risk.LOW,
        signals={"paths": ("README.md",)},
        primary="technical_writer",
        only=True,
    ),
    _case(
        "T1 table of contents",
        "Add a table of contents to docs/development.md",
        Risk.LOW,
        signals={"paths": ("docs/development.md",)},
        primary="technical_writer",
        only=True,
    ),
    _case(
        "T1 token docs",
        "Fix a typo in the token docs",
        Risk.LOW,
        primary="technical_writer",
        only=True,
    ),
    _case(
        "T1 module layout docs",
        "Update the README to document the new module layout",
        Risk.LOW,
        signals={"paths": ("README.md",)},
        primary="technical_writer",
        only=True,
    ),
    _case(
        "T2 audit_log column",
        "Add a nullable correlation_id column to the audit_log table, with an Alembic migration",
        primary="software_engineer",
        includes=_roles("database_engineer", "security_engineer"),
    ),
    _case(
        "T3 rename policy gate",
        "Rename PolicyEngine.evaluate to PolicyEngine.decide and update all callers",
        Risk.HIGH,
        signals={"dependents": 17},
        primary="software_engineer",
        includes=_roles("security_engineer", "architect"),
    ),
    _case(
        "T4 harden admin_token",
        "Harden admin_token comparison in _require_admin",
        Risk.HIGH,
        signals={"paths": ("apps/api/src/eios_api/routes/admin.py",), "explicit_targets": True},
        includes=_roles("security_engineer"),
    ),
    _case(
        "T4 constant time",
        "Make _require_admin constant time",
        includes=_roles("security_engineer"),
    ),
    _case(
        "T5 new enum value",
        "Add a new SANDBOX_TIMEOUT EventType and show it in the UI Mission Control view",
        primary="software_engineer",
        excludes=_roles("security_engineer", "database_engineer", "ml_engineer"),
    ),
    _case(
        "T6 sandbox timeout",
        "Change the sandbox default timeout from 30 to 60 seconds",
        signals={"paths": ("packages/policy/src/eios_policy/sandbox.py",)},
        primary="software_engineer",
        includes=_roles("security_engineer"),
    ),
    _case(
        "T7 postgres bump",
        "Bump the postgres base image to version 17",
        signals={"paths": ("docker-compose.yml",), "languages": frozenset({"yaml"})},
        includes=_roles("database_engineer", "devops_sre"),
    ),
    _case(
        "T7 compose by path only",
        "Bump the base image to version 17",
        signals={"paths": ("docker-compose.yml",), "languages": frozenset({"yaml"})},
        includes=_roles("devops_sre"),
    ),
    _case(
        "T7 ci by path only",
        "Pin the runner version",
        signals={"paths": (".github/workflows/ci.yml",)},
        includes=_roles("devops_sre"),
    ),
    _case(
        "T8 read-only review",
        "Review the workflow engine enforcement of the one-writer rule and report any way "
        "to bypass it",
        primary="code_reviewer",
        writer=False,
        includes=_roles("security_engineer"),
    ),
    _case(
        "explain is read-only",
        "Explain how the job queue retries failed jobs",
        Risk.LOW,
        writer=False,
        excludes=_roles("software_engineer"),
    ),
    _case(
        "investigate builds",
        "Investigate why context builds are slow",
        Risk.LOW,
        writer=False,
        includes=_roles("performance_engineer"),
        excludes=_roles("software_engineer"),
    ),
    _case(
        "security audit",
        "Audit the MCP server surface for anything that lets a model "
        "reach the database or run a shell",
        Risk.HIGH,
        writer=False,
        includes=_roles("security_engineer"),
    ),
    _case(
        "pydantic model is not ML",
        "Add a created_by field to the Project model",
        primary="software_engineer",
        excludes=_roles("ml_engineer"),
    ),
    _case(
        "author is not auth",
        "Credit the author in the footer",
        Risk.LOW,
        excludes=_roles("security_engineer"),
    ),
]


@pytest.mark.parametrize("case", DOGFOOD, ids=lambda c: c.id)
def test_dogfood_goals_get_the_expected_roles(case: DogfoodCase) -> None:
    team = select_team(case.goal, TeamSignals(risk=case.risk, **case.signals), AGENTS)  # type: ignore[arg-type]
    roles = set(team.roles)
    if case.primary:
        assert team.primary_role == case.primary, team.reasons
    assert case.includes <= roles, (team.roles, team.reasons, team.skipped)
    assert not (case.excludes & roles), (team.roles, team.reasons)
    if case.only:
        assert team.specialists == [], team.reasons
    if not case.writer:
        assert team.writer is None and not AGENTS[team.primary_role].can_write
        assert not {"file_edit", "build_run"} & set(team.capabilities_needed)
        assert "independent review before completion" not in team.review_requirements
    else:
        assert team.writer is not None
        assert_one_writer(team, AGENTS)


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
    assert len(team.specialists) <= MAX_SPECIALISTS
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


def test_specialists_are_capped_at_two_unless_the_risk_is_critical() -> None:
    goal = (
        "Optimize the slow SQL query, harden the token auth, redesign the module boundaries, "
        "fix the docker pipeline and investigate the legacy undocumented behaviour"
    )
    high = select_team(goal, TeamSignals(risk=Risk.HIGH, touches_sql=True), AGENTS)
    critical = select_team(goal, TeamSignals(risk=Risk.CRITICAL, touches_sql=True), AGENTS)
    assert MAX_SPECIALISTS == 2 and len(high.specialists) == 2
    assert 2 < len(critical.specialists) <= 4


@pytest.mark.parametrize(
    ("path", "role"),
    [
        ("packages/policy/src/eios_policy/engine.py", "security_engineer"),
        ("apps/api/src/eios_api/routes/admin.py", "security_engineer"),
        ("src/auth/session.py", "security_engineer"),
        ("migrations/versions/0010_add_column.py", "database_engineer"),
        ("packages/storage/src/eios_storage/tables/policy.py", "database_engineer"),
        ("db/schema.sql", "database_engineer"),
        ("docker-compose.yml", "devops_sre"),
        ("services/api/Dockerfile", "devops_sre"),
        (".github/workflows/ci.yml", "devops_sre"),
    ],
)
def test_paths_map_to_roles_by_glob(path: str, role: str) -> None:
    assert role in path_roles((path,))


@pytest.mark.parametrize(
    "path", ["src/author.py", "src/authors/list.py", "README.md", "src/index.py", "web/src/ui.tsx"]
)
def test_unrelated_paths_map_to_no_role(path: str) -> None:
    assert path_roles((path,)) == {}


def test_documentation_only_work_ignores_subject_keywords_and_paths() -> None:
    team = select_team(
        "Document the sandbox policy and token handling",
        TeamSignals(risk=Risk.LOW, paths=("docs/security/policy/sandbox.md",)),
        AGENTS,
    )
    assert team.primary_role == "technical_writer" and team.specialists == []
    assert "tests added or the absence justified" not in team.review_requirements


@pytest.mark.parametrize(
    ("goal", "writes"),
    [
        ("Explain why the writer role is rejected", False),
        ("Review the build step", False),
        ("Understand how the builds are scheduled", False),
        ("Review the code and fix the typos", True),
        ("Can you please fix the retry limit", True),
        ("I want to add a new retrieval source for CI build logs", True),
        ("Investigate why context builds are slow and then fix it", True),
        ("Audit the auth module, then harden it", True),
    ],
)
def test_a_change_verb_only_counts_as_the_main_verb_of_a_clause(goal: str, writes: bool) -> None:
    team = select_team(goal, TeamSignals(risk=Risk.LOW), AGENTS)
    assert (team.writer is not None) is writes, team.reasons


def test_a_name_the_task_introduces_does_not_trigger_subject_keywords() -> None:
    new = select_team("Add a new SANDBOX_TIMEOUT EventType", TeamSignals(risk=Risk.LOW), AGENTS)
    existing = select_team("Add admin_token rotation", TeamSignals(risk=Risk.LOW), AGENTS)
    assert "security_engineer" not in new.roles and "security_engineer" in existing.roles


def test_read_only_primary_is_a_non_writer_and_drops_edit_and_build_capabilities() -> None:
    team = select_team("Explain how the job queue retries failed jobs", TeamSignals(), AGENTS)
    assert team.writer is None and not AGENTS[team.primary_role].can_write
    assert not {"file_edit", "build_run"} & set(team.capabilities_needed)
    thin = {k: v for k, v in AGENTS.items() if k not in {"project_archaeologist", "code_reviewer"}}
    fallback = select_team("Explain how the job queue retries failed jobs", TeamSignals(), thin)
    assert fallback.primary_role == "architect" and fallback.writer is None


def test_confidence_rests_on_evidence_not_on_the_risk_label() -> None:
    plain = select_team("Fix the pagination", TeamSignals(risk=Risk.LOW), AGENTS)
    by_risk = select_team("Fix the pagination", TeamSignals(risk=Risk.HIGH), AGENTS)
    by_evidence = select_team("Harden the token check", TeamSignals(risk=Risk.LOW), AGENTS)
    assert by_risk.specialists and by_risk.confidence < plain.confidence
    assert "security_engineer" in by_evidence.specialists
    assert by_evidence.confidence > plain.confidence


def test_every_definition_names_the_exact_output_key_in_its_field_criteria() -> None:
    defs, errors = load_definitions(REPO / "workflows")
    assert errors == [] and {"engineering.default", "engineering.docs"} <= set(defs)
    for definition in defs.values():
        for node in definition.nodes:
            for c in node.criteria:
                if c.kind == "field":
                    assert c.id == c.field, f"{definition.id}/{node.id}: {c.id} != {c.field}"


def test_default_workflow_requires_independent_verification_and_a_curator() -> None:
    defs, _ = load_definitions(REPO / "workflows")
    default = defs["engineering.default"]
    assert default.node("verify").roles == ["reviewer"]
    assert default.node("curate").roles == ["knowledge_curator", "writer"]
    assert "context_confidence" in {c.id for c in default.node("understand").criteria}


def test_docs_definition_needs_no_test_evidence() -> None:
    defs, _ = load_definitions(REPO / "workflows")
    docs = defs["engineering.docs"]
    assert [n.id for n in docs.nodes] == ["understand", "implement", "verify", "curate"]
    assert all(c.kind != "evidence" for n in docs.nodes for c in n.criteria)
    assert docs.node("verify").on_failure.goto == "implement"


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
