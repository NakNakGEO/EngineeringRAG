"""Retention, audit export, admin API and recovery behaviour."""

from __future__ import annotations

import base64
import json
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
import sqlalchemy as sa
from httpx import ASGITransport
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

from eios_domain.events import ActorType
from eios_domain.ids import new_id, utcnow
from eios_domain.knowledge import KnowledgeItemCreate, SourceKind
from eios_domain.policy import PolicyRequest
from eios_domain.vault import Vault
from eios_governance.retention import RetentionPolicy, RetentionService
from eios_policy.audit import export_ndjson, verify_ndjson
from eios_runtime import Container, build_container
from tests.conftest import make_settings

pytestmark = pytest.mark.integration


@pytest.fixture
async def c(db: AsyncEngine, tmp_path: Path) -> Container:
    return build_container(
        make_settings(blob_dir=tmp_path / "blobs", workspace_roots=str(tmp_path)), db
    )


async def test_retention_removes_only_what_is_expired_or_old(c: Container, db: AsyncEngine) -> None:
    keep = await c.knowledge.add_item(
        KnowledgeItemCreate(
            vault=Vault.DEFAULT,
            title="keep",
            content="durable",
            source_kind=SourceKind.MANUAL,
            created_by="u",
        )
    )
    gone = await c.knowledge.add_item(
        KnowledgeItemCreate(
            vault=Vault.EPHEMERAL,
            title="gone",
            content="scratch",
            source_kind=SourceKind.MANUAL,
            created_by="u",
            expires_at=utcnow() + timedelta(days=1),
        )
    )
    ev_gone = await c.pipeline.ingest_raw(b"old scratch", ttl=timedelta(days=1))
    ev_keep = await c.pipeline.ingest_raw(b"durable evidence", vault=Vault.DEFAULT)
    async with c.recorder.run(kind="old", goal="g") as ctx:
        pass
    async with c.recorder.run(kind="recent", goal="g") as recent:
        pass
    past = utcnow() - timedelta(days=200)
    async with db.begin() as conn:
        await conn.execute(
            sa.text("UPDATE knowledge.item SET expires_at = :t WHERE id = :i"),
            {"t": utcnow() - timedelta(hours=1), "i": gone.id},
        )
        await conn.execute(
            sa.text("UPDATE evidence.record SET expires_at = :t WHERE id = :i"),
            {"t": utcnow() - timedelta(hours=1), "i": ev_gone.id},
        )
        await conn.execute(
            sa.text("UPDATE platform.run SET finished_at = :t WHERE id = :i"),
            {"t": past, "i": ctx.run_id},
        )
        old_job = new_id()
        await conn.execute(
            sa.text(
                "INSERT INTO platform.job (id, type, status, payload, attempts, max_attempts, "
                "available_at, created_at, updated_at) "
                "VALUES (:i, 't', 'succeeded', '{}', 1, 3, :t, :t, :t)"
            ),
            {"i": old_job, "t": past},
        )
    report = await c.retention.run()
    assert report.deleted["ephemeral_knowledge"] == 1 and report.deleted["expired_evidence"] == 1
    assert report.deleted["events"] >= 2 and report.deleted["jobs"] == 1
    assert report.deleted["orphan_blobs"] == 1 and report.deleted.get("audit", 0) == 0
    assert (
        await c.knowledge.knowledge.get(keep.id) is not None
        and await c.knowledge.knowledge.get(gone.id) is None
    )
    assert await c.evidence.get(ev_keep.id) is not None and await c.evidence.get(ev_gone.id) is None
    assert not c.blobs.exists(ev_gone.content_hash) and c.blobs.exists(ev_keep.content_hash)
    assert (await c.events.list_events(ctx.run_id)).items == []  # old finished run's events pruned
    assert (await c.events.list_events(recent.run_id)).items  # recent run untouched


async def test_append_only_still_holds_outside_the_retention_job(
    c: Container, db: AsyncEngine
) -> None:
    async with c.recorder.run(kind="x", goal="g") as ctx:
        pass
    async with db.begin() as conn:
        with pytest.raises(DBAPIError, match="append-only"):
            await conn.execute(sa.text("DELETE FROM observability.event"))
    assert (await c.events.list_events(ctx.run_id)).items


