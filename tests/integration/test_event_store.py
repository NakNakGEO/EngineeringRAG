from __future__ import annotations

import asyncio
import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

from eios_domain.errors import DomainError, NotFoundError
from eios_domain.events import ActorType, EventDraft, EventStatus, EventType
from eios_domain.run import Run
from eios_observability import PostgresEventStore, PostgresRunRepository

pytestmark = pytest.mark.integration


async def _run(engine: AsyncEngine) -> Run:
    return await PostgresRunRepository(engine).create(Run(kind="t"))


def _draft(run: Run, type_: EventType = EventType.KNOWLEDGE_HIT, **kw: object) -> EventDraft:
    return EventDraft(
        run_id=run.id,
        trace_id=run.trace_id,
        span_id=uuid.uuid4(),
        type=type_,
        **kw,  # type: ignore[arg-type]
    )


async def test_append_assigns_gap_free_increasing_seq(db: AsyncEngine) -> None:
    store, run = PostgresEventStore(db), await _run(db)
    first = await store.append(_draft(run))
    batch = await store.append_many([_draft(run), _draft(run), _draft(run)])
    assert [first.seq] + [e.seq for e in batch] == [1, 2, 3, 4]
    assert await store.latest_seq(run.id) == 4


async def test_concurrent_appends_stay_gap_free_and_unique(db: AsyncEngine) -> None:
    store, run = PostgresEventStore(db), await _run(db)
    results = await asyncio.gather(*(store.append(_draft(run, summary=str(i))) for i in range(60)))
    assert sorted(e.seq for e in results) == list(range(1, 61))
    page = await store.list_events(run.id, limit=1000)
    assert [e.seq for e in page.items] == list(range(1, 61))


async def test_runs_have_independent_sequences(db: AsyncEngine) -> None:
    store = PostgresEventStore(db)
    a, b = await _run(db), await _run(db)
    await store.append_many([_draft(a), _draft(a)])
    assert (await store.append(_draft(b))).seq == 1


async def test_append_to_unknown_run_is_rejected(db: AsyncEngine) -> None:
    store = PostgresEventStore(db)
    ghost = Run(kind="ghost")
    with pytest.raises(NotFoundError):
        await store.append(_draft(ghost))


async def test_append_many_requires_single_run(db: AsyncEngine) -> None:
    store = PostgresEventStore(db)
    a, b = await _run(db), await _run(db)
    with pytest.raises(DomainError):
        await store.append_many([_draft(a), _draft(b)])


async def test_cursor_pagination_and_filters(db: AsyncEngine) -> None:
    store, run = PostgresEventStore(db), await _run(db)
    span = uuid.uuid4()
    await store.append_many(
        [_draft(run, EventType.TOOL_STARTED, status=EventStatus.STARTED) for _ in range(5)]
        + [
            EventDraft(
                run_id=run.id,
                trace_id=run.trace_id,
                span_id=span,
                type=EventType.POLICY_DENIED,
                status=EventStatus.DENIED,
            )
        ]
    )
    seen: list[int] = []
    cursor = 0
    while True:
        page = await store.list_events(run.id, after_seq=cursor, limit=2)
        seen += [e.seq for e in page.items]
        if not page.has_more:
            assert page.next_cursor is None
            break
        assert page.next_cursor == page.items[-1].seq
        cursor = page.next_cursor
    assert seen == [1, 2, 3, 4, 5, 6]

    denied = await store.list_events(run.id, status=EventStatus.DENIED)
    assert [e.type for e in denied.items] == [EventType.POLICY_DENIED]
    by_type = await store.list_events(run.id, types=["TOOL_STARTED"])
    assert len(by_type.items) == 5
    by_span = await store.list_events(run.id, span_id=span)
    assert [e.seq for e in by_span.items] == [6]


async def test_secrets_are_redacted_before_persisting(db: AsyncEngine) -> None:
    store, run = PostgresEventStore(db), await _run(db)
    event = await store.append(
        _draft(
            run,
            summary="dsn postgresql+psycopg://u:topsecret@h/db",
            data={"api_key": "sk-123", "note": "mssql://sa:pw123@corp/db"},
        )
    )
    assert "topsecret" not in event.summary
    assert event.data["api_key"] == "[REDACTED]" and "pw123" not in event.data["note"]
    async with db.connect() as conn:
        raw = (
            await conn.execute(sa.text("SELECT summary, data::text FROM observability.event"))
        ).one()
    assert "topsecret" not in raw[0] and "sk-123" not in raw[1] and "pw123" not in raw[1]


async def test_events_are_append_only(db: AsyncEngine) -> None:
    store, run = PostgresEventStore(db), await _run(db)
    await store.append(_draft(run))
    for statement in (
        "UPDATE observability.event SET summary = 'tampered'",
        "DELETE FROM observability.event",
    ):
        with pytest.raises(DBAPIError, match="append-only"):
            async with db.begin() as conn:
                await conn.execute(sa.text(statement))
    assert (await store.list_events(run.id)).items[0].summary == ""


async def test_delete_only_allowed_inside_retention_scope(db: AsyncEngine) -> None:
    store, run = PostgresEventStore(db), await _run(db)
    await store.append(_draft(run))
    async with db.begin() as conn:
        await conn.execute(sa.text("SELECT set_config('eios.retention', 'on', true)"))
        await conn.execute(sa.text("DELETE FROM observability.event"))
    assert (await store.list_events(run.id)).items == []
    # the setting was transaction-local: a later plain DELETE is rejected again
    await store.append(_draft(run))
    with pytest.raises(DBAPIError, match="append-only"):
        async with db.begin() as conn:
            await conn.execute(sa.text("DELETE FROM observability.event"))


async def test_runs_with_events_cannot_be_deleted(db: AsyncEngine) -> None:
    store, run = PostgresEventStore(db), await _run(db)
    await store.append(_draft(run))
    with pytest.raises(DBAPIError):
        async with db.begin() as conn:
            await conn.execute(sa.text("DELETE FROM platform.run"))


async def test_actor_types_round_trip(db: AsyncEngine) -> None:
    store, run = PostgresEventStore(db), await _run(db)
    e = await store.append(_draft(run, actor_type=ActorType.LLM, actor_id="gpt-x"))
    got = (await store.list_events(run.id)).items[0]
    assert (
        got.actor_type is ActorType.LLM and got.actor_id == "gpt-x" and got.event_id == e.event_id
    )
