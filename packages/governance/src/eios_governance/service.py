"""Knowledge governance: trust, health, supersession, contradictions, dependency invalidation."""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field

from eios_domain.errors import DomainError, NotFoundError
from eios_domain.events import EventType
from eios_domain.governance import EvidenceFact, check_health_change, check_trust_change
from eios_domain.knowledge import (
    Health,
    HumanApproval,
    KnowledgeItem,
    Trust,
    TrustViolationError,
)
from eios_domain.vault import Vault
from eios_governance.store import GovernanceStore
from eios_knowledge import EvidenceRepository, KnowledgeRepository
from eios_observability.recorder import RunContext

# (project_id) -> {path: content hash} of the current committed index; None = not indexed.
CommittedFiles = Callable[[uuid.UUID], Awaitable[dict[str, str] | None]]
_LIVE = frozenset({Health.CURRENT, Health.UNVERIFIED, Health.STALE})
_NO_INVALIDATION = frozenset(
    {Health.SUPERSEDED, Health.HISTORICAL, Health.QUARANTINED, Health.STALE}
)


def _approval(human: HumanApproval | None) -> dict[str, object] | None:
    return human.model_dump(mode="json") if human else None


@dataclass
class InvalidationReport:
    stale: list[uuid.UUID] = field(default_factory=list)
    skipped: int = 0


