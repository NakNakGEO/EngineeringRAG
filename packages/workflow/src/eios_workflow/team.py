"""Agent Team Selector: the minimum effective team, deterministic and explainable.

Rules: always one primary role (default Software Engineer for coding); specialists only when
domain, risk, complexity, missing expertise or required independent review justifies them; never
more than one writer; never a role merely because it exists. Every role that was considered but
not used is listed with the reason.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from eios_domain.policy import Risk
from eios_domain.workflow import TeamSelection

MAX_SPECIALISTS = 3
MAX_SPECIALISTS_CRITICAL = 4


@dataclass(frozen=True)
class AgentInfo:
    id: str
    can_write: bool
    routable: bool
    capabilities: tuple[str, ...] = ()


@dataclass(frozen=True)
class TeamSignals:
    """Facts about the task that are not in the goal text (from impact analysis, coverage...)."""

    risk: Risk = Risk.MEDIUM
    paths: tuple[str, ...] = ()
    languages: frozenset[str] = frozenset()
    dependents: int = 0
    tests_missing: bool = False
    touches_sql: bool = False
    coverage: float | None = None  # fraction of the project understood, if known
    explicit_targets: bool = False  # the caller named concrete files/symbols
    extra: dict[str, str] = field(default_factory=dict)


def _words(*words: str) -> re.Pattern[str]:
    """Whole-word match (``auth`` does not match ``author``); list inflections explicitly."""
    return re.compile(r"\b(?:" + "|".join(words) + r")\b", re.IGNORECASE)


_DOCS = _words("docs?", "documentation", "readme", "changelog", "tutorials?", "adrs?")
_CODE_CHANGE = _words(
    r"implement\w*", r"fix\w*", r"add\w*", r"build\w*", r"refactor\w*", r"chang\w+", r"updat\w+",
    r"renam\w+", r"remov\w+", r"creat\w+", r"writ\w+", r"migrat\w+", "bugs?", "features?",
    "endpoints?", r"optimi[sz]\w+",
)  # fmt: skip
_CODE_TARGET = _words(
    "code", "functions?", "class(?:es)?", "endpoints?", "bugs?", "apis?", "modules?", "tests?",
    "migrations?", "schemas?", "services?", "refactor\\w*", "performance",
)  # fmt: skip
_READ_ONLY = _words(
    r"review\w*", r"audit\w*", r"explain\w*", r"investigat\w+", r"understand\w*", r"analy[sz]\w+",
    r"assess\w*",
)  # fmt: skip
_DESIGN = _words(
    r"design\w*", "architecture", "architect", "boundary", "boundaries", "adrs?",
    r"refactor\w*", r"decouple\w*", "modules?",
)  # fmt: skip
_SECURITY = _words(
    "auth", "authn", "authz", "authentication", r"authori[sz]\w+", "permissions?", "secrets?",
    "tokens?", "passwords?", r"crypto\w*", r"encrypt\w*", r"vulnerab\w+", "injection", "xss",
    "csrf", r"sandbox\w*", r"polic(?:y|ies)", "oauth", "jwt", r"saniti[sz]\w+", "security",
)  # fmt: skip
_DATABASE = _words(
    "sql", "databases?", "schemas?", "migrations?", "index(?:es)?", "stored procedures?",
    "procedures?", "quer(?:y|ies)", "tables?", "ddl", "transactions?",
)  # fmt: skip
_PERF = _words(
    "performance", r"slow\w*", "latency", "throughput", r"optimi[sz]\w+", r"profil\w+",
    "bottlenecks?", "memory leaks?",
)  # fmt: skip
_DEVOPS = _words(
    r"docker\w*", "compose", "ci", "pipelines?", r"deploy\w*", "kubernetes", "helm", "terraform",
    "monitoring", r"releas\w+",
)  # fmt: skip
_LEGACY = _words(
    "legacy", "undocumented", "reverse", r"decompil\w+", "unfamiliar", "inherited"
)  # fmt: skip
_DATA = _words("etl", "data pipelines?", "warehouse", "lineage", "datasets?", "ingestion")
_AI = _words("llms?", "prompts?", "embeddings?", "rag", "agents?", "retrieval")
_ML = _words("ml", "models?", "training", "inference", "classifiers?", "feature store")
_RESEARCH = _words(r"research\w*", "papers?", "survey", "state of the art", "compare approaches")

_PRIORITY = (
    "security_engineer",
    "database_engineer",
    "architect",
    "qa_engineer",
    "reverse_engineer",
    "project_archaeologist",
    "performance_engineer",
    "devops_sre",
    "data_engineer",
    "ai_engineer",
    "ml_engineer",
    "ai_researcher",
)


def _docs_only(goal: str, signals: TeamSignals) -> bool:
    if signals.paths and all(p.lower().endswith((".md", ".rst", ".txt")) for p in signals.paths):
        return True
    return bool(_DOCS.search(goal)) and not _CODE_TARGET.search(goal)


def select_team(goal: str, signals: TeamSignals, agents: dict[str, AgentInfo]) -> TeamSelection:
    reasons: dict[str, str] = {}
    skipped: dict[str, str] = {}

    def usable(role: str) -> bool:
        info = agents.get(role)
        return info is not None and info.routable

    # ---- primary role -------------------------------------------------------------------
    if _docs_only(goal, signals) and usable("technical_writer"):
        primary, why = "technical_writer", "documentation-only change"
    elif _READ_ONLY.search(goal) and not _CODE_CHANGE.search(goal):
        if re.search(r"\breview", goal, re.IGNORECASE) and usable("code_reviewer"):
            primary, why = "code_reviewer", "read-only review request; no writer needed"
        elif _DESIGN.search(goal) and usable("architect"):
            primary, why = "architect", "read-only architectural analysis; no writer needed"
        else:
            primary, why = "software_engineer", "default primary for engineering work"
    else:
        primary, why = "software_engineer", "default primary for coding work"
    if not usable(primary):
        fallback = next((r for r in ("software_engineer", *agents) if usable(r)), primary)
        skipped[primary] = "primary role is not registered or routable"
        primary, why = fallback, f"fallback primary ({why})"
    reasons[primary] = why

    # ---- specialists -------------------------------------------------------------------------
    wanted: dict[str, str] = {}
    risk_rank = {Risk.LOW: 0, Risk.MEDIUM: 1, Risk.HIGH: 2, Risk.CRITICAL: 3}[signals.risk]
    if _SECURITY.search(goal) or (risk_rank >= 3):
        wanted["security_engineer"] = (
            "security-sensitive wording in the goal"
            if _SECURITY.search(goal)
            else "critical-risk change needs a security review"
        )
    if _DATABASE.search(goal) or signals.touches_sql or "sql" in signals.languages:
        wanted["database_engineer"] = "the change involves SQL, schema or database design"
    if _DESIGN.search(goal) or signals.dependents >= 8 or risk_rank >= 2:
        wanted["architect"] = (
            "architectural wording in the goal"
            if _DESIGN.search(goal)
            else f"wide blast radius ({signals.dependents} dependents)"
            if signals.dependents >= 8
            else "high-risk change needs an architectural check"
        )
    if signals.tests_missing and risk_rank >= 1:
        wanted["qa_engineer"] = "affected code has no tests"
    elif risk_rank >= 2:
        wanted["qa_engineer"] = "high-risk change needs independent verification design"
    if _LEGACY.search(goal):
        wanted["reverse_engineer"] = "behaviour of legacy/undocumented code must be inferred"
    if (_LEGACY.search(goal) and (signals.coverage is not None and signals.coverage < 0.5)) or (
        signals.coverage is not None and signals.coverage < 0.25 and not signals.explicit_targets
    ):
        wanted["project_archaeologist"] = "unfamiliar project with low semantic coverage"
    if _PERF.search(goal):
        wanted["performance_engineer"] = "performance work needs measurement-driven analysis"
    if _DEVOPS.search(goal):
        wanted["devops_sre"] = "build, deployment or infrastructure concerns"
    if _DATA.search(goal):
        wanted["data_engineer"] = "data pipeline concerns"
    if _AI.search(goal):
        wanted["ai_engineer"] = "LLM/retrieval application concerns"
    if _ML.search(goal):
        wanted["ml_engineer"] = "ML model lifecycle concerns"
    if _RESEARCH.search(goal):
        wanted["ai_researcher"] = "external research is requested"

    cap = MAX_SPECIALISTS_CRITICAL if signals.risk is Risk.CRITICAL else MAX_SPECIALISTS
    specialists: list[str] = []
    primary_info = agents.get(primary)
    writer = primary if primary_info and primary_info.can_write else None
    for role in _PRIORITY:
        if role not in wanted or role == primary:
            continue
        if not usable(role):
            skipped[role] = "needed but not registered or routable"
            continue
        info = agents[role]
        if info.can_write and writer is not None:
            skipped[role] = "a second writer is never selected (one writer, many reviewers)"
            continue
        if len(specialists) >= cap:
            skipped[role] = f"specialist limit ({cap}) reached; lower priority than selected roles"
            continue
        specialists.append(role)
        reasons[role] = wanted[role]
    # independent review for risky writes, using the cheapest reviewer role available
    if (
        writer is not None
        and risk_rank >= 1
        and not any(r in specialists for r in ("architect", "qa_engineer", "security_engineer"))
        and "code_reviewer" not in specialists
        and usable("code_reviewer")
        and primary != "code_reviewer"
        and len(specialists) < cap
    ):
        specialists.append("code_reviewer")
        reasons["code_reviewer"] = "independent review of a non-trivial change"
    for role in _PRIORITY:
        if role not in wanted and role not in skipped and role != primary:
            skipped.setdefault(role, "no trigger in the goal or impact signals")

    # ---- outputs ---------------------------------------------------------------------------------
    review_requirements: list[str] = []
    if writer is not None:
        review_requirements.append("independent review before completion")
    if "security_engineer" in specialists:
        review_requirements.append("security review of the change")
    if "database_engineer" in specialists:
        review_requirements.append(
            "SQL returned as text for human execution; migration + rollback reviewed"
        )
    if signals.tests_missing:
        review_requirements.append("tests added or the absence justified")
    capabilities: set[str] = set()
    for role in [primary, *specialists]:
        capabilities.update(agents[role].capabilities if role in agents else ())
    confidence = 0.55
    confidence += 0.15 if signals.explicit_targets else 0.0
    confidence += 0.1 if signals.paths or signals.languages else 0.0
    confidence += (
        0.1 if wanted and not any("not registered" in v for v in skipped.values()) else 0.0
    )
    confidence -= 0.2 if any("not registered" in v for v in skipped.values()) else 0.0
    confidence = max(0.1, min(0.95, confidence))
    return TeamSelection(
        primary_role=primary,
        specialists=specialists,
        reasons=reasons,
        capabilities_needed=sorted(capabilities),
        review_requirements=review_requirements,
        confidence=round(confidence, 2),
        skipped=skipped,
        writer=writer,
    )


def assert_one_writer(team: TeamSelection, agents: dict[str, AgentInfo]) -> None:
    writers = [r for r in team.roles if agents.get(r) and agents[r].can_write]
    if len(writers) > 1:
        raise ValueError(f"more than one writer selected: {writers}")
    if writers and team.writer != writers[0]:
        raise ValueError("the declared writer does not match the selected writer role")
