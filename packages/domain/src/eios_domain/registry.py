"""Registry domain rules: states, origins, trust ceilings, routability (pure).

Capabilities are semantic needs; providers (tools/adapters/subsystems) implement them; agents
are reasoning roles; skills are reusable procedures. Everything is registered, versioned and
state-controlled. An unregistered or unapproved thing is never routable.
"""

from __future__ import annotations

import re
import unicodedata
from enum import StrEnum
from itertools import pairwise

# Capability ids that must never exist in any registry (Root Rule: external database isolation).
# Phase 6 enforces the same list from the signed Root Policy; this constant lets the registries
# refuse such manifests from the moment they exist.
FORBIDDEN_CAPABILITIES: frozenset[str] = frozenset(
    {
        "external_database_connect",
        "external_database_read",
        "external_database_write",
        "external_database_execute",
        "external_database_ddl",
    }
)
FORBIDDEN_CAPABILITY_PREFIXES: tuple[str, ...] = (
    "external_database",
    "external_db",
    "external_sql",
    "root_policy",
    "policy_modify",
)

ID_PATTERN = re.compile(r"^[a-z][a-z0-9_.-]{1,79}$")
VERSION_PATTERN = re.compile(r"^\d{1,4}\.\d{1,4}\.\d{1,4}(?:[-+][0-9A-Za-z.-]+)?$")

EXTERNAL_DATABASE_REASON = (
    "Root Policy forbids live access to external, company, QA or production databases. "
    "Ask for SQL as text for a human to run instead."
)

# -- capability ids ---------------------------------------------------------------------------
# Identifier rules are token based so that casing, separators and word order do not matter
# (external_db_connector, ExternalDbConnector, oracle-reader, run_sql, modify_root_policy).
_CAMEL_LOWER_UPPER = re.compile(r"([a-z0-9])([A-Z])")
_CAMEL_ACRONYM = re.compile(r"([A-Z]+)([A-Z][a-z])")
_DB_TOKENS = frozenset(
    {
        "database", "databases", "db", "dbs", "rdbms", "dbms", "datawarehouse", "dwh", "edw",
        "sqlserver", "mssql", "sqldb", "azuresql", "cloudsql", "oracle", "plsql", "postgres",
        "postgresql", "pgsql", "mysql", "mariadb", "mongo", "mongodb", "redis", "cassandra",
        "snowflake", "redshift", "bigquery", "teradata", "db2", "sybase", "informix", "netezza",
        "vertica", "saphana", "dynamodb", "cosmosdb", "couchbase", "couchdb", "jdbc", "odbc",
    }
)  # fmt: skip
_ENV_TOKENS = frozenset(
    {
        "external", "ext", "company", "corporate", "enterprise", "prod", "production", "preprod",
        "qa", "uat", "staging", "live", "remote", "customer", "vendor", "thirdparty", "legacy",
        "onprem", "onpremise", "onpremises",
    }
)  # fmt: skip
_ACCESS_TOKENS = frozenset(
    {
        "connector", "connectors", "connect", "connection", "client", "clients", "driver",
        "drivers", "adapter", "gateway", "proxy", "bridge", "reader", "writer", "executor",
        "runner", "query", "queries", "fetch", "fetcher", "read", "write", "execute", "exec",
        "access", "sync", "syncer", "dump", "loader", "extractor", "crawler", "scraper", "login",
        "puller", "importer", "exporter", "replicator", "ddl", "dml",
    }
)  # fmt: skip
_DB_SUBSTRINGS = (
    "database", "oracle", "postgres", "mysql", "mariadb", "mongodb", "redshift", "snowflake",
    "bigquery", "teradata", "sqlserver", "mssql", "cassandra", "dynamodb", "cosmosdb",
)  # fmt: skip
_ACCESS_SUBSTRINGS = (
    "connector", "driver", "adapter", "gateway", "reader", "writer", "executor", "runner", "fetch",
    "query", "client", "loader", "extractor", "crawler", "scraper", "connect", "sync",
)  # fmt: skip
_SQL_EXEC_TOKENS = frozenset(
    {
        "execute", "exec", "executor", "run", "runner", "connector", "connect", "client",
        "driver", "adapter", "gateway", "proxy", "login", "session",
    }
)  # fmt: skip
_POLICY_EDIT_TOKENS = frozenset(
    {
        "modify", "edit", "write", "change", "update", "disable", "override", "bypass", "patch",
        "unlock", "delete", "remove", "rewrite",
    }
)  # fmt: skip


