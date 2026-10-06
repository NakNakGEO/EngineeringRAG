"""Registry domain rules: states, origins, trust ceilings, routability (pure).

Capabilities are semantic needs; providers (tools/adapters/subsystems) implement them; agents
are reasoning roles; skills are reusable procedures. Everything is registered, versioned and
state-controlled. An unregistered or unapproved thing is never routable.
"""

from __future__ import annotations

import re
from enum import StrEnum

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
    "root_policy",
    "policy_modify",
)

ID_PATTERN = re.compile(r"^[a-z][a-z0-9_.-]{1,79}$")
VERSION_PATTERN = re.compile(r"^\d{1,4}\.\d{1,4}\.\d{1,4}(?:[-+][0-9A-Za-z.-]+)?$")


_DB_NOUN = (
    r"(?:data ?bases?|\bdbs?\b|sql ?server|oracle|postgres(?:ql)?|mysql|maria ?db|mongo(?:db)?|"
    r"redis|cassandra|snowflake|data ?warehouse|rdbms)"
)
_DB_VERB = (
    r"(?:connect(?:s|ing)?(?: to)?|log ?in(?:to)?|open(?:ing)? a connection|"
    r"execut\w+ (?:sql )?(?:against|on)|"
    r"run(?:ning)? (?:\w+ ){0,3}(?:against|on)|query(?:ing)?|read(?:ing)? from|writ(?:e|ing) to|"
    r"apply(?:ing)? (?:\w+ ){0,3}to|deploy(?:ing)? (?:\w+ ){0,3}to|dump(?:ing)?|access(?:ing)?)"
)
_EXTERNAL_DB_INTENT = re.compile(rf"{_DB_VERB}\W+(?:\w+\W+){{0,6}}?{_DB_NOUN}", re.IGNORECASE)
_TEXT_ONLY = re.compile(
    r"\b(?:as text|text only|sql text|from (?:a )?(?:file|script|text)|for a human|"
    r"human (?:will )?(?:run|execute))\b",
    re.IGNORECASE,
)


def detects_external_database_intent(text: str) -> bool:
    """Heuristic gate for *requests* that read as wanting a live database connection.

    This is only an early, advisory gate (so a workshop is never even started for such a need).
    The real prevention is structural: no database drivers, no database ports, no database clients,
    network-isolated sandbox, and the Policy Engine. Wording such as "as text for a human" is
    accepted because producing SQL text for a human to run is explicitly allowed.
    """
    match = _EXTERNAL_DB_INTENT.search(text)
    if match is None:
        return False
    return not _TEXT_ONLY.search(text)


def is_forbidden_capability(capability_id: str) -> bool:
    lowered = capability_id.lower()
    return lowered in FORBIDDEN_CAPABILITIES or lowered.startswith(FORBIDDEN_CAPABILITY_PREFIXES)


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