async def test_audit_rows_can_never_be_updated_even_during_retention(
    c: Container, db: AsyncEngine
) -> None:
    await c.policy.evaluate(PolicyRequest(actor_type=ActorType.LLM, actor_id="a", action="db.read"))
    async with db.begin() as conn:
        await conn.execute(sa.text("SELECT set_config('eios.retention','on',true)"))
        with pytest.raises(DBAPIError, match="append-only"):
            await conn.execute(sa.text("UPDATE policy.audit_log SET at = at"))


async def test_audit_retention_deletes_only_when_configured(c: Container, db: AsyncEngine) -> None:
    await c.policy.evaluate(PolicyRequest(actor_type=ActorType.LLM, actor_id="a", action="db.read"))
    assert (await c.retention.run()).deleted.get("audit", 0) == 0
    svc = RetentionService(db, None, RetentionPolicy(audit_days=1))
    async with db.begin() as conn:
        await conn.execute(sa.text("SELECT set_config('eios.retention','on',true)"))
    assert (await svc.run()).deleted["audit"] == 0  # too recent
    assert len(await c.audit.list()) == 1


async def test_audit_export_is_hash_chained_and_tamper_evident(
    c: Container, db: AsyncEngine
) -> None:
    for i in range(7):
        await c.policy.evaluate(
            PolicyRequest(actor_type=ActorType.LLM, actor_id="a", action="db.read", target=f"t{i}")
        )
    lines = [line async for line in export_ndjson(db, batch=3)]
    assert len(lines) == 8 and json.loads(lines[-1])["count"] == 7
    assert verify_ndjson(lines) == (True, "ok")
    edited = list(lines)
    rec = json.loads(edited[2])
    rec["record"]["effect"] = "allow"
    edited[2] = json.dumps(rec)
    ok, why = verify_ndjson(edited)
    assert not ok and "line 3" in why
    assert not verify_ndjson(lines[:-1])[0]  # truncated
    removed = lines[:2] + lines[3:]
    assert not verify_ndjson(removed)[0]  # a deleted entry breaks the chain


async def test_admin_api_is_token_gated_and_round_trips(
    db: AsyncEngine, migrated_database_url: str, tmp_path: Path
) -> None:
    from eios_api.app import create_app

    settings = make_settings(
        database_url=migrated_database_url,
        blob_dir=tmp_path / "b",
        admin_token="adm1n-secret-token",
        manifests_dir=Path("manifests"),
    )
    app = create_app(settings, engine=db, readiness_checks=[])
    container: Container = app.state.container
    await container.knowledge.add_item(
        KnowledgeItemCreate(
            vault=Vault.DEFAULT,
            title="portable",
            content="travels",
            source_kind=SourceKind.MANUAL,
            created_by="u",
        )
    )
    await container.policy.evaluate(
        PolicyRequest(actor_type=ActorType.LLM, actor_id="a", action="db.read")
    )
    adm = {"X-EIOS-Admin-Token": "adm1n-secret-token"}
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as http:
        for method, path, body in (
            ("post", "/admin/export", {"passphrase": "x" * 14}),
            ("post", "/admin/retention", None),
            ("get", "/admin/audit/export", None),
        ):
            assert (
                await getattr(http, method)(path, **({"json": body} if body else {}))
            ).status_code == 401
            assert (
                await getattr(http, method)(
                    path, headers={"X-EIOS-Admin-Token": "nope"}, **({"json": body} if body else {})
                )
            ).status_code == 401
        exp = await http.post(
            "/admin/export", json={"passphrase": "a long enough passphrase"}, headers=adm
        )
        assert exp.status_code == 200 and exp.json()["counts"]["knowledge_items"] == 1
        pkg = exp.json()["package_base64"]
        dry = await http.post(
            "/admin/import",
            json={"package_base64": pkg, "passphrase": "a long enough passphrase", "dry_run": True},
            headers=adm,
        )
        assert dry.status_code == 200 and dry.json()["dry_run"] is True
        bad = await http.post(
            "/admin/import",
            json={"package_base64": pkg, "passphrase": "wrong wrong wrong"},
            headers=adm,
        )
        assert bad.status_code == 400
        garbage = await http.post(
            "/admin/import",
            json={"package_base64": base64.b64encode(b"junk").decode(), "passphrase": "x"},
            headers=adm,
        )
        assert garbage.status_code == 400
        audit = await http.get("/admin/audit/export", headers=adm)
        assert audit.status_code == 200 and verify_ndjson(audit.text.splitlines()) == (True, "ok")
        assert (await http.post("/admin/retention", headers=adm)).status_code == 200