def _identifier_tokens(identifier: str) -> list[str]:
    spaced = _CAMEL_ACRONYM.sub(r"\1 \2", _CAMEL_LOWER_UPPER.sub(r"\1 \2", identifier))
    return [t for t in re.split(r"[^a-z0-9]+", spaced.lower()) if t]


def _contains_run(tokens: list[str], run: list[str]) -> bool:
    n = len(run)
    return any(tokens[i : i + n] == run for i in range(len(tokens) - n + 1))


def is_forbidden_capability(capability_id: str) -> bool:
    """True for ids that name live external-database access or Root Policy changes.

    Only ever widened: the original exact/prefix rules still apply verbatim, and token rules
    catch the rest regardless of casing, separators or word order.
    """
    lowered = capability_id.lower()
    if lowered in FORBIDDEN_CAPABILITIES or lowered.startswith(FORBIDDEN_CAPABILITY_PREFIXES):
        return True
    tokens = _identifier_tokens(capability_id)
    if not tokens:
        return False
    if any(_contains_run(tokens, prefix.split("_")) for prefix in FORBIDDEN_CAPABILITY_PREFIXES):
        return True
    if "root" in tokens and "policy" in tokens:
        return True
    if "policy" in tokens and not _POLICY_EDIT_TOKENS.isdisjoint(tokens):
        return True
    if "".join(tokens).startswith(tuple(p.replace("_", "") for p in FORBIDDEN_CAPABILITY_PREFIXES)):
        return True
    if any(  # run-together words: oraclereader, sqlserverclient
        any(db in token for db in _DB_SUBSTRINGS) and any(a in token for a in _ACCESS_SUBSTRINGS)
        for token in tokens
    ):
        return True
    vocabulary = set(tokens) | {a + b for a, b in pairwise(tokens)}
    if not _DB_TOKENS.isdisjoint(vocabulary):
        return not _ENV_TOKENS.isdisjoint(vocabulary) or not _ACCESS_TOKENS.isdisjoint(vocabulary)
    return "sql" in vocabulary and not _SQL_EXEC_TOKENS.isdisjoint(vocabulary)


# -- free-text intent --------------------------------------------------------------------------
# The detector reads a request as a stream of lower-case words (identifiers split into words,
# Unicode normalised), one sentence at a time. A request is flagged when an access verb reaches a
# database target: "read orders directly from the company production Oracle database",
# "connect to prod", "run this migration against QA", "external_db_connector".
_DB_ENTERPRISE = (
    r"(?:oracle|pl ?sql|sql ?server|mssql|ms ?sql|azure sql|cloud sql|db2|sybase|informix|"
    r"teradata|netezza|vertica|sap ?hana|redshift|big ?query|azure synapse|snowflake|"
    r"dynamo ?db|cosmos ?db|couch ?base|rds)"
)
_DB_COMMON = (
    r"(?:data ?bases?|dbs?|rdbms|dbms|postgres(?:ql)?|pg ?sql|mysql|maria ?db|"
    r"mongo ?(?:db)?|redis|cassandra|couch ?db|data ?warehouse|dwh|edw|jdbc|odbc|read ?replicas?)"
)
# A DB noun that merely modifies a file-like noun ("database migration files") is not a target.
_FILELIKE = (
    r"(?! (?:(?:schema|ddl|sql|migration|seed|backup|dump|data|table) ){0,2}"
    r"(?:files?|scripts?|migrations?|dumps?|docs?|documentation|diagrams?|designs?|models?|"
    r"folders?|directory|directories|definitions?|fixtures?|snapshots?|changelogs?|guides?|"
    r"tutorials?)\b)"
)
_DB_ANY = rf"\b(?:{_DB_ENTERPRISE}|{_DB_COMMON})\b{_FILELIKE}"
_ENV = (
    r"(?:prod|production|pre ?prod|qa|uat|staging|live|company|corporate|enterprise|external|"
    r"remote|customer|vendor|third ?party|on ?prem(?:ise)?s?|legacy)"
)
_W = r"[a-z0-9]+"
_ENV_TARGET = rf"\b{_ENV} (?:{_W} ){{0,2}}?{_DB_ANY}"
_STRONG = rf"(?:{_ENV_TARGET}|\b{_DB_ENTERPRISE}\b{_FILELIKE})"


