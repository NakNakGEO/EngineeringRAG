from __future__ import annotations

import uuid
from pathlib import Path

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

pytestmark = pytest.mark.integration

PROJECT = str(uuid.uuid4())
PROV = [{"source": "tool:scan", "actor": "scanner", "source_version": "abc123"}]


def _body(**kw: object) -> dict[str, object]:
    base: dict[str, object] = {
        "vault": "project",
        "project_id": PROJECT,
        "title": "Retry policy",
        "content": "Failed payments are retried three times.",
        "created_by": "claude",
        "provenance": PROV,
    }
    base.update(kw)
    return base


async def test_create_get_and_search(client: httpx.AsyncClient) -> None:
    created = await client.post("/knowledge", json=_body())
    assert created.status_code == 201
    item = created.json()
    # client-supplied claims never raise trust: API content is LLM-sourced and low-trust
    assert (
        item["source_kind"] == "llm" and item["trust"] == "RAW" and item["health"] == "UNVERIFIED"
    )

    detail = (await client.get(f"/knowledge/{item['id']}")).json()
    assert detail["item"]["title"] == "Retry policy"
    assert detail["provenance"][0]["source"] == "tool:scan"

    hits = (
        await client.post(
            "/knowledge/search", json={"query": "payments retried", "project_id": PROJECT}
        )
    ).json()
    assert [h["item"]["id"] for h in hits] == [item["id"]]
    vec = (
        await client.post(
            "/knowledge/search",
            json={
                "query": "how are failed payments retried",
                "project_id": PROJECT,
                "mode": "vector",
            },
        )
    ).json()
    assert vec[0]["item"]["id"] == item["id"]


async def test_other_projects_never_see_project_knowledge(client: httpx.AsyncClient) -> None:
    await client.post("/knowledge", json=_body())
    other = str(uuid.uuid4())
    hits = (
        await client.post("/knowledge/search", json={"query": "payments", "project_id": other})
    ).json()
    assert hits == []
    none = (await client.post("/knowledge/search", json={"query": "payments"})).json()
    assert none == []


@pytest.mark.parametrize(
    "override",
    [
        {"trust": "VERIFIED"},  # literal only allows RAW/OBSERVED
        {"trust": "APPROVED"},
        {"source_kind": "manual"},  # unknown field is ignored; source is still forced to llm
        {"provenance": []},  # provenance required
        {"vault": "project", "project_id": None},  # project vault needs a project
        {"vault": "default", "project_id": PROJECT},  # company data in the portable vault
    ],
)
async def test_untrusted_client_cannot_escalate(
    client: httpx.AsyncClient, override: dict[str, object]
) -> None:
    response = await client.post("/knowledge", json=_body(**override))
    if override.get("source_kind") == "manual":
        assert response.status_code == 201 and response.json()["source_kind"] == "llm"
    else:
        assert response.status_code == 422


async def test_ephemeral_knowledge_gets_a_ttl(client: httpx.AsyncClient) -> None:
    item = (await client.post("/knowledge", json=_body(vault="ephemeral", project_id=None))).json()
    assert item["expires_at"] is not None


async def test_unknown_ids_404(client: httpx.AsyncClient) -> None:
    ghost = uuid.uuid4()
    assert (await client.get(f"/knowledge/{ghost}")).status_code == 404
    assert (await client.get(f"/evidence/{ghost}")).status_code == 404
    assert (await client.get(f"/evidence/{ghost}/content")).status_code == 404


async def test_evidence_endpoints_serve_metadata_and_bounded_content(
    client: httpx.AsyncClient,
    live_api: str,
    db: AsyncEngine,
    tmp_path: Path,
) -> None:
    # ingest through the real container used by the running app
    from eios_knowledge import LocalBlobStore
    from eios_runtime import build_container
    from tests.conftest import make_settings

    # same blob dir as the live app: <tmp_path>/blobs
    container = build_container(make_settings(blob_dir=tmp_path / "blobs"), db)
    assert isinstance(container.blobs, LocalBlobStore)
    record = await container.pipeline.ingest_raw(b"0123456789" * 10, tool_id="scan", summary="s")

    meta = (await client.get(f"/evidence/{record.id}")).json()
    assert meta["trust"] == "RAW" and meta["vault"] == "ephemeral" and meta["size_bytes"] == 100
    full = await client.get(f"/evidence/{record.id}/content")
    assert full.content == b"0123456789" * 10 and "x-content-truncated" not in full.headers
    part = await client.get(f"/evidence/{record.id}/content", params={"max_bytes": 10})
    assert part.content == b"0123456789" and part.headers["x-content-truncated"] == "true"
