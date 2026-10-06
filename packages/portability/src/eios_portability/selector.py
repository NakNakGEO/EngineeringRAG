"""Select what may leave the machine in a portable export: the Default Vault, and nothing else.

Every query filters ``vault = 'default'``. Embeddings and search vectors are not exported (they are
re-derived on import). Provenance links to evidence that is not itself exported are stripped so no
project-derived hash or identifier travels with the package.
"""

from __future__ import annotations

import base64
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine

from eios_domain.vault import Vault
from eios_storage.tables.evidence import blob as blob_t
from eios_storage.tables.evidence import record as record_t
from eios_storage.tables.knowledge import decision as decision_t
from eios_storage.tables.knowledge import item as item_t
from eios_storage.tables.knowledge import provenance as provenance_t
from eios_storage.tables.memory import item as memory_t

_EXCLUDED_COLUMNS = {"embedding", "search_vector"}


class ExportLeakError(Exception):
    """A bundle contained data that must never be exported."""


def _jsonable(value: Any) -> Any:
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, bytes):
        return base64.b64encode(value).decode()
    if isinstance(value, list | tuple):
        return [_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    return value


@dataclass
class ExportBundle:
    knowledge_items: list[dict[str, Any]] = field(default_factory=list)
    provenance: list[dict[str, Any]] = field(default_factory=list)
    memory_items: list[dict[str, Any]] = field(default_factory=list)
    decisions: list[dict[str, Any]] = field(default_factory=list)
    evidence_records: list[dict[str, Any]] = field(default_factory=list)
    blobs: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "knowledge_items": self.knowledge_items,
            "provenance": self.provenance,
            "memory_items": self.memory_items,
            "decisions": self.decisions,
            "evidence_records": self.evidence_records,
            "blobs": self.blobs,
        }

    def counts(self) -> dict[str, int]:
        return {k: len(v) for k, v in self.to_dict().items()}


def assert_clean(bundle: ExportBundle) -> None:
    """Last line of defence: refuse a bundle with anything that is not default-vault data."""
    for name in ("knowledge_items", "memory_items", "decisions", "evidence_records"):
        for row in getattr(bundle, name):
            if row.get("vault") != Vault.DEFAULT.value or row.get("project_id") is not None:
                raise ExportLeakError(f"{name} contains non-default-vault data")


async def collect_default_vault(engine: AsyncEngine) -> ExportBundle:
    """Read the exportable Default Vault. Project and Ephemeral vault rows are never selected."""

    def cols(table: sa.Table) -> list[sa.Column[Any]]:
        return [c for c in table.c if c.name not in _EXCLUDED_COLUMNS]

    default = Vault.DEFAULT.value
    async with engine.connect() as conn:
        items = (
            await conn.execute(sa.select(*cols(item_t)).where(item_t.c.vault == default))
        ).all()
        item_ids = [r._mapping["id"] for r in items]
        memory = (
            await conn.execute(sa.select(*cols(memory_t)).where(memory_t.c.vault == default))
        ).all()
        decisions = (
            await conn.execute(sa.select(*cols(decision_t)).where(decision_t.c.vault == default))
        ).all()
        records = (
            await conn.execute(sa.select(*cols(record_t)).where(record_t.c.vault == default))
        ).all()
        record_ids = {r._mapping["id"] for r in records}
        blob_ids = [r._mapping["blob_id"] for r in records if r._mapping["blob_id"]]
        blobs = (
            (await conn.execute(sa.select(blob_t).where(blob_t.c.id.in_(blob_ids)))).all()
            if blob_ids
            else []
        )
        provenance = (
            (
                await conn.execute(
                    sa.select(provenance_t).where(provenance_t.c.item_id.in_(item_ids))
                )
            ).all()
            if item_ids
            else []
        )

    bundle = ExportBundle(
        knowledge_items=[_jsonable(dict(r._mapping)) for r in items],
        memory_items=[_jsonable(dict(r._mapping)) for r in memory],
        decisions=[_jsonable(dict(r._mapping)) for r in decisions],
        evidence_records=[_jsonable(dict(r._mapping)) for r in records],
        blobs=[_jsonable(dict(r._mapping)) for r in blobs],
    )
    for r in provenance:
        row = dict(r._mapping)
        if row["evidence_id"] is not None and row["evidence_id"] not in record_ids:
            row["evidence_id"] = None  # points at evidence that is not exported
            row["evidence_hash"] = None
        bundle.provenance.append(_jsonable(row))
    assert_clean(bundle)
    return bundle