def _verbs(*bases: str, extra: tuple[str, ...] = ()) -> str:
    """Regex for the verb forms of each base ("migrate" -> migrate/migrates/migrated/migrating).

    Exact forms rather than ``stem\\w*`` so that nouns ("migration", "connector") do not match.
    """
    forms: set[str] = set(extra)
    for base in bases:
        head, _, tail = base.rpartition(" ")
        prefix = f"{head} " if head else ""
        word = tail
        forms.add(base)
        if word.endswith("e"):
            forms.update({base + "s", base + "d", prefix + word[:-1] + "ing"})
        elif word.endswith("y") and word[-2:-1] not in "aeiou":
            forms.update({prefix + word[:-1] + "ies", prefix + word[:-1] + "ied", base + "ing"})
        else:
            suffix = "es" if word.endswith(("s", "x", "ch", "sh", "z")) else "s"
            forms.update({base + suffix, base + "ed", base + "ing"})
            forms.update({base + word[-1] + "ed", base + word[-1] + "ing"})  # dropped, grabbing
    return "(?:" + "|".join(sorted(forms, key=len, reverse=True)) + ")"


def _gap(upper: int, stop: str) -> str:
    return rf"(?:(?!(?:{stop}) ){_W} ){{0,{upper}}}?"


_STOP_VERB = "then|because|which|that|but|if|when|while|so"
_STOP_TARGET = _STOP_VERB + "|to|for|by|with|using|via"
_PRE = _gap(8, _STOP_VERB)
_POST = _gap(4, _STOP_TARGET)
_NEAR = _gap(6, _STOP_TARGET)
_NEAR_TO = _gap(6, _STOP_VERB)
_SQLISH = (
    r"(?:sql|quer(?:y|ies)|statements?|ddl|dml|migrations?|scripts?|select|insert|update|delete|"
    r"alter|drop|truncate|stored procedures?|procs?|commands?|batch)"
)
_SOURCE = _verbs(
    "read", "fetch", "pull", "retrieve", "extract", "export", "download", "grab", "scrape",
    "stream", "replicate", "mirror", "sync", "copy", "ingest", "siphon", "harvest", "exfiltrate",
)  # fmt: skip
_SOURCE_WEAK = _verbs("get", "load", "import", "list", "show", "count", "find", "look up")
_WRITE = _verbs("write", extra=("wrote", "written"))
_SINK = _verbs(
    "insert", "upsert", "push", "apply", "deploy", "migrate", "truncate", "drop", "alter",
    "publish", "flush", "overwrite",
)  # fmt: skip
_SINK_WEAK = _verbs(
    "update", "delete", "save", "store", "persist", "send", "seed", "merge", "modify", "patch",
    "change", "edit", "fix", "correct", "remove", "load", "import", extra=("sent",),
)  # fmt: skip
_EXEC = _verbs("run", "execute", "exec", "submit", "fire", "replay", extra=("ran",))
_CONNECT_CORE = (
    rf"(?:{_verbs('connect')}(?: (?:to|with))?|{_verbs('log')} ?in(?:to| to)?|logins?(?: to)?|"
    rf"{_verbs('sign')} in(?:to| to)?|{_verbs('authenticate')}|"
    rf"{_verbs('open')} (?:a )?(?:connection|session)|{_verbs('attach')} to|{_verbs('tunnel')}|"
    rf"{_verbs('bridge')} to|{_verbs('talk')} to)"
)
_CONNECT = (
    rf"(?:{_CONNECT_CORE}|{_verbs('hook', 'point', 'plug', 'wire', 'link')} "
    rf"(?:{_W} ){{0,4}}?(?:up )?(?:to|at|into))"
)
_INSPECT_VERBS = _verbs(
    "check", "verify", "inspect", "compare", "reconcile", "validate", "diff", "examine",
    "investigate", "analyze", "analyse", "troubleshoot",
)  # fmt: skip
_INSPECT = rf"(?:{_INSPECT_VERBS}|{_verbs('look')} (?:at|into|in))"
_PROBE = _verbs(
    "query", "access", "dump", "introspect", "browse", "scrape", "crawl", "harvest", "exfiltrate",
    "siphon", "interrogate", extra=("queries",),
)  # fmt: skip
_DB_CLIENT = (
    r"(?:psql|sqlcmd|sqlplus|mongosh|redis cli|bcp|pg dump|pg restore|mysqldump|snowsql|cqlsh|"
    r"isql|impala shell|clickhouse client|(?:my ?sql|maria ?db) (?:client|cli))"
)
_CONNECTOR = (
    r"(?:connectors?|drivers?|adapters?|gateways?|prox(?:y|ies)|bridges?|readers?|writers?|"
    r"executors?|runners?|clients?|crawlers?|scrapers?|extractors?|fetchers?|pullers?|"
    r"importers?|exporters?|replicators?|credentials?|dsn|connection strings?)"
)
_BUILDER = _verbs(
    "build", "create", "implement", "write", "generate", "add", "develop", "provide", "set up",
    "need", "want", "make", "give", "install", "register", "propose", "wire up",
    extra=("built", "wrote"),
)  # fmt: skip
_PREP_FROM = r"(?:from|out of|off)"
_PREP_BARE = r"(?:to|into|onto|on|against)"
_PREP_ANY = r"(?:to|into|onto|on|against|in|inside|within|at)"
_PREP_SINK = r"(?:to|into|onto|on|against|in|inside|within|at|from)"
_PREP_INSPECT = r"(?:in|on|inside|within|against|at|from|of)"
_ENV_ONLY = r"(?:the |our |their |a |an )?(?:prod|production|qa|uat|pre ?prod|live)\b"
_NOT_A_SERVER = (
    r"(?! (?:branch|code|build|release|server|cluster|environment|env|deploy\w*|config\w*|"
    r"pipeline|bucket|app\w*|service\w*|site|url|host)\b)"
)

