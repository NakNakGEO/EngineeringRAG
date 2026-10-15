"""Knowledge maintenance job: catches what event-driven governance missed."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine

from eios_domain.events import EventType
from eios_domain.knowledge import Health
from eios_governance.service import GovernanceService
from eios_knowledge.knowledge_repo import _ITEM_COLUMNS, row_to_item
from eios_observability.recorder import RunContext
from eios_storage.tables.governance import dependency as dependency_t
from eios_storage.tables.knowledge import item as item_t

MAINTAIN_JOB = "knowledge.maintain"
MAX_ITEMS = 5000


@dataclass
class MaintenanceReport:
    items_examined: int = 0
    contradictions_found: int = 0
    stale_marked: int = 0
    projects_checked: int = 0


class MaintenanceService:
    def __init__(self, engine: AsyncEngine, governance: GovernanceService) -> None:
        self._engine = engine
        self._gov = governance

    async def run(
        self, project_id: uuid.UUID | None = None, ctx: RunContext | None = None
    ) -> MaintenanceReport:
        report = MaintenanceReport()
        live = [Health.CURRENT.value, Health.UNVERIFIED.value, Health.STALE.value]
        stmt = (
            sa.select(*_ITEM_COLUMNS)
            .where(item_t.c.health.in_(live), item_t.c.superseded_by.is_(None))
            .order_by(item_t.c.updated_at.desc())
            .limit(MAX_ITEMS)
        )
        if project_id is not None:
            stmt = stmt.where(item_t.c.project_id == project_id)
        async with self._engine.connect() as conn:
            items = [row_to_item(r) for r in (await conn.execute(stmt)).all()]
            dep_rows = (
                await conn.execute(
                    sa.select(dependency_t.c.project_id, dependency_t.c.item_id)
                    .where(dependency_t.c.dep_kind == "file")
                    .distinct()
                )
            ).all()
        report.items_examined = len(items)
        report.contradictions_found = await self._gov.detect_contradictions(
            items, actor="maintenance"
        )
        by_project: dict[uuid.UUID, list[uuid.UUID]] = {}
        for row in dep_rows:
            if row.project_id is not None and (project_id is None or row.project_id == project_id):
                by_project.setdefault(row.project_id, []).append(row.item_id)
        for pid, item_ids in by_project.items():
            report.projects_checked += 1
            report.stale_marked += len((await self._gov.recheck_dependencies(pid, item_ids)).stale)
        if ctx is not None:
            await ctx.emit(
                EventType.KNOWLEDGE_UPDATED, "knowledge maintenance finished",
                data={"examined": report.items_examined,
                      "contradictions": report.contradictions_found,
                      "stale": report.stale_marked},
            )  # fmt: skip
        return report
