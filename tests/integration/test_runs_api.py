from __future__ import annotations

import asyncio
import json
import uuid

import httpx
import pytest

pytestmark = pytest.mark.integration


async def _wait_status(
    client: httpx.AsyncClient, run_id: str, want: str, within: float = 10
) -> dict:  # type: ignore[type-arg]
    for _ in range(int(within / 0.05)):
        body = (await client.get(f"/runs/{run_id}")).json()
        if body["status"] == want:
            return body  # type: ignore[no-any-return]
        await asyncio.sleep(0.05)
    raise AssertionError(f"run never reached {want}: {body}")


async def _sse(
    client: httpx.AsyncClient, url: str, headers: dict[str, str] | None = None
) -> list[dict]:  # type: ignore[type-arg]
    """Collect (id, event, data) frames until the server ends the stream."""
    frames: list[dict] = []  # type: ignore[type-arg]
    async with client.stream("GET", url, headers=headers) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        current: dict = {}  # type: ignore[type-arg]
        async for line in response.aiter_lines():
            if line == "":
                if current:
                    frames.append(current)
                current = {}
            elif line.startswith(":"):
                continue
            else:
                key, _, value = line.partition(": ")
                current[key] = value
    return frames


async def test_create_run_runs_demo_and_exposes_timeline(client: httpx.AsyncClient) -> None:
    response = await client.post("/runs", json={"kind": "demo", "goal": "hello"})
    assert response.status_code == 202
    run_id = response.json()["run_id"]
    assert response.headers["x-correlation-id"]

    run = await _wait_status(client, run_id, "completed")
    assert run["goal"] == "hello" and run["finished_at"] is not None

    page = (await client.get(f"/runs/{run_id}/events", params={"limit": 500})).json()
    seqs = [e["seq"] for e in page["items"]]
    assert seqs == list(range(1, len(seqs) + 1))
    assert page["items"][0]["type"] == "RUN_STARTED"
    assert page["items"][-1]["type"] == "RUN_COMPLETED"
    assert all(e["run_id"] == run_id and e["schema_version"] == 1 for e in page["items"])


async def test_event_filters_and_pagination(client: httpx.AsyncClient) -> None:
    run_id = (await client.post("/runs", json={})).json()["run_id"]
    await _wait_status(client, run_id, "completed")
    first = (await client.get(f"/runs/{run_id}/events", params={"limit": 3})).json()
    assert len(first["items"]) == 3 and first["has_more"] and first["next_cursor"] == 3
    rest = (
        await client.get(f"/runs/{run_id}/events", params={"after_seq": 3, "limit": 500})
    ).json()
    assert rest["items"][0]["seq"] == 4
    hits = (await client.get(f"/runs/{run_id}/events", params={"type": "KNOWLEDGE_HIT"})).json()
    assert len(hits["items"]) == 2


async def test_failed_demo_run(client: httpx.AsyncClient) -> None:
    run_id = (await client.post("/runs", json={"fail": True})).json()["run_id"]
    run = await _wait_status(client, run_id, "failed")
    assert "demo failure" in run["error"]
    last = (await client.get(f"/runs/{run_id}/events", params={"limit": 500})).json()["items"][-1]
    assert last["type"] == "RUN_FAILED" and last["status"] == "failed"


async def test_graph_endpoint(client: httpx.AsyncClient) -> None:
    run_id = (await client.post("/runs", json={})).json()["run_id"]
    await _wait_status(client, run_id, "completed")
    graph = (await client.get(f"/runs/{run_id}/graph")).json()
    assert graph["truncated"] is False
    assert len(graph["nodes"]) >= 4 and len(graph["edges"]) == len(graph["nodes"]) - 1


async def test_list_runs_cursor(client: httpx.AsyncClient) -> None:
    for _ in range(3):
        await client.post("/runs", json={})
    page1 = (await client.get("/runs", params={"limit": 2})).json()
    assert len(page1["items"]) == 2 and page1["next_cursor"]
    page2 = (await client.get("/runs", params={"limit": 2, "cursor": page1["next_cursor"]})).json()
    assert len(page2["items"]) == 1 and page2["next_cursor"] is None
    assert (await client.get("/runs", params={"cursor": "garbage"})).status_code == 400