_INTENT_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p)
    for p in (
        # connect to / probe a database
        rf"\b{_CONNECT} {_NEAR_TO}{_DB_ANY}",
        rf"\b{_PROBE} {_NEAR}{_DB_ANY}",
        rf"\b{_CONNECT_CORE} (?:the |our |their |a |an )?(?:prod|production|qa|uat|pre ?prod)\b",
        # read from a database ("read orders directly from the company production Oracle db")
        rf"\b{_SOURCE} {_PRE}{_PREP_FROM} {_POST}{_DB_ANY}",
        rf"\b{_SOURCE_WEAK} {_PRE}{_PREP_FROM} {_POST}{_STRONG}",
        # write to a database
        rf"\b{_SINK} {_PRE}{_PREP_BARE} {_POST}{_DB_ANY}",
        rf"\b{_WRITE} {_PRE}(?:to|into|onto) {_POST}{_DB_ANY}",
        rf"\b(?:{_SINK}|{_WRITE}|{_SINK_WEAK}) {_PRE}{_PREP_SINK} {_POST}{_STRONG}",
        rf"\b{_INSPECT} {_PRE}{_PREP_INSPECT} {_POST}{_STRONG}",
        # execute against a database (SQL-ish payload, or an environment-qualified target)
        rf"\b{_EXEC} {_gap(3, _STOP_VERB)}{_SQLISH} {_gap(4, _STOP_VERB)}"
        rf"{_PREP_BARE} {_POST}{_DB_ANY}",
        rf"\b{_EXEC} {_PRE}{_PREP_BARE} {_POST}{_ENV_TARGET}",
        rf"\b(?:{_EXEC}|{_SINK}) {_gap(4, _STOP_VERB)}{_SQLISH} {_gap(4, _STOP_VERB)}"
        rf"{_PREP_ANY} {_ENV_ONLY}{_NOT_A_SERVER}",
        rf"\bselect {_gap(6, _STOP_VERB)}from {_gap(4, _STOP_VERB)}"
        rf"(?:on|against|in|at) {_ENV_ONLY}{_NOT_A_SERVER}",
        # database command-line clients
        rf"\b(?:run|use|using|launch|open|invoke|execute|start|via|through|with|spawn|call) "
        rf"(?:the |a |an )?{_DB_CLIENT}\b",
        # connector-like noun phrases: "external db connector", "oracle reader"
        rf"{_STRONG} (?:{_W} )?{_CONNECTOR}\b",
        rf"\b{_BUILDER} {_gap(4, _STOP_VERB)}{_DB_ANY} (?:{_W} )?"
        r"(?:connectors?|drivers?|adapters?|gateways?|prox(?:y|ies)|bridges?|executors?|"
        r"crawlers?|scrapers?|extractors?|replicators?)\b",
    )
)
# Wording that says the SQL is only *text*: producing it for a human to run is allowed.
_TEXT_ONLY = re.compile(
    r"\b(?:as text|text only|sql text|from (?:a )?(?:file|script|text)|for a human|"
    r"human (?:will )?(?:run|execute))\b",
    re.IGNORECASE,
)
# The original detector, kept verbatim and applied to the raw text so that widening the
# detector can never unblock anything it used to block.
_LEGACY_DB_NOUN = (
    r"(?:data ?bases?|\bdbs?\b|sql ?server|oracle|postgres(?:ql)?|mysql|maria ?db|mongo(?:db)?|"
    r"redis|cassandra|snowflake|data ?warehouse|rdbms)"
)
_LEGACY_DB_VERB = (
    r"(?:connect(?:s|ing)?(?: to)?|log ?in(?:to)?|open(?:ing)? a connection|"
    r"execut\w+ (?:sql )?(?:against|on)|"
    r"run(?:ning)? (?:\w+ ){0,3}(?:against|on)|query(?:ing)?|read(?:ing)? from|writ(?:e|ing) to|"
    r"apply(?:ing)? (?:\w+ ){0,3}to|deploy(?:ing)? (?:\w+ ){0,3}to|dump(?:ing)?|access(?:ing)?)"
)
_LEGACY_INTENT = re.compile(
    rf"{_LEGACY_DB_VERB}\W+(?:\w+\W+){{0,6}}?{_LEGACY_DB_NOUN}", re.IGNORECASE
)
# Wording that says nobody is in the loop: it voids the text-only exemption.
_UNATTENDED = re.compile(
    r"\b(?:directly|straight|automatic(?:ally)?|autonomous(?:ly)?|yourself|itself|on its own|"
    r"without (?:a )?(?:human|review|approval|asking|confirmation)|no human)\b"
)
_CLAUSE_BREAK = re.compile(
    r" (?:and then|and also|then|but|afterwards?|after that|while|whereas|also|plus|however|"
    r"instead|meanwhile) | and (?=(?:connect|log|sign|run|execut|quer|read|fetch|pull|appl|"
    r"deploy|dump|access|writ|load|extract|export|sync|migrat)\w* )"
)
_SENTENCE = re.compile(r"(?<=[.!?;:])\s+|\n\s*\n")
# Cyrillic and Greek look-alikes of Latin letters, by code point (ASCII-only source on purpose).
_CONFUSABLES: dict[int, str] = {
    0x430: "a", 0x435: "e", 0x43E: "o", 0x440: "p", 0x441: "c", 0x443: "y", 0x445: "x",
    0x456: "i", 0x458: "j", 0x455: "s", 0x501: "d", 0x410: "A", 0x412: "B", 0x415: "E",
    0x41A: "K", 0x41C: "M", 0x41D: "H", 0x41E: "O", 0x420: "P", 0x421: "C", 0x422: "T",
    0x425: "X", 0x3BF: "o", 0x3B1: "a",
}  # fmt: skip
_MAX_SCANNED = 200_000


