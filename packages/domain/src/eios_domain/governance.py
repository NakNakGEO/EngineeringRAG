"""Knowledge governance rules (pure): who may move trust and health, and on what grounds.

The principle: an assertion cannot vouch for itself. VERIFIED needs evidence that did not come
from a model (tool/code output) or a human; APPROVED is human-only; demotions are always allowed.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from eios_domain.knowledge import Health, HumanApproval, SourceKind, Trust, TrustViolationError

INDEPENDENT_SOURCES = frozenset({SourceKind.TOOL, SourceKind.CODE})  # not model-generated
CORROBORATING_SOURCES = frozenset(
    {SourceKind.TOOL, SourceKind.CODE, SourceKind.RESEARCH, SourceKind.MANUAL, SourceKind.IMPORT}
)


@dataclass(frozen=True)
class EvidenceFact:
    """What the rules need to know about one referenced evidence record."""

    source_kind: SourceKind
    in_scope: bool  # same project (or default vault) as the knowledge item
    has_content_hash: bool = True


def usable(evidence: Sequence[EvidenceFact], sources: frozenset[SourceKind]) -> int:
    return sum(
        1 for e in evidence if e.in_scope and e.has_content_hash and e.source_kind in sources
    )


def check_trust_change(
    current: Trust,
    target: Trust,
    *,
    evidence: Sequence[EvidenceFact] = (),
    human: HumanApproval | None = None,
) -> None:
    """Raise :class:`TrustViolationError` unless ``current -> target`` is justified."""
    if target <= current:
        return  # demotion or no-op: always allowed
    if target is Trust.APPROVED:
        if human is None:
            raise TrustViolationError("APPROVED requires an explicit human approval")
        return
    if target is Trust.VERIFIED:
        if human is not None:
            return
        if usable(evidence, INDEPENDENT_SOURCES) < 1:
            raise TrustViolationError(
                "VERIFIED requires independent evidence (tool or code output) or a human "
                "verification; an assertion cannot verify itself"
            )
        return
    if target is Trust.DERIVED:
        if human is None and usable(evidence, CORROBORATING_SOURCES) < 1:
            raise TrustViolationError("DERIVED requires at least one non-model evidence record")
        return
    if target is Trust.OBSERVED and human is None and not any(e.in_scope for e in evidence):
        raise TrustViolationError("OBSERVED requires at least one in-scope evidence record")


_TERMINAL = frozenset({Health.SUPERSEDED, Health.HISTORICAL})


def check_health_change(
    current: Health,
    target: Health,
    trust: Trust,
    *,
    reason: str,
    human: HumanApproval | None = None,
    superseded_by_set: bool = False,
) -> None:
    if target is current:
        return
    if target is Health.SUPERSEDED and not superseded_by_set:
        raise TrustViolationError("SUPERSEDED is only set by supersession (needs a successor)")
    if target is Health.QUARANTINED and not reason.strip():
        raise TrustViolationError("quarantine needs a reason")
    if current is Health.QUARANTINED and human is None:
        raise TrustViolationError("leaving QUARANTINED requires a human decision")
    if current in _TERMINAL and human is None:
        raise TrustViolationError(
            f"{current.value} knowledge is historical; reinstating needs a human"
        )
    if target is Health.CURRENT:
        if trust <= Trust.RAW:
            raise TrustViolationError("RAW knowledge cannot be CURRENT")
        if current is Health.CONTRADICTED and human is None:
            raise TrustViolationError("resolving a contradiction as CURRENT needs a human decision")
    if not reason.strip() and target not in {Health.CURRENT, Health.UNVERIFIED}:
        raise TrustViolationError("a health change away from CURRENT/UNVERIFIED needs a reason")