class GovernanceService:
    def __init__(
        self,
        store: GovernanceStore,
        knowledge: KnowledgeRepository,
        evidence: EvidenceRepository,
        *,
        committed_files: CommittedFiles | None = None,
    ) -> None:
        self.store = store
        self._knowledge = knowledge
        self._evidence = evidence
        self._committed = committed_files

    async def _item(self, item_id: uuid.UUID) -> KnowledgeItem:
        item = await self._knowledge.get(item_id)
        if item is None:
            raise NotFoundError(f"knowledge item {item_id} not found")
        return item

    # -- trust --
    async def promote_trust(
        self,
        item_id: uuid.UUID,
        target: Trust,
        *,
        actor: str,
        reason: str = "",
        evidence_ids: Sequence[uuid.UUID] = (),
        human: HumanApproval | None = None,
        ctx: RunContext | None = None,
    ) -> KnowledgeItem:
        """Change an item's trust. Raises :class:`TrustViolationError` if it is not justified."""
        item = await self._item(item_id)
        records = await self._evidence.get_many(list(evidence_ids)) if evidence_ids else []
        if len(records) != len(set(evidence_ids)):
            raise TrustViolationError("some referenced evidence does not exist")
        facts = [
            EvidenceFact(
                source_kind=r.source_kind,
                in_scope=(r.project_id == item.project_id)
                if item.vault is Vault.PROJECT
                else (r.project_id is None),
                has_content_hash=bool(r.content_hash),
            )
            for r in records
        ]
        check_trust_change(item.trust, target, evidence=facts, human=human)
        if target is item.trust:
            return item
        values: dict[str, object] = {"trust": target.value}
        if target <= Trust.RAW and item.health is Health.CURRENT:
            values["health"] = Health.UNVERIFIED.value  # RAW can never be CURRENT
        applied = await self.store.apply_item_change(
            item_id,
            change="trust",
            values=values,
            from_value=item.trust.value,
            to_value=target.value,
            actor=actor,
            reason=reason,
            evidence_ids=evidence_ids,
            approval=_approval(human),
            expected={"trust": item.trust.value},
        )
        if not applied:
            raise DomainError("the item changed concurrently; re-read and retry")
        if ctx is not None:
            await ctx.emit(
                EventType.KNOWLEDGE_UPDATED,
                f"trust {item.trust.value} -> {target.value}",
                data={
                    "item_id": str(item_id),
                    "from": item.trust.value,
                    "to": target.value,
                    "evidence": [str(e) for e in evidence_ids],
                    "human": human is not None,
                },
            )
        return await self._item(item_id)

    # -- health --
    async def set_health(
        self,
        item_id: uuid.UUID,
        target: Health,
        *,
        actor: str,
        reason: str,
        human: HumanApproval | None = None,
        ctx: RunContext | None = None,
    ) -> KnowledgeItem:
        item = await self._item(item_id)
        check_health_change(item.health, target, item.trust, reason=reason, human=human)
        if target is item.health:
            return item
        if target is Health.CURRENT and await self.store.open_contradictions_for(item_id):
            raise TrustViolationError("an item with an open contradiction cannot be CURRENT")
        applied = await self.store.apply_item_change(
            item_id,
            change="health",
            values={"health": target.value},
            from_value=item.health.value,
            to_value=target.value,
            actor=actor,
            reason=reason,
            approval=_approval(human),
            expected={"health": item.health.value},
        )
        if not applied:
            raise DomainError("the item changed concurrently; re-read and retry")
        if ctx is not None:
            await ctx.emit(
                EventType.KNOWLEDGE_UPDATED,
                f"health {item.health.value} -> {target.value}",
                data={
                    "item_id": str(item_id),
                    "from": item.health.value,
                    "to": target.value,
                    "reason": reason[:200],
                },
            )
        return await self._item(item_id)

    # -- supersession --
    async def supersede(
        self,
        old_id: uuid.UUID,
        new_id: uuid.UUID,
        *,
        actor: str,
        reason: str,
        human: HumanApproval | None = None,
        ctx: RunContext | None = None,
    ) -> KnowledgeItem:
        if old_id == new_id:
            raise DomainError("an item cannot supersede itself")
        old, new = await self._item(old_id), await self._item(new_id)
        if (old.vault, old.project_id) != (new.vault, new.project_id):
            raise DomainError("supersession must stay within one vault/project scope")
        if old.superseded_by is not None or old.health is Health.SUPERSEDED:
            raise DomainError("the item is already superseded")
        if new.health in {Health.SUPERSEDED, Health.QUARANTINED}:
            raise DomainError(f"a {new.health.value} item cannot be a successor")
        cursor, seen = new, {new.id}
        while cursor.superseded_by is not None and cursor.superseded_by not in seen:
            if cursor.superseded_by == old_id:
                raise DomainError("supersession would create a cycle")
            seen.add(cursor.superseded_by)
            cursor = await self._item(cursor.superseded_by)
        if old.trust is Trust.APPROVED and human is None:
            raise TrustViolationError("superseding APPROVED knowledge requires a human decision")
        applied = await self.store.apply_item_change(
            old_id,
            change="supersede",
            values={"health": Health.SUPERSEDED.value, "superseded_by": new_id},
            from_value=old.health.value,
            to_value=f"superseded_by:{new_id}",
            actor=actor,
            reason=reason,
            approval=_approval(human),
            expected={"health": old.health.value},
        )
        if not applied:
            raise DomainError("the item changed concurrently; re-read and retry")
        if ctx is not None:
            await ctx.emit(
                EventType.KNOWLEDGE_UPDATED,
                "knowledge superseded",
                data={"old": str(old_id), "new": str(new_id), "reason": reason[:200]},
            )
        return await self._item(old_id)

    # -- contradictions --
    async def report_contradiction(
        self,
        item_a: uuid.UUID,
        item_b: uuid.UUID,
        *,
        reason: str,
        actor: str,
        ctx: RunContext | None = None,
    ) -> uuid.UUID | None:
        """Record a contradiction and mark the less trusted side CONTRADICTED (both if equal)."""
        a, b = await self._item(item_a), await self._item(item_b)
        if (a.vault, a.project_id) != (b.vault, b.project_id):
            raise DomainError("contradictions are tracked within one vault/project scope")
        cid = await self.store.add_contradiction(a.id, b.id, reason, actor)
        if cid is None:
            return None  # already known
        losers = [a] if a.trust < b.trust else [b] if b.trust < a.trust else [a, b]
        for item in losers:
            if item.health in _LIVE:
                await self.store.apply_item_change(
                    item.id,
                    change="contradiction",
                    values={"health": Health.CONTRADICTED.value},
                    from_value=item.health.value,
                    to_value=Health.CONTRADICTED.value,
                    actor=actor,
                    reason=f"contradicts {b.id if item is a else a.id}: {reason}"[:500],
                    expected={"health": item.health.value},
                )
        if ctx is not None:
            await ctx.emit(
                EventType.KNOWLEDGE_UPDATED,
                "contradiction recorded",
                data={"contradiction_id": str(cid), "items": [str(a.id), str(b.id)]},
            )
        return cid

    async def resolve_contradiction(
        self,
        contradiction_id: uuid.UUID,
        *,
        winner: uuid.UUID,
        human: HumanApproval,
        resolution: str,
    ) -> None:
        """A human picks the winner; the loser is superseded by it. Never automatic."""
        rows = [
            c
            for c in await self.store.contradictions(status="open", limit=1000)
            if c["id"] == contradiction_id
        ]
        if not rows:
            raise NotFoundError("no such open contradiction")
        pair = (rows[0]["item_a"], rows[0]["item_b"])
        if winner not in pair:
            raise DomainError("the winner must be one of the contradicting items")
        loser = pair[1] if winner == pair[0] else pair[0]
        # the loser's health is CONTRADICTED: supersede is a human act anyway
        await self.supersede(
            loser,
            winner,
            actor=human.approver,
            reason=f"contradiction resolved: {resolution}",
            human=human,
        )
        await self.store.resolve_contradiction(contradiction_id, human.approver, resolution)
        w = await self._item(winner)
        if w.health is Health.CONTRADICTED and not await self.store.open_contradictions_for(winner):
            await self.set_health(
                winner,
                Health.CURRENT,
                actor=human.approver,
                reason=f"contradiction resolved: {resolution}",
                human=human,
            )

    async def detect_contradictions(self, items: Sequence[KnowledgeItem], *, actor: str) -> int:
        """Items sharing a ``subject_key`` in one scope but saying different things."""
        groups: dict[tuple[str, uuid.UUID | None, str], list[KnowledgeItem]] = {}
        for item in items:
            if item.subject_key and item.health in _LIVE and item.superseded_by is None:
                groups.setdefault((item.vault.value, item.project_id, item.subject_key), []).append(
                    item
                )
        found = 0
        for (_, _, subject), group in groups.items():
            digests = {
                hashlib.sha256(" ".join(i.content.split()).lower().encode()).hexdigest()
                for i in group
            }
            if len(group) < 2 or len(digests) < 2:
                continue
            group.sort(key=lambda i: (i.trust.rank, i.updated_at), reverse=True)
            top = group[0]
            for other in group[1:]:
                if (
                    hashlib.sha256(" ".join(other.content.split()).lower().encode()).hexdigest()
                    == hashlib.sha256(" ".join(top.content.split()).lower().encode()).hexdigest()
                ):
                    continue
                if await self.report_contradiction(
                    top.id,
                    other.id,
                    reason=f"same subject '{subject}' with different content",
                    actor=actor,
                ):
                    found += 1
        return found

    # -- dependencies & invalidation --
    async def link_dependencies(
        self,
        item_id: uuid.UUID,
        *,
        files: Sequence[tuple[str, str | None]] = (),
        symbols: Sequence[str] = (),
        items: Sequence[uuid.UUID] = (),
    ) -> int:
        item = await self._item(item_id)
        if item.vault is not Vault.PROJECT and (files or symbols):
            raise DomainError("code dependencies belong to project-vault knowledge")
        deps: list[tuple[str, str, str | None]] = [("file", p, h) for p, h in files]
        deps += [("symbol", s, None) for s in symbols]
        deps += [("item", str(i), None) for i in items if i != item_id]
        return await self.store.set_dependencies(item_id, item.project_id, deps)

    async def invalidate_for_changes(
        self,
        project_id: uuid.UUID,
        *,
        changed: Sequence[str],
        removed: Sequence[str],
        actor: str = "governance",
        ctx: RunContext | None = None,
    ) -> InvalidationReport:
        """Mark knowledge that depends on changed/removed files STALE (and its dependents)."""
        report = InvalidationReport()
        queue_keys = list({*changed, *removed})
        why = dict.fromkeys(removed, "removed") | {
            k: "changed" for k in changed if k not in set(removed)
        }
        affected = await self.store.dependents_of(project_id, "file", queue_keys)
        frontier: list[tuple[uuid.UUID, str]] = []
        seen: set[uuid.UUID] = set()
        for row in affected:
            frontier.append((row["id"], f"file '{row['dep_key']}' {why[row['dep_key']]}"))
        hops = 0
        while frontier and hops < 5:
            next_frontier: list[tuple[uuid.UUID, str]] = []
            for item_id, reason in frontier:
                if item_id in seen:
                    continue
                seen.add(item_id)
                item = await self._knowledge.get(item_id)
                if item is None or item.health in _NO_INVALIDATION:
                    report.skipped += 1
                    continue
                if await self.store.apply_item_change(
                    item_id,
                    change="health",
                    values={"health": Health.STALE.value},
                    from_value=item.health.value,
                    to_value=Health.STALE.value,
                    actor=actor,
                    reason=reason,
                    expected={"health": item.health.value},
                ):
                    report.stale.append(item_id)
                    for dep in await self.store.dependents_of(project_id, "item", [str(item_id)]):
                        next_frontier.append((dep["id"], f"depends on stale item {item_id}"))
            frontier = next_frontier
            hops += 1
        if ctx is not None and report.stale:
            await ctx.emit(
                EventType.KNOWLEDGE_UPDATED,
                f"{len(report.stale)} knowledge items marked stale",
                data={"project_id": str(project_id), "stale": [str(i) for i in report.stale[:50]]},
            )
        return report

    async def recheck_dependencies(
        self, project_id: uuid.UUID, items: Sequence[uuid.UUID]
    ) -> InvalidationReport:
        """Compare recorded file hashes with the current index (catches missed sync events)."""
        report = InvalidationReport()
        if self._committed is None:
            return report
        current = await self._committed(project_id)
        if current is None:
            return report
        changed: list[str] = []
        removed: list[str] = []
        for item_id in items:
            for dep in await self.store.dependencies(item_id):
                if dep["dep_kind"] != "file":
                    continue
                now = current.get(dep["dep_key"])
                if now is None:
                    removed.append(dep["dep_key"])
                elif dep["dep_hash"] and dep["dep_hash"] != now:
                    changed.append(dep["dep_key"])
        if changed or removed:
            return await self.invalidate_for_changes(
                project_id, changed=changed, removed=removed, actor="maintenance"
            )
        return report
