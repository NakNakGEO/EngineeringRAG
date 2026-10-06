"""SQL persistence for the registries."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from pydantic import BaseModel
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncEngine

from eios_capability.manifests import (
    AgentManifest,
    CapabilityManifest,
    SkillManifest,
    ToolManifest,
)
from eios_domain.ids import new_id, utcnow
from eios_domain.registry import Origin, RegistryState
from eios_storage.tables.agent import definition as agent_t
from eios_storage.tables.capability import definition as capability_t
from eios_storage.tables.capability import provider as provider_t
from eios_storage.tables.capability import provider_capability as pc_t
from eios_storage.tables.capability import provider_metric as metric_t
from eios_storage.tables.capability import state_history as history_t
from eios_storage.tables.skill import definition as skill_t


class ProviderRow(BaseModel):
    id: str
    version: str
    type: str
    origin: Origin
    state: RegistryState
    maturity: str
    trust: str
    verification_state: str
    priority: int
    manifest: dict[str, Any]
    manifest_hash: str
    approved_by: str | None
    approval_id: uuid.UUID | None
    health_status: str
    health_checked_at: datetime | None
    health_detail: str
    state_reason: str
    capabilities: list[str] = []


class MetricRow(BaseModel):
    provider_id: str
    provider_version: str
    capability_id: str
    successes: int
    failures: int
    total_latency_ms: int
    tokens: int
    eval_score: float | None
    eval_samples: int
    known_weaknesses: list[str]
    last_used_at: datetime | None
    last_verified_at: datetime | None

    @property
    def samples(self) -> int:
        return self.successes + self.failures

    @property
    def success_rate(self) -> float | None:
        return self.successes / self.samples if self.samples else None

    @property
    def avg_latency_ms(self) -> float | None:
        return self.total_latency_ms / self.samples if self.samples else None


def _provider(row: Any, capabilities: list[str] | None = None) -> ProviderRow:
    m = dict(row._mapping)
    return ProviderRow(**m, capabilities=capabilities or [])


class RegistryStore:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    # -- capabilities -----------------------------------------------------------------------
    async def get_capability(self, capability_id: str) -> dict[str, Any] | None:
        async with self._engine.connect() as conn:
            row = (
                await conn.execute(
                    sa.select(capability_t).where(capability_t.c.id == capability_id)
                )
            ).first()
        return dict(row._mapping) if row else None

    async def list_capabilities(self) -> list[dict[str, Any]]:
        async with self._engine.connect() as conn:
            rows = (await conn.execute(sa.select(capability_t).order_by(capability_t.c.id))).all()
        return [dict(r._mapping) for r in rows]

    async def upsert_capability(self, manifest: CapabilityManifest, digest: str) -> str:
        """Returns 'added', 'updated' or 'unchanged'."""
        existing = await self.get_capability(manifest.id)
        if existing is not None and existing["manifest_hash"] == digest:
            return "unchanged"
        now = utcnow()
        values = {
            "description": manifest.description,
            "risk": manifest.risk.value,
            "inputs": manifest.inputs,
            "outputs": manifest.outputs,
            "allowed_scopes": manifest.allowed_scopes,
            "tags": manifest.tags,
            "manifest": manifest.model_dump(mode="json"),
            "manifest_hash": digest,
            "updated_at": now,
        }
        async with self._engine.begin() as conn:
            if existing is None:
                await conn.execute(
                    sa.insert(capability_t).values(id=manifest.id, created_at=now, **values)
                )
            else:
                await conn.execute(
                    sa.update(capability_t).where(capability_t.c.id == manifest.id).values(**values)
                )
        return "added" if existing is None else "updated"

    # -- providers --------------------------------------------------------------------------
    async def get_provider(
        self, provider_id: str, version: str | None = None
    ) -> ProviderRow | None:
        stmt = sa.select(provider_t).where(provider_t.c.id == provider_id)
        if version is not None:
            stmt = stmt.where(provider_t.c.version == version)
        async with self._engine.connect() as conn:
            rows = (await conn.execute(stmt)).all()
            if not rows:
                return None
            best = max(rows, key=lambda r: _version_key(r._mapping["version"]))
            caps = (
                await conn.execute(
                    sa.select(pc_t.c.capability_id).where(
                        pc_t.c.provider_id == provider_id,
                        pc_t.c.provider_version == best._mapping["version"],
                    )
                )
            ).all()
        return _provider(best, sorted(c[0] for c in caps))

    async def list_providers(
        self,
        *,
        capability_id: str | None = None,
        states: Sequence[RegistryState] | None = None,
    ) -> list[ProviderRow]:
        stmt = sa.select(provider_t)
        if capability_id is not None:
            stmt = stmt.join(
                pc_t,
                sa.and_(
                    pc_t.c.provider_id == provider_t.c.id,
                    pc_t.c.provider_version == provider_t.c.version,
                ),
            ).where(pc_t.c.capability_id == capability_id)
        if states:
            stmt = stmt.where(provider_t.c.state.in_([s.value for s in states]))
        stmt = stmt.order_by(provider_t.c.id, provider_t.c.version)
        async with self._engine.connect() as conn:
            rows = (await conn.execute(stmt)).all()
            caps = (await conn.execute(sa.select(pc_t))).all()
        by_provider: dict[tuple[str, str], list[str]] = {}
        for c in caps:
            by_provider.setdefault((c.provider_id, c.provider_version), []).append(c.capability_id)
        return [
            _provider(r, sorted(by_provider.get((r._mapping["id"], r._mapping["version"]), [])))
            for r in rows
        ]

    async def upsert_provider(
        self,
        manifest: ToolManifest,
        digest: str,
        *,
        origin: Origin,
        state: RegistryState,
        approved_by: str | None = None,
        approval_id: uuid.UUID | None = None,
    ) -> str:
        existing = await self.get_provider(manifest.id, manifest.version)
        if existing is not None and existing.manifest_hash == digest and existing.state is state:
            return "unchanged"
        now = utcnow()
        values: dict[str, Any] = {
            "type": manifest.type,
            "origin": origin.value,
            "state": state.value,
            "maturity": manifest.maturity,
            "trust": manifest.trust,
            "verification_state": manifest.verification_state,
            "priority": manifest.priority,
            "manifest": manifest.model_dump(mode="json"),
            "manifest_hash": digest,
            "approved_by": approved_by,
            "approval_id": approval_id,
            "updated_at": now,
        }
        async with self._engine.begin() as conn:
            if existing is None:
                await conn.execute(
                    sa.insert(provider_t).values(
                        id=manifest.id,
                        version=manifest.version,
                        created_at=now,
                        **values,
                    )
                )
            else:
                await conn.execute(
                    sa.update(provider_t)
                    .where(provider_t.c.id == manifest.id, provider_t.c.version == manifest.version)
                    .values(**values)
                )
            await conn.execute(
                sa.delete(pc_t).where(
                    pc_t.c.provider_id == manifest.id, pc_t.c.provider_version == manifest.version
                )
            )
            await conn.execute(
                sa.insert(pc_t),
                [
                    {
                        "provider_id": manifest.id,
                        "provider_version": manifest.version,
                        "capability_id": c,
                    }
                    for c in sorted(set(manifest.capabilities))
                ],
            )
        return "added" if existing is None else "updated"

    async def set_provider_health(
        self, provider_id: str, version: str, status: str, detail: str
    ) -> None:
        async with self._engine.begin() as conn:
            await conn.execute(
                sa.update(provider_t)
                .where(provider_t.c.id == provider_id, provider_t.c.version == version)
                .values(
                    health_status=status, health_checked_at=utcnow(), health_detail=detail[:500]
                )
            )

    # -- agents & skills --------------------------------------------------------------------
    async def get_agent(self, agent_id: str, version: str | None = None) -> dict[str, Any] | None:
        return await self._get_def(agent_t, agent_id, version)

    async def list_agents(self) -> list[dict[str, Any]]:
        return await self._list_defs(agent_t)

    async def get_skill(self, skill_id: str, version: str | None = None) -> dict[str, Any] | None:
        return await self._get_def(skill_t, skill_id, version)

    async def list_skills(self) -> list[dict[str, Any]]:
        return await self._list_defs(skill_t)

    async def _get_def(
        self, table: sa.Table, item_id: str, version: str | None
    ) -> dict[str, Any] | None:
        stmt = sa.select(table).where(table.c.id == item_id)
        if version is not None:
            stmt = stmt.where(table.c.version == version)
        async with self._engine.connect() as conn:
            rows = (await conn.execute(stmt)).all()
        if not rows:
            return None
        best = max(rows, key=lambda r: _version_key(r._mapping["version"]))
        return dict(best._mapping)

    async def _list_defs(self, table: sa.Table) -> list[dict[str, Any]]:
        async with self._engine.connect() as conn:
            rows = (
                await conn.execute(sa.select(table).order_by(table.c.id, table.c.version))
            ).all()
        latest: dict[str, dict[str, Any]] = {}
        for r in rows:
            m = dict(r._mapping)
            if m["id"] not in latest or _version_key(m["version"]) > _version_key(
                latest[m["id"]]["version"]
            ):
                latest[m["id"]] = m
        return sorted(latest.values(), key=lambda d: d["id"])

    async def upsert_agent(
        self,
        manifest: AgentManifest,
        digest: str,
        *,
        origin: Origin,
        state: RegistryState,
        approved_by: str | None = None,
        approval_id: uuid.UUID | None = None,
    ) -> str:
        return await self._upsert_def(
            agent_t,
            manifest.id,
            manifest.version,
            digest,
            {
                "role": manifest.role,
                "can_write": manifest.can_write,
                "manifest": manifest.model_dump(mode="json"),
            },
            origin,
            state,
            approved_by,
            approval_id,
        )

    async def upsert_skill(
        self,
        manifest: SkillManifest,
        digest: str,
        *,
        origin: Origin,
        state: RegistryState,
        approved_by: str | None = None,
        approval_id: uuid.UUID | None = None,
    ) -> str:
        return await self._upsert_def(
            skill_t,
            manifest.id,
            manifest.version,
            digest,
            {"manifest": manifest.model_dump(mode="json")},
            origin,
            state,
            approved_by,
            approval_id,
        )

    async def _upsert_def(
        self,
        table: sa.Table,
        item_id: str,
        version: str,
        digest: str,
        extra: dict[str, Any],
        origin: Origin,
        state: RegistryState,
        approved_by: str | None,
        approval_id: uuid.UUID | None,
    ) -> str:
        existing = await self._get_def(table, item_id, version)
        if (
            existing is not None
            and existing["manifest_hash"] == digest
            and existing["state"] == state.value
        ):
            return "unchanged"
        now = utcnow()
        values = {
            "origin": origin.value,
            "state": state.value,
            "manifest_hash": digest,
            "approved_by": approved_by,
            "approval_id": approval_id,
            "updated_at": now,
            **extra,
        }
        async with self._engine.begin() as conn:
            if existing is None:
                await conn.execute(
                    sa.insert(table).values(id=item_id, version=version, created_at=now, **values)
                )
            else:
                await conn.execute(
                    sa.update(table)
                    .where(table.c.id == item_id, table.c.version == version)
                    .values(**values)
                )
        return "added" if existing is None else "updated"

    # -- state ------------------------------------------------------------------------------
    async def set_state(
        self,
        kind: str,
        item_id: str,
        version: str,
        new_state: RegistryState,
        *,
        actor: str,
        reason: str,
        approved_by: str | None = None,
        approval_id: uuid.UUID | None = None,
    ) -> RegistryState:
        """Persist a state change and its history row atomically. Returns the previous state."""
        table = {"provider": provider_t, "agent": agent_t, "skill": skill_t}[kind]
        async with self._engine.begin() as conn:
            current = (
                await conn.execute(
                    sa.select(table.c.state)
                    .where(table.c.id == item_id, table.c.version == version)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if current is None:
                raise LookupError(f"{kind} {item_id}@{version} is not registered")
            values: dict[str, Any] = {
                "state": new_state.value,
                "state_reason": reason[:500],
                "updated_at": utcnow(),
            }
            if approved_by is not None:
                values.update(approved_by=approved_by, approval_id=approval_id)
            await conn.execute(
                sa.update(table)
                .where(table.c.id == item_id, table.c.version == version)
                .values(**values)
            )
            await conn.execute(
                sa.insert(history_t).values(
                    id=new_id(),
                    kind=kind,
                    item_id=item_id,
                    item_version=version,
                    from_state=current,
                    to_state=new_state.value,
                    actor=actor,
                    reason=reason[:500],
                    approval_id=approval_id,
                    at=utcnow(),
                )
            )
        return RegistryState(current)

    async def history(self, kind: str, item_id: str, limit: int = 100) -> list[dict[str, Any]]:
        async with self._engine.connect() as conn:
            rows = (
                await conn.execute(
                    sa.select(history_t)
                    .where(history_t.c.kind == kind, history_t.c.item_id == item_id)
                    .order_by(history_t.c.at.desc())
                    .limit(limit)
                )
            ).all()
        return [dict(r._mapping) for r in rows]

    # -- metrics (Capability Intelligence inputs) ----------------------------------------------
    async def record_outcome(
        self,
        provider_id: str,
        version: str,
        capability_id: str,
        *,
        success: bool,
        latency_ms: int = 0,
        tokens: int = 0,
    ) -> None:
        now = utcnow()
        stmt = pg_insert(metric_t).values(
            provider_id=provider_id,
            provider_version=version,
            capability_id=capability_id,
            successes=1 if success else 0,
            failures=0 if success else 1,
            total_latency_ms=max(latency_ms, 0),
            tokens=max(tokens, 0),
            last_used_at=now,
            updated_at=now,
        )
        stmt = stmt.on_conflict_do_update(
            constraint="pk_provider_metric",
            set_={
                "successes": metric_t.c.successes + (1 if success else 0),
                "failures": metric_t.c.failures + (0 if success else 1),
                "total_latency_ms": metric_t.c.total_latency_ms + max(latency_ms, 0),
                "tokens": metric_t.c.tokens + max(tokens, 0),
                "last_used_at": now,
                "updated_at": now,
            },
        )
        async with self._engine.begin() as conn:
            await conn.execute(stmt)

    async def metrics_for(self, capability_id: str) -> dict[tuple[str, str], MetricRow]:
        async with self._engine.connect() as conn:
            rows = (
                await conn.execute(
                    sa.select(metric_t).where(metric_t.c.capability_id == capability_id)
                )
            ).all()
        return {(r.provider_id, r.provider_version): MetricRow(**dict(r._mapping)) for r in rows}


def _version_key(version: str) -> tuple[int, ...]:
    core = version.split("-", 1)[0].split("+", 1)[0]
    try:
        return tuple(int(p) for p in core.split("."))
    except ValueError:
        return (0,)