def _words(text: str, *, split_camel: bool) -> str:
    if split_camel:
        text = _CAMEL_ACRONYM.sub(r"\1 \2", _CAMEL_LOWER_UPPER.sub(r"\1 \2", text))
    return " ".join(re.findall(r"[a-z0-9]+", text.lower()))


def find_external_database_intent(text: str) -> str | None:
    """The excerpt (as normalised words) that reads as live external-database access, if any.

    Advisory and heuristic: it exists so that no workshop is started and no workflow runs at
    normal risk for such a need. The real prevention is structural (no database drivers, no
    database ports or clients, network-isolated sandbox, Policy Engine). Wording such as "as
    text for a human" is accepted because producing SQL text for a human to run is explicitly
    allowed, unless the same clause says it happens unattended ("directly", "automatically").
    """
    legacy = _LEGACY_INTENT.search(text)
    if legacy is not None and not _TEXT_ONLY.search(text):
        return " ".join(legacy.group(0).lower().split())[:160]
    if len(text) > _MAX_SCANNED:  # fail closed: padding must not push intent out of view
        return "request too large to screen"
    raw = unicodedata.normalize("NFKC", text)
    raw = "".join(ch for ch in raw if unicodedata.category(ch) != "Cf").translate(_CONFUSABLES)
    for sentence in _SENTENCE.split(raw):
        for split_camel in (True, False):
            words = _words(sentence, split_camel=split_camel)
            breaks = [(m.start(), m.end()) for m in _CLAUSE_BREAK.finditer(words)]
            for pattern in _INTENT_PATTERNS:
                for match in pattern.finditer(words):
                    lo = max((end for _, end in breaks if end <= match.start()), default=0)
                    hi = min(
                        (start for start, _ in breaks if start >= match.end()), default=len(words)
                    )
                    clause = words[lo:hi]
                    if _TEXT_ONLY.search(clause) and not _UNATTENDED.search(clause):
                        continue
                    return match.group(0)[:160]
    return None


