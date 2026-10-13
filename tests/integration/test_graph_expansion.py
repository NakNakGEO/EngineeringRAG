"""Graph expansion on a saturated graph: hubs, alphabetical noise and tests of a named symbol."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from eios_domain.policy import Risk
from eios_retrieval import ContextLevel, RetrievalQuery
from eios_retrieval.expander import GraphExpander
from eios_retrieval.graph import walk
from eios_retrieval.models import Candidate, CandidateKind, Source
from eios_runtime import Container, build_container
from tests.conftest import make_settings
from tests.gitfixtures import make_repo

pytestmark = pytest.mark.integration

NOISE = 30


def _repo_files() -> dict[str, str | bytes]:
    files: dict[str, str | bytes] = {
        "app/__init__.py": "",
        "app/hub.py": "def hub():\n    return 1\n",
        "app/core.py": ("from app.hub import hub\n\n\ndef named_target():\n    return hub()\n"),
        # relevant caller sorts LAST alphabetically
        "app/zz_ledger_export.py": (
            "from app.core import named_target\n\n\n"
            "def export_ledger():\n    return named_target()\n"
        ),
        "tests/test_core.py": (
            "from app.core import named_target\n\n\ndef test_returns_one():\n"
            "    assert named_target() == 1\n"
        ),
    }
    for n in range(NOISE):
        files[f"app/a{n:02d}_noise.py"] = (
            f"from app.core import named_target\nfrom app.hub import hub\n\n\n"
            f"def noise_{n}():\n    return named_target() + hub()\n"
        )
    return files


@pytest.fixture
async def hubby(db: AsyncEngine, tmp_path: Path) -> tuple[Container, RetrievalQuery]:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    container = build_container(
        make_settings(workspace_roots=str(workspace), blob_dir=tmp_path / "blobs"), db
    )
    repo = make_repo(workspace, "hubby", _repo_files())
    boot = await container.projects.bootstrap(repo)
    assert boot.project is not None
    await container.projects.sync_now(boot.project.id)
    query = RetrievalQuery(
        text="How does the ledger export use `named_target`?",
        project_id=boot.project.id,
        risk=Risk.LOW,
    )
    return container, query


def _seed() -> Candidate:
    return Candidate(
        id="symbol:app/core.py::named_target",
        kind=CandidateKind.SYMBOL,
        title="named_target (function)",
        path="app/core.py",
        source=Source.EXACT,
        rank=1,
        scope="committed",
    )


async def test_a_hub_is_reported_but_its_dependents_are_not_walked(
    hubby: tuple[Container, RetrievalQuery],
) -> None:
    container, query = hubby
    store = container.projects.store
    project = await container.projects.require(query.project_id)  # type: ignore[arg-type]
    branch = project.last_branch or "main"
    seeds = ["file:app/core.py"]
    open_walk = await walk(store, project.id, branch, seeds, depth=2, direction="both")
    bounded = await walk(
        store, project.id, branch, seeds, depth=2, direction="both", hub_limit=NOISE // 2
    )
    hub_key = "file:app/hub.py"
    assert hub_key in open_walk and hub_key in bounded  # the hub itself is a real dependency
    noise = {k for k in open_walk if "_noise.py" in k}
    assert noise  # reached either way: they import core directly (distance 1)
    # ...but everything that only reaches the graph by being a dependent *of the hub* is cut
    assert len(bounded) <= len(open_walk)
    assert all(bounded[k].distance <= 1 for k in bounded if "_noise.py" in k)


async def test_ranking_uses_the_question_not_the_alphabet(
    hubby: tuple[Container, RetrievalQuery],
) -> None:
    container, query = hubby
    rc, _ = await container.governor._prepare(query)
    expander = GraphExpander(container.projects)
    picked = await expander.expand(rc, [_seed()], depth=1, direction="in", limit=5)
    paths = [c.path for c in picked]
    assert "app/zz_ledger_export.py" in paths  # shares the word 'ledger' with the question
    assert len(paths) == 5 and len(set(paths)) == 5  # symbols stand in for their file stubs
    again = await expander.expand(rc, [_seed()], depth=1, direction="in", limit=5)
    assert [c.id for c in again] == [c.id for c in picked]  # deterministic


async def test_a_test_that_imports_the_named_symbol_is_found_at_the_first_level(
    hubby: tuple[Container, RetrievalQuery],
) -> None:
    container, query = hubby
    q = query.model_copy(update={"text": "Which tests exercise `named_target`?"})
    pack = await container.governor.build(q, max_level=ContextLevel.L1)
    assert pack.levels_tried == [ContextLevel.L1]
    found = [i for i in pack.items if i.candidate.path == "tests/test_core.py"]
    assert found and "graph_tests" in found[0].sources
    # the dependents-of-the-seed lookup only runs when the question is about tests
    plain = await container.governor.build(
        query.model_copy(update={"text": "How does `named_target` use the hub?"}),
        max_level=ContextLevel.L1,
    )
    assert all("graph_tests" not in i.sources for i in plain.items)
