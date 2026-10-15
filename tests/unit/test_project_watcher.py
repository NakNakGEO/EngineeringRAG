"""The live-index watcher: baseline first, then one sync per real change, never a storm."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

import pytest

from eios_project_intelligence.watcher import ProjectWatcher

pytestmark = pytest.mark.asyncio


@dataclass
class FakeProject:
    id: uuid.UUID
    name: str


class FakeProjects:
    def __init__(self, *names: str) -> None:
        self.items = [FakeProject(uuid.uuid4(), n) for n in names]
        self.signatures: dict[uuid.UUID, str] = {p.id: "v1" for p in self.items}
        self.broken: set[uuid.UUID] = set()
        self.synced: list[uuid.UUID] = []

    async def list_projects(self) -> list[FakeProject]:
        return self.items

    async def worktree_signature(self, project: FakeProject) -> str:
        if project.id in self.broken:
            raise OSError("unreadable")
        return self.signatures[project.id]

    async def request_sync(self, project_id: uuid.UUID) -> None:
        self.synced.append(project_id)


def watcher(fake: FakeProjects, *, min_gap: float = 0.0) -> ProjectWatcher:
    return ProjectWatcher(fake, interval=1.0, min_gap=min_gap)  # type: ignore[arg-type]


async def test_first_look_is_a_baseline_not_a_sync() -> None:
    fake = FakeProjects("a")
    w = watcher(fake)
    assert await w.poll_once() == []
    assert await w.poll_once() == []
    assert fake.synced == []


async def test_a_changed_signature_queues_exactly_one_sync() -> None:
    fake = FakeProjects("a", "b")
    w = watcher(fake)
    await w.poll_once()
    changed = fake.items[0].id
    fake.signatures[changed] = "v2"
    assert await w.poll_once() == [changed]
    assert await w.poll_once() == []  # same signature again: nothing new
    assert fake.synced == [changed]


async def test_changes_inside_the_minimum_gap_wait_for_the_next_round() -> None:
    fake = FakeProjects("a")
    w = watcher(fake, min_gap=3600.0)
    pid = fake.items[0].id
    await w.poll_once()
    fake.signatures[pid] = "v2"
    assert await w.poll_once() == [pid]  # first sync is allowed
    fake.signatures[pid] = "v3"
    assert await w.poll_once() == []  # too soon: held back, not lost
    assert fake.synced == [pid]


async def test_an_unreadable_project_does_not_stop_the_others() -> None:
    fake = FakeProjects("bad", "good")
    bad, good = fake.items[0].id, fake.items[1].id
    w = watcher(fake)
    await w.poll_once()
    fake.broken.add(bad)
    fake.signatures[good] = "v2"
    assert await w.poll_once() == [good]
