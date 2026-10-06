from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from eios_jobs import JobRunner
from eios_project_intelligence import SYNC_JOB
from eios_runtime import build_container
from tests.conftest import make_settings
from tests.gitfixtures import MINI_PROJECT, commit_all, make_repo, write

pytestmark = pytest.mark.integration


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    return tmp_path / "workspace"


async def test_workspace_is_enforced_over_http(client: httpx.AsyncClient, tmp_path: Path) -> None:
    outside = make_repo(tmp_path, "outside", MINI_PROJECT)
    response = await client.post("/projects/bootstrap", json={"path": str(outside)})
    assert response.status_code == 403 and "outside the approved" in response.json()["detail"]
    assert (await client.post("/projects/bootstrap", json={"path": "/etc"})).status_code == 403
    traversal = str(tmp_path / "workspace" / ".." / "outside")
    assert (await client.post("/projects/bootstrap", json={"path": traversal})).status_code == 403


async def test_non_git_directory_reports_error_state(
    client: httpx.AsyncClient, workspace: Path
) -> None:
    (workspace / "plain").mkdir()
    body = (
        await client.post("/projects/bootstrap", json={"path": str(workspace / "plain")})
    ).json()
    assert body["state"] == "ERROR" and body["project"] is None and body["detail"]


async def test_bootstrap_with_inline_sync_then_browse_the_index(
    client: httpx.AsyncClient, workspace: Path
) -> None:
    repo = make_repo(
        workspace, "proj", MINI_PROJECT, remote="https://tok:pw@github.com/Acme/Proj.git"
    )
    body = (await client.post("/projects/bootstrap", json={"path": str(repo), "wait": True})).json()
    assert (
        body["state"] == "NEW"
        and body["needs_sync"]
        and body["sync"]["changed"] == len(MINI_PROJECT)
    )
    assert body["project"]["remote"] == "github.com/acme/proj" and "pw" not in str(body)
    pid = body["project"]["id"]

    detail = (await client.get(f"/projects/{pid}")).json()
    assert detail["project"]["bootstrap_state"] == "CURRENT"
    assert detail["latest_snapshot"]["files_total"] == len(MINI_PROJECT)

    again = (await client.post("/projects/bootstrap", json={"path": str(repo)})).json()
    assert again["state"] == "CURRENT" and not again["needs_sync"] and again["job_id"] is None

    first = (await client.get(f"/projects/{pid}/files", params={"limit": 3})).json()
    # bytewise ordering, whatever the database locale is
    assert [f["path"] for f in first] == ["README.md", "app/__init__.py", "app/api.py"]
    rest = (
        await client.get(
            f"/projects/{pid}/files", params={"after_path": first[-1]["path"], "limit": 100}
        )
    ).json()
    assert len(first) + len(rest) == len(MINI_PROJECT)
    py = (await client.get(f"/projects/{pid}/files", params={"language": "python"})).json()
    assert all(f["language"] == "python" for f in py) and len(py) == 5
    sql = (await client.get(f"/projects/{pid}/files", params={"prefix": "db/"})).json()
    assert [f["path"] for f in sql] == ["db/schema.sql"]

    syms = (await client.get(f"/projects/{pid}/symbols", params={"q": "userservice"})).json()
    assert {s["qualified_name"] for s in syms} >= {"UserService", "UserService.create"}

    hood = (
        await client.get(f"/projects/{pid}/graph", params={"key": "file:app/service.py"})
    ).json()
    assert "file:app/models.py" in {n["key"] for n in hood["nodes"]}
    cov = (await client.get(f"/projects/{pid}/coverage")).json()
    assert cov["files_total"] == len(MINI_PROJECT) and cov["languages"]["python"] == 5
    assert [p["id"] for p in (await client.get("/projects")).json()] == [pid]


async def test_queued_sync_is_processed_by_a_worker_and_is_observable(
    client: httpx.AsyncClient, workspace: Path, db: AsyncEngine, tmp_path: Path
) -> None:
    repo = make_repo(workspace, "proj", MINI_PROJECT)
    body = (await client.post("/projects/bootstrap", json={"path": str(repo)})).json()
    assert body["job_id"] and body["run_id"] and body["sync"] is None
    pid, run_id = body["project"]["id"], body["run_id"]

    # not indexed yet
    assert (await client.get(f"/projects/{pid}/files")).status_code == 409

    # a second request while queued is deduplicated
    again = (await client.post(f"/projects/{pid}/sync", json={})).json()
    assert again["job_id"] == body["job_id"] and again["run_id"] == run_id

    container = build_container(
        make_settings(workspace_roots=str(workspace), blob_dir=tmp_path / "b"), db
    )
    runner = JobRunner(container.queue, {SYNC_JOB: container.projects.handle_sync_job})
    assert await runner.run_once()

    for _ in range(100):
        run = (await client.get(f"/runs/{run_id}")).json()
        if run["status"] == "completed":
            break
        await asyncio.sleep(0.05)
    assert run["status"] == "completed"
    types = [
        e["type"]
        for e in (await client.get(f"/runs/{run_id}/events", params={"limit": 500})).json()["items"]
    ]
    assert "PROJECT_SYNCED" in types and types.count("FILE_CHANGED") == len(MINI_PROJECT)
    assert (await client.get(f"/projects/{pid}/files")).status_code == 200


async def test_inline_resync_picks_up_commits_and_uncommitted_work(
    client: httpx.AsyncClient, workspace: Path
) -> None:
    repo = make_repo(workspace, "proj", MINI_PROJECT)
    pid = (await client.post("/projects/bootstrap", json={"path": str(repo), "wait": True})).json()[
        "project"
    ]["id"]
    write(repo, {"extra.py": "def extra():\n    pass\n"})
    commit_all(repo, "extra")
    write(repo, {"wip.py": "def wip():\n    pass\n"})
    result = (await client.post(f"/projects/{pid}/sync", json={"wait": True})).json()["sync"]
    assert result["changed"] == 1 and result["overlay"] == 1 and result["state"] == "DIRTY"
    overlay = (await client.get(f"/projects/{pid}/files", params={"scope": "overlay"})).json()
    assert [f["path"] for f in overlay] == ["wip.py"]
    found = (await client.get(f"/projects/{pid}/symbols", params={"q": "wip"})).json()
    assert found == []  # overlay is excluded unless asked for
    found = (
        await client.get(f"/projects/{pid}/symbols", params={"q": "wip", "include_overlay": True})
    ).json()
    assert [(s["scope"], s["name"]) for s in found] == [("overlay", "wip")]


async def test_unknown_project_and_bad_input(client: httpx.AsyncClient) -> None:
    import uuid

    ghost = uuid.uuid4()
    for path in ("", "/files", "/symbols?q=x", "/graph?key=x", "/coverage"):
        assert (await client.get(f"/projects/{ghost}{path}")).status_code == 404
    assert (await client.post(f"/projects/{ghost}/sync", json={})).status_code == 404
    assert (await client.post("/projects/bootstrap", json={"path": ""})).status_code == 422
    assert (
        await client.post("/projects/bootstrap", json={"path": "x", "sync": "bogus"})
    ).status_code == 422