async def test_unknown_run_and_bad_input(client: httpx.AsyncClient) -> None:
    ghost = uuid.uuid4()
    for path in ("", "/events", "/graph", "/stream"):
        assert (await client.get(f"/runs/{ghost}{path}")).status_code == 404
    assert (await client.get("/runs/not-a-uuid")).status_code == 422
    assert (await client.post("/runs", json={"kind": "workflow"})).status_code == 422
    assert (await client.post("/runs", json={"step_delay_ms": 999999})).status_code == 422


async def test_stream_delivers_live_events_in_order_and_ends(client: httpx.AsyncClient) -> None:
    run_id = (await client.post("/runs", json={"step_delay_ms": 60})).json()["run_id"]
    frames = await asyncio.wait_for(_sse(client, f"/runs/{run_id}/stream"), timeout=15)
    events = [f for f in frames if f.get("event") != "end"]
    assert frames[-1]["event"] == "end"
    ids = [int(f["id"]) for f in events]
    assert ids == list(range(1, len(ids) + 1))
    assert events[0]["event"] == "RUN_STARTED" and events[-1]["event"] == "RUN_COMPLETED"
    payload = json.loads(events[3]["data"])
    assert payload["run_id"] == run_id and payload["seq"] == 4


async def test_stream_is_incremental_not_buffered(client: httpx.AsyncClient) -> None:
    """The first event must arrive well before the run finishes (it takes >= ~0.6s)."""
    run_id = (await client.post("/runs", json={"step_delay_ms": 150})).json()["run_id"]
    async with client.stream("GET", f"/runs/{run_id}/stream") as response:
        async for line in response.aiter_lines():
            if line.startswith("event: RUN_STARTED"):
                break
        run = (await client.get(f"/runs/{run_id}")).json()
        assert run["status"] == "running"


async def test_stream_resumes_after_disconnect_without_gaps_or_duplicates(
    client: httpx.AsyncClient,
) -> None:
    run_id = (await client.post("/runs", json={"step_delay_ms": 40})).json()["run_id"]
    seen: list[int] = []
    async with client.stream("GET", f"/runs/{run_id}/stream") as response:
        async for line in response.aiter_lines():
            if line.startswith("id: "):
                seen.append(int(line[4:]))
            if len(seen) == 4:
                break  # client drops the connection mid-run
    frames = await asyncio.wait_for(
        _sse(client, f"/runs/{run_id}/stream", headers={"Last-Event-ID": str(seen[-1])}),
        timeout=15,
    )
    resumed = [int(f["id"]) for f in frames if "id" in f]
    assert resumed[0] == seen[-1] + 1
    assert seen + resumed == list(range(1, len(seen + resumed) + 1))
    await _wait_status(client, run_id, "completed")
    total = (await client.get(f"/runs/{run_id}/events", params={"limit": 500})).json()["items"]
    assert len(seen + resumed) == len(total)


async def test_stream_of_finished_run_replays_everything_then_ends(
    client: httpx.AsyncClient,
) -> None:
    run_id = (await client.post("/runs", json={})).json()["run_id"]
    await _wait_status(client, run_id, "completed")
    frames = await _sse(client, f"/runs/{run_id}/stream")
    total = (await client.get(f"/runs/{run_id}/events", params={"limit": 500})).json()["items"]
    assert [int(f["id"]) for f in frames if "id" in f] == [e["seq"] for e in total]
    assert frames[-1]["event"] == "end"


async def test_correlation_id_is_echoed_by_runs_api(client: httpx.AsyncClient) -> None:
    response = await client.get("/runs", headers={"X-Correlation-ID": "corr-test-0001"})
    assert response.headers["x-correlation-id"] == "corr-test-0001"
