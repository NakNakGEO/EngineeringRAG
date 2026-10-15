"""Decision ledger lifecycle: PROPOSED -> ACCEPTED | REJECTED, ACCEPTED -> SUPERSEDED.

Anyone (including a model) may *propose*; only a named human accepts or rejects. Every transition
is recorded. Rejected and superseded decisions are historical and immutable.
"""

from __future__ import annotations

import uuid
from typing import Any

from eios_domain.errors import DomainError, NotFoundError
from eios_domain.knowledge import Decision, DecisionStatus, HumanApproval
from eios_governance.store import GovernanceStore
from eios_knowledge import DecisionRepository


class DecisionLifecycle:
    def __init__(self, repo: DecisionRepository, store: GovernanceStore, engine: Any) -> None:
        self._repo = repo
        self._store = store
        self._engine = engine

    async def _get(self, decision_id: uuid.UUID) -> Decision:
        decision = await self._repo.get(decision_id)
        if decision is None:
            raise NotFoundError(f"decision {decision_id} not found")
        return decision

    async def _record(
        self, d: Decision, to: str, human: HumanApproval, reason: str, change: str = "status"
    ) -> None:
        async with self._engine.begin() as conn:
            await self._store.record(
                conn, subject_kind="decision", subject_id=d.id, change=change,
                from_value=d.status.value, to_value=to, actor=human.approver, reason=reason,
                approval=human.model_dump(mode="json"),
            )  # fmt: skip

    async def accept(
        self, decision_id: uuid.UUID, human: HumanApproval, reason: str = ""
    ) -> Decision:
        d = await self._get(decision_id)
        if d.status is not DecisionStatus.PROPOSED:
            raise DomainError(f"only a PROPOSED decision can be accepted (it is {d.status.value})")
        if not d.selected.strip():
            raise DomainError("a decision needs a selected option before it can be accepted")
        out = await self._repo.set_status(decision_id, DecisionStatus.ACCEPTED)
        await self._record(d, DecisionStatus.ACCEPTED.value, human, reason)
        return out

    async def reject(self, decision_id: uuid.UUID, human: HumanApproval, reason: str) -> Decision:
        d = await self._get(decision_id)
        if d.status is not DecisionStatus.PROPOSED:
            raise DomainError(f"only a PROPOSED decision can be rejected (it is {d.status.value})")
        if not reason.strip():
            raise DomainError("rejecting a decision needs a reason")
        out = await self._repo.set_status(decision_id, DecisionStatus.REJECTED)
        await self._record(d, DecisionStatus.REJECTED.value, human, reason)
        return out

    async def supersede(
        self, old_id: uuid.UUID, new_id: uuid.UUID, human: HumanApproval, reason: str = ""
    ) -> Decision:
        old, new = await self._get(old_id), await self._get(new_id)
        if old.status is not DecisionStatus.ACCEPTED:
            raise DomainError("only an ACCEPTED decision can be superseded")
        if new.status is not DecisionStatus.ACCEPTED:
            raise DomainError("the superseding decision must itself be ACCEPTED")
        if (old.vault, old.project_id) != (new.vault, new.project_id):
            raise DomainError("supersession must stay within one vault/project scope")
        chain, cursor = {old_id}, new
        while cursor.superseded_by is not None and cursor.superseded_by not in chain:
            if cursor.superseded_by == old_id:
                raise DomainError("supersession would create a cycle")
            chain.add(cursor.superseded_by)
            cursor = await self._get(cursor.superseded_by)
        out = await self._repo.supersede(old_id, new_id)
        await self._record(old, f"superseded_by:{new_id}", human, reason, change="supersede")
        return out

    async def history(self, decision_id: uuid.UUID) -> list[dict[str, Any]]:
        return await self._store.history("decision", decision_id)
