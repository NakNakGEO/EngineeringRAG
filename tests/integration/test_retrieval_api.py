from __future__ import annotations

import uuid
from pathlib import Path

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from eios_runtime import build_container
from tests.conftest import make_settings
from tests.evals.corpus import build_corpus

pytestmark = pytest.mark.integration


@pytest.fixture
async def project_id(client: httpx.AsyncClient, db: AsyncEngine, tmp_path: Path) -> str:
    workspace = tmp_path / "workspace"
    container = build_container(
        make_settings(workspace_roots=str(workspace), blob_dir=tmp_path / "b"), db
    )
    corpus = await build_corpus(container, db, workspace)
    return str(corpus.project_id)


async def test_context_build_endpoint_returns_pack_and_records_a_run(
    client: httpx.AsyncClient, project_id: str
) -> None:
    response = await client.post(
        "/context/build",
        json={
            "text": "Before I change `PaymentGateway.charge`, what does it rely on?",
            "project_id": project_id,
        },
    )
    assert response.status_code == 200
    body = response.json()
    pack = body["pack"]
    assert pack["gap"]["ready_to_act"] is True and pack["level"] in {"L1", "L2"}
    assert pack["items"][0]["candidate"]["id"].endswith("PaymentGateway.charge")
    assert set(pack["gap"]) == {"ready_to_act", "missing_context", "confidence", "coverage"}
    assert {
        "critical_evidence_ids",
        "redundant_context_ids",
        "required_expansions",
        "reason",
    } <= set(pack)

    run = (await client.get(f"/runs/{body['run_id']}")).json()
    assert run["status"] == "completed" and run["kind"] == "context_build"
    types = [
        e["type"]
        for e in (await client.get(f"/runs/{body['run_id']}/events", params={"limit": 500})).json()[
            "items"
        ]
    ]
    assert (
        "CONTEXT_REQUESTED" in types and "RETRIEVAL_STARTED" in types and "RUN_COMPLETED" in types
    )


async def test_context_build_without_recording_and_with_level_limits(
    client: httpx.AsyncClient, project_id: str
) -> None:
    body = (
        await client.post(
            "/context/build",
            json={
                "text": "Explain `QuantumLedgerSynchronizer`",
                "project_id": project_id,
                "record_run": False,
                "max_level": "L2",
                "token_budget": 4000,
            },
        )
    ).json()
    assert body["run_id"] is None
    assert body["pack"]["gap"]["ready_to_act"] is False
    assert any(m["kind"] == "unresolved_entity" for m in body["pack"]["gap"]["missing_context"])
    bad = await client.post(
        "/context/build",
        json={"text": "x", "min_level": "L3", "max_level": "L1", "project_id": project_id},
    )
    assert bad.status_code == 422


async def test_search_endpoint_runs_one_pass(client: httpx.AsyncClient, project_id: str) -> None:
    body = (
        await client.post(
            "/retrieval/search",
            json={
                "text": "retry backoff for failed card charges",
                "project_id": project_id,
                "level": "L2",
                "limit": 8,
            },
        )
    ).json()
    assert 0 < len(body["items"]) <= 8
    assert {"fts", "vector"} <= set(body["produced"]) and body["errors"] == []
    assert all(item["score"] > 0 and item["sources"] for item in body["items"])


async def test_impact_endpoint(client: httpx.AsyncClient, project_id: str) -> None:
    body = (
        await client.get(
            f"/projects/{project_id}/impact",
            params={
                "path": ["shop/payments/retry.py"],
                "symbol": ["RetryPolicy.should_retry"],
                "depth": 2,
            },
        )
    ).json()
    assert body["tests"] == ["tests/test_retry.py"] and body["tests_missing"] is False
    assert any(d["path"] == "shop/payments/gateway.py" for d in body["dependents"])
    assert all(d["depth"] <= 2 for d in body["dependents"])
    assert body["risk"] in {"low", "medium", "high", "critical"}
    assert (await client.get(f"/projects/{project_id}/impact")).status_code == 422
    assert (
        await client.get(f"/projects/{uuid.uuid4()}/impact", params={"path": "a.py"})
    ).status_code == 404
    assert (
        await client.get(f"/projects/{project_id}/impact", params={"path": "a.py", "depth": 9})
    ).status_code == 422


async def test_unknown_project_is_a_404_not_a_crash(client: httpx.AsyncClient) -> None:
    for url, body in (
        ("/context/build", {"text": "hello", "project_id": str(uuid.uuid4())}),
        ("/retrieval/search", {"text": "hello", "project_id": str(uuid.uuid4())}),
    ):
        assert (await client.post(url, json=body)).status_code == 404
    knowledge_only = await client.post(
        "/context/build", json={"text": "anything at all", "record_run": False}
    )
    assert (
        knowledge_only.status_code == 200
        and knowledge_only.json()["pack"]["gap"]["ready_to_act"] is False
    )