def detects_external_database_intent(text: str) -> bool:
    """True when a request reads as wanting a live connection to an external database."""
    return find_external_database_intent(text) is not None


class RegistryState(StrEnum):
    UNREGISTERED = "UNREGISTERED"
    EXPERIMENTAL = "EXPERIMENTAL"
    VERIFIED = "VERIFIED"
    TRUSTED = "TRUSTED"
    DISABLED = "DISABLED"
    BROKEN = "BROKEN"
    QUARANTINED = "QUARANTINED"


class Origin(StrEnum):
    BUILTIN = "builtin"  # shipped and reviewed with the release
    PLUGIN = "plugin"  # supplied by the owner in a plugin directory
    GENERATED = "generated"  # produced by the Capability Workshop
    DOWNLOADED = "downloaded"  # onboarded from the internet via the Workshop


class Maturity(StrEnum):
    EXPERIMENTAL = "experimental"
    BETA = "beta"
    STABLE = "stable"


ROUTABLE_STATES = frozenset(
    {RegistryState.EXPERIMENTAL, RegistryState.VERIFIED, RegistryState.TRUSTED}
)
_RANK = {RegistryState.EXPERIMENTAL: 1, RegistryState.VERIFIED: 2, RegistryState.TRUSTED: 3}

# The highest state a freshly loaded manifest may claim, by origin. Only code that ships with the
# release can declare itself TRUSTED; everything else starts EXPERIMENTAL and has to earn more.
ORIGIN_STATE_CEILING: dict[Origin, RegistryState] = {
    Origin.BUILTIN: RegistryState.TRUSTED,
    Origin.PLUGIN: RegistryState.EXPERIMENTAL,
    Origin.GENERATED: RegistryState.EXPERIMENTAL,
    Origin.DOWNLOADED: RegistryState.EXPERIMENTAL,
}
APPROVAL_REQUIRED_ORIGINS = frozenset({Origin.GENERATED, Origin.DOWNLOADED})


