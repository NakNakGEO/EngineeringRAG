"""Agent Team Selector: the minimum effective team, deterministic and explainable.

Rules: always one primary role (default Software Engineer for coding); specialists only when
domain, risk, complexity, missing expertise or required independent review justifies them; never
more than one writer; never a role merely because it exists. Every role that was considered but
not used is listed with the reason.

Goals are matched on identifier-split tokens (``admin_token`` and ``PolicyEngine.evaluate`` are
read as words), so keyword triggers work for how engineers actually phrase tasks. Paths named by
the caller map to roles through globs. Documentation-only work ignores subject keywords: the
words of a README describe its topic, not the change.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field

from eios_domain.policy import Risk
from eios_domain.workflow import TeamSelection
from eios_knowledge.text import tokenize

MAX_SPECIALISTS = 2
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
    symbols: tuple[str, ...] = ()
    languages: frozenset[str] = frozenset()
    dependents: int = 0
    tests_missing: bool = False
    touches_sql: bool = False
    coverage: float | None = None  # fraction of the project understood, if known
    explicit_targets: bool = False  # the caller named concrete files/symbols
    extra: dict[str, str] = field(default_factory=dict)


def _words(*words: str) -> re.Pattern[str]:
    """Whole-word match on normalised text (``auth`` does not match ``author``)."""
    return re.compile(r"\b(?:" + "|".join(words) + r")\b", re.IGNORECASE)


# A name the task introduces ("add a new SANDBOX_TIMEOUT EventType", "add a created_by field")
# is not a reference to existing code and must not trigger subject keywords.
_NEW_NAME = re.compile(
    r"(?i:\b(?:add|create|introduce|new)\s+(?:(?:an?|the|new|nullable|optional|required)\s+)*)"
    r"[A-Za-z0-9]+(?:_[A-Za-z0-9]+)+"
    r"(?i:\s+(?:column|field|enum|event\s?type|constant|variable|parameter|setting|flag|option|"
    r"attribute|property|key|member|value|class|function|method|endpoint|type|metric|label)\b)"
)


def _normalise(text: str) -> str:
    """Lower-case identifier-split words joined by single spaces."""
    return " ".join(tokenize(_NEW_NAME.sub(" ", text)))


_DOCS = _words("docs?", "documentation", "readme", "changelog", "tutorials?", "adrs?")
_DOC_SUFFIXES = (".md", ".rst", ".txt", ".adoc")
_CODE_TARGET = _words(
    "code", "functions?", "class(?:es)?", "endpoints?", "bugs?", "apis?", "tests?",
    "migrations?", "schemas?", "services?", r"refactor\w*", "performance",
)  # fmt: skip
_READ_ONLY = _words(
    r"review\w*", r"audit\w*", r"explain\w*", r"investigat\w+", r"understand\w*", r"analy[sz]\w+",
    r"assess\w*", r"describe\w*", r"summari[sz]\w+", r"trace\w*", r"locate\w*",
)  # fmt: skip
_REVIEW = _words(r"review\w*", r"audit\w*")
_DESIGN = _words(
    r"design\w*", "architecture", "architect", "boundary", "boundaries", "adrs?",
    r"refactor\w*", r"decouple\w*",
)  # fmt: skip
_SECURITY = _words(
    "auth", "authn", "authz", "authentication", r"authori[sz]\w+", "permissions?", "secrets?",
    "tokens?", "passwords?", r"crypto\w*", r"encrypt\w*", r"vulnerab\w+", "injection", "xss",
    "csrf", r"sandbox\w*", r"polic(?:y|ies)", "oauth", "jwt", r"saniti[sz]\w+", "security",
    r"audit\w*", r"harden\w*", r"bypass\w*", "shells?", r"privilege\w*", r"exploit\w*", "admins?",
)  # fmt: skip
_DATABASE = _words(
    "sql", "databases?", "schemas?", "migrations?", "index(?:es)?", "stored procedures?",
    "procedures?", "quer(?:y|ies)", "ddl", "transactions?", r"postgres\w*", "columns?", "alembic",
    "constraints?", "foreign keys?", r"(?:create|alter|drop|truncate)(?: \w+){0,3} tables?(?! of)",
)  # fmt: skip
_PERF = _words(
    "performance", r"slow\w*", "latency", "throughput", r"optimi[sz]\w+", r"profil\w+",
    "bottlenecks?", "memory leaks?",
)  # fmt: skip
_DEVOPS = _words(
    r"docker\w*", "compose", "ci", "pipelines?", r"deploy\w*", "kubernetes", "helm", "terraform",
    "monitoring", r"releas\w+", "base images?",
)  # fmt: skip
_LEGACY = _words(
    "legacy", "undocumented", "reverse", r"decompil\w+", "unfamiliar", "inherited"
)  # fmt: skip
_DATA = _words("etl", "data pipelines?", "warehouse", "lineage", "datasets?", "ingestion")
_AI = _words("llms?", "prompts?", "embeddings?", "rag", "agents?", "retrieval")
_ML = _words(  # a bare "model" is a domain/pydantic model far more often than an ML model
    "ml", "training", "inference", "classifiers?", "feature store", "pytorch", "tensorflow",
    "sklearn", "neural", "machine learning", "deep learning", r"fine tun\w+", "hyperparameters?",
)  # fmt: skip
_RESEARCH = _words(r"research\w*", "papers?", "survey", "state of the art", "compare approaches")

_CHANGE_VERBS = (
    "implement", "fix", "add", "build", "refactor", "change", "update", "rename", "remove",
    "create", "write", "migrate", "optimise", "optimize", "make", "harden", "bump", "upgrade",
    "replace", "move", "delete", "rotate", "rework", "tune", "extend", "introduce", "enable",
    "disable", "set", "switch", "convert", "port", "wire", "patch", "drop", "increase",
    "decrease", "lower", "raise", "support", "ban", "reject", "deprecate", "extract", "split",
    "revert", "restructure", "decouple", "redesign", "correct", "adjust", "restrict", "prevent",
    "forbid", "limit", "require", "allow", "stop", "resolve",
)  # fmt: skip


def _forms(verb: str) -> set[str]:
    stem = verb[:-1] if verb.endswith("e") else verb
    forms = {verb, verb + "s", verb + "es", verb + "d", stem + "ed", stem + "ing"}
    if re.search(r"[^aeiou][aeiou][bdgnpt]$", verb):  # set -> setting, drop -> dropping
        forms |= {verb + verb[-1] + "ing", verb + verb[-1] + "ed"}
    return forms


# A change verb counts only as the main verb of a clause ("fix X and update Y"); the noun
# "writer" or "builds" inside a question is not a request to change anything.
_CHANGE_FORMS = frozenset({f for v in _CHANGE_VERBS for f in _forms(v)} | {"wrote", "written"})
_CLAUSE_SPLIT = re.compile(r"[.;:!?\n(]|,|\s-\s|\b(?:and|then|but|so|also|plus)\b", re.IGNORECASE)
_LEAD_FILLER = frozenset(
    {
        "please", "kindly", "we", "i", "you", "should", "must", "need", "needs", "want", "wants",
        "to", "can", "could", "would", "will", "shall", "let", "lets", "us", "now", "just", "go",
        "ahead", "first", "next", "finally", "how", "do", "does", "did", "why", "what",
    }
)  # fmt: skip


def _clause_heads(goal: str) -> Iterator[str]:
    for clause in _CLAUSE_SPLIT.split(goal):
        words = re.findall(r"[A-Za-z0-9_']+", clause.lower())
        head = next((w for w in words if w not in _LEAD_FILLER), None)
        if head is not None:
            yield head


def _changes_something(goal: str) -> bool:
    return any(head in _CHANGE_FORMS for head in _clause_heads(goal))


def _glob(pattern: str) -> re.Pattern[str]:
    out: list[str] = []
    i = 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("".join(out), re.IGNORECASE)


# Where a change lands says who must look at it, whatever the goal text says.
_PATH_GLOBS: dict[str, tuple[str, ...]] = {
    "security_engineer": (
        "**/policy/**", "**/eios_policy/**", "**/routes/admin*", "**/routes/policy*",
        "**/auth/**", "**/auth.*", "**/auth_*", "**/authn*", "**/authz*", "**/authentication*",
        "**/authorization*", "**/sandbox*", "**/secrets*", "**/crypto*", "**/root_policy*",
    ),
    "database_engineer": (
        "**/migrations/**", "**/alembic/**", "**/storage/tables/**", "**/eios_storage/tables/**",
        "**/*.sql",
    ),
    "devops_sre": (
        "**/docker-compose*", "**/compose.y*ml", "**/dockerfile*", "**/*.dockerfile",
        "**/.github/**", "**/.gitlab-ci.yml", "**/.circleci/**", "**/jenkinsfile",
        "**/azure-pipelines*.yml",
    ),
}  # fmt: skip
_PATH_ROLES = {role: [_glob(g) for g in globs] for role, globs in _PATH_GLOBS.items()}

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


def path_roles(paths: tuple[str, ...]) -> dict[str, str]:
    """Role -> the first path that maps to it by glob (migrations, auth, docker, CI...)."""
    found: dict[str, str] = {}
    for path in paths:
        norm = path.replace("\\", "/").removeprefix("./")
        for role, patterns in _PATH_ROLES.items():
            if role not in found and any(p.fullmatch(norm) for p in patterns):
                found[role] = path
    return found


def docs_paths_only(paths: Sequence[str]) -> bool:
    return bool(paths) and all(p.lower().endswith(_DOC_SUFFIXES) for p in paths)


def _docs_only(text: str, signals: TeamSignals) -> bool:
    if docs_paths_only(signals.paths):
        return True
    return bool(_DOCS.search(text)) and not _CODE_TARGET.search(text)


def select_team(goal: str, signals: TeamSignals, agents: dict[str, AgentInfo]) -> TeamSelection:
    reasons: dict[str, str] = {}
    skipped: dict[str, str] = {}
    text = _normalise(goal)
    docs_only = _docs_only(text, signals)
    read_only = bool(_READ_ONLY.search(text)) and not _changes_something(goal)

    def usable(role: str) -> bool:
        info = agents.get(role)
        return info is not None and info.routable

    def about(pattern: re.Pattern[str]) -> bool:
        """The goal's subject matches (documentation describes a subject, it does not change it)."""
        return not docs_only and pattern.search(text) is not None

    # ---- primary role -------------------------------------------------------------------
    candidates: list[tuple[str, str]]
    if docs_only:
        candidates = [("technical_writer", "documentation-only change")]
    elif read_only:
        review = ("code_reviewer", "read-only review request; no writer needed")
        design = ("architect", "read-only architectural analysis; no writer needed")
        study = ("project_archaeologist", "read-only investigation; no writer needed")
        candidates = (
            [review, design, study]
            if _REVIEW.search(text)
            else [design, review, study]
            if _DESIGN.search(text)
            else [study, review, design]
        )
    else:
        candidates = [("software_engineer", "default primary for coding work")]
    explicit_primary = docs_only or read_only
    chosen = next((c for c in candidates if usable(c[0])), None)
    if chosen is None:
        skipped[candidates[0][0]] = "primary role is not registered or routable"
        fallback = next((r for r in ("software_engineer", *agents) if usable(r)), candidates[0][0])
        chosen, explicit_primary = (fallback, f"fallback primary ({candidates[0][1]})"), False
    primary, why = chosen
    reasons[primary] = why

    # ---- specialists -------------------------------------------------------------------------
    wanted: dict[str, list[str]] = {}
    backed: set[str] = set()  # roles with a trigger beyond the risk label
    risk_rank = {Risk.LOW: 0, Risk.MEDIUM: 1, Risk.HIGH: 2, Risk.CRITICAL: 3}[signals.risk]

    def want(role: str, reason: str, *, evidence: bool = True) -> None:
        wanted.setdefault(role, []).append(reason)
        if evidence:
            backed.add(role)

    by_path = {} if docs_only else path_roles(signals.paths)
    if about(_SECURITY):
        want("security_engineer", "security-sensitive wording in the goal")
    if "security_engineer" in by_path:
        want(
            "security_engineer", f"touches security-sensitive code ({by_path['security_engineer']})"
        )
    if risk_rank >= 3:
        want("security_engineer", "critical-risk change needs a security review", evidence=False)
    if about(_DATABASE) or signals.touches_sql or "sql" in signals.languages:
        want("database_engineer", "the change involves SQL, schema or database design")
    if "database_engineer" in by_path:
        want(
            "database_engineer",
            f"touches migrations, tables or SQL ({by_path['database_engineer']})",
        )
    if about(_DESIGN):
        want("architect", "architectural wording in the goal")
    if signals.dependents >= 8:
        want("architect", f"wide blast radius ({signals.dependents} dependents)")
    if risk_rank >= 2:
        want("architect", "high-risk change needs an architectural check", evidence=False)
    if signals.tests_missing and risk_rank >= 1 and not docs_only:
        want("qa_engineer", "affected code has no tests")
    elif risk_rank >= 2:
        want(
            "qa_engineer", "high-risk change needs independent verification design", evidence=False
        )
    if about(_LEGACY):
        want("reverse_engineer", "behaviour of legacy/undocumented code must be inferred")
    if signals.coverage is not None and (
        (about(_LEGACY) and signals.coverage < 0.5)
        or (signals.coverage < 0.25 and not signals.explicit_targets)
    ):
        want("project_archaeologist", "unfamiliar project with low semantic coverage")
    if about(_PERF):
        want("performance_engineer", "performance work needs measurement-driven analysis")
    if about(_DEVOPS):
        want("devops_sre", "build, deployment or infrastructure concerns")
    if "devops_sre" in by_path:
        want("devops_sre", f"touches build or deployment files ({by_path['devops_sre']})")
    elif "dockerfile" in signals.languages and not docs_only:
        want("devops_sre", "touches a Dockerfile")
    if about(_DATA):
        want("data_engineer", "data pipeline concerns")
    if about(_AI):
        want("ai_engineer", "LLM/retrieval application concerns")
    if about(_ML):
        want("ml_engineer", "ML model lifecycle concerns")
    if about(_RESEARCH):
        want("ai_researcher", "external research is requested")

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
        reasons[role] = "; ".join(wanted[role])
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
    if signals.tests_missing and not docs_only:
        review_requirements.append("tests added or the absence justified")
    capabilities: set[str] = set()
    for role in [primary, *specialists]:
        capabilities.update(agents[role].capabilities if role in agents else ())
    if writer is None:
        capabilities -= {"file_edit", "build_run"}  # a read-only team neither edits nor builds
    # confidence follows how much of the selection rests on evidence, not on defaults
    confidence = 0.5
    confidence += 0.15 if signals.explicit_targets else 0.0
    confidence += 0.05 if signals.paths or signals.languages else 0.0
    confidence += 0.1 if explicit_primary else 0.0
    confidence += 0.05 * min(sum(1 for r in specialists if r in backed), 3)
    confidence -= 0.05 * sum(1 for r in specialists if r in wanted and r not in backed)
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