def state_rank(state: RegistryState) -> int:
    return _RANK.get(state, 0)


def clamp_state(declared: RegistryState, origin: Origin) -> RegistryState:
    """A manifest cannot claim more trust than its origin allows."""
    ceiling = ORIGIN_STATE_CEILING[origin]
    if declared in ROUTABLE_STATES and state_rank(declared) > state_rank(ceiling):
        return ceiling
    return declared


def is_routable(state: RegistryState, origin: Origin, *, approved: bool) -> bool:
    """Generated and downloaded executables are not routable until a human approved them."""
    if state not in ROUTABLE_STATES:
        return False
    return not (origin in APPROVAL_REQUIRED_ORIGINS and not approved)


class TransitionError(ValueError):
    """A registry state change that the rules do not allow."""


def check_transition(
    current: RegistryState,
    target: RegistryState,
    origin: Origin,
    *,
    human_approved: bool = False,
    verification_passed: bool = False,
) -> None:
    """Raise :class:`TransitionError` unless the state change is allowed.

    * Demotions (DISABLED, BROKEN, QUARANTINED) are always allowed - safety first.
    * EXPERIMENTAL -> VERIFIED needs passed verification (tests/evaluation).
    * VERIFIED -> TRUSTED needs a human decision.
    * Leaving QUARANTINED needs a human decision.
    * Generated/downloaded things cannot exceed EXPERIMENTAL without human approval.
    * BROKEN/DISABLED -> routable states need verification (health/tests) again.
    """
    if target is current:
        return
    if target in {RegistryState.DISABLED, RegistryState.BROKEN, RegistryState.QUARANTINED}:
        return
    if current is RegistryState.QUARANTINED and not human_approved:
        raise TransitionError("leaving QUARANTINED requires a human decision")
    if target is RegistryState.UNREGISTERED:
        raise TransitionError("cannot move back to UNREGISTERED; disable it instead")
    needs_approval = (
        target in ROUTABLE_STATES
        and origin in APPROVAL_REQUIRED_ORIGINS
        and not human_approved
        and (target is not RegistryState.EXPERIMENTAL or current is RegistryState.UNREGISTERED)
    )
    if needs_approval:
        raise TransitionError(
            f"{origin.value} artifacts need human approval to become {target.value}"
        )
    if target is RegistryState.VERIFIED and state_rank(current) < 2 and not verification_passed:
        raise TransitionError("promotion to VERIFIED needs passed verification")
    if target is RegistryState.TRUSTED and not human_approved:
        raise TransitionError("promotion to TRUSTED needs a human decision")
    re_enabling = target is RegistryState.EXPERIMENTAL and current in {
        RegistryState.BROKEN,
        RegistryState.DISABLED,
    }
    if re_enabling and not verification_passed and not human_approved:
        raise TransitionError("re-enabling requires a passing health check or a human decision")
