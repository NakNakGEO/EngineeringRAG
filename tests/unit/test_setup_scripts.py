"""The local-setup helper and the smoke test's non-network logic."""

from __future__ import annotations

import argparse
import importlib.util
import re
import sys
from pathlib import Path
from types import ModuleType

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"


def _load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module  # dataclasses resolve their module through sys.modules
    spec.loader.exec_module(module)
    return module


setup_local = _load("setup_local")
smoke_stack = _load("smoke_stack")


def _args(**overrides: object) -> argparse.Namespace:
    values: dict[str, object] = {"workspace_dir": None, "port_offset": 0, "admin_token": False}
    values.update(overrides)
    return argparse.Namespace(**values)


@pytest.fixture
def checkout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A throwaway checkout with a real .env.example, so the real .env is never touched."""
    (tmp_path / ".env.example").write_text(
        (setup_local.ROOT / ".env.example").read_text(encoding="utf-8"), encoding="utf-8"
    )
    monkeypatch.setattr(setup_local, "ROOT", tmp_path)
    monkeypatch.setattr(setup_local, "ENV_FILE", tmp_path / ".env")
    monkeypatch.setattr(setup_local, "ENV_EXAMPLE", tmp_path / ".env.example")
    return tmp_path


def test_set_env_value_replaces_or_appends() -> None:
    text = "A=1\nB=2\n"
    assert setup_local.set_env_value(text, "B", "9") == "A=1\nB=9\n"
    assert setup_local.set_env_value(text, "C", "3") == "A=1\nB=2\nC=3\n"
    assert setup_local.set_env_value("A=1\nAB=2\n", "A", "7") == "A=7\nAB=2\n"  # whole key only


def test_a_new_env_gets_a_random_url_safe_password_used_consistently(checkout: Path) -> None:
    values, created = setup_local.prepare_env(_args())
    assert created
    password = values["EIOS_POSTGRES_PASSWORD"]
    assert password != "change-me-locally" and re.fullmatch(r"[A-Za-z0-9_-]{20,}", password)
    assert password in values["EIOS_DATABASE_URL"]  # host tools use the same credentials
    assert (checkout / "workspace").is_dir()  # the default workspace folder is created
    again, created_again = setup_local.prepare_env(_args())
    assert not created_again and again["EIOS_POSTGRES_PASSWORD"] == password  # never regenerated


def test_ports_workspace_and_admin_token_options(checkout: Path, tmp_path: Path) -> None:
    repos = tmp_path / "my repos"
    values, _ = setup_local.prepare_env(
        _args(port_offset=100, workspace_dir=str(repos), admin_token=True)
    )
    assert values["EIOS_API_PORT"] == "8100" and values["EIOS_WEB_PORT"] == "8180"
    assert values["EIOS_POSTGRES_PORT"] == "5532" and ":5532/" in values["EIOS_DATABASE_URL"]
    assert Path(values["EIOS_WORKSPACE_DIR"]) == repos.resolve() and repos.is_dir()
    assert len(values["EIOS_ADMIN_TOKEN"]) >= 20


def test_an_existing_env_is_never_modified(checkout: Path) -> None:
    (checkout / ".env").write_text("EIOS_POSTGRES_PASSWORD=mine\n", encoding="utf-8")
    setup_local.prepare_env(_args(port_offset=100, admin_token=True))
    assert (checkout / ".env").read_text(encoding="utf-8") == "EIOS_POSTGRES_PASSWORD=mine\n"


def test_env_files_are_parsed_without_quotes_or_comments(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_text("# c\nA=\"x y\"\n\nB='q'\nC=1\n", encoding="utf-8")
    assert smoke_stack.read_env(path) == {"A": "x y", "B": "q", "C": "1"}


def test_sse_framed_mcp_responses_are_understood() -> None:
    body = 'event: message\ndata: {"jsonrpc":"2.0","id":1,"result":{"tools":[]}}\n\n'
    assert smoke_stack.McpSession._from_sse(body)["result"] == {"tools": []}
    with pytest.raises(smoke_stack.CheckError):
        smoke_stack.McpSession._from_sse("event: ping\n")


def test_the_sample_project_is_created_once_and_reused(tmp_path: Path) -> None:
    first = smoke_stack.create_sample(tmp_path)
    assert (first / ".git").is_dir() and (first / "sample" / "retry.py").is_file()
    head = (first / ".git" / "HEAD").read_text(encoding="utf-8")
    second = smoke_stack.create_sample(tmp_path)
    assert second == first and (second / ".git" / "HEAD").read_text(encoding="utf-8") == head


def test_the_sample_never_overwrites_a_folder_that_is_not_its_own(tmp_path: Path) -> None:
    other = tmp_path / smoke_stack.SAMPLE_NAME
    other.mkdir()
    (other / "mine.txt").write_text("keep", encoding="utf-8")
    with pytest.raises(smoke_stack.CheckError):
        smoke_stack.create_sample(tmp_path)
    assert (other / "mine.txt").read_text(encoding="utf-8") == "keep"


def test_the_sample_is_a_valid_python_project_that_the_checks_describe() -> None:
    for rel, text in smoke_stack.SAMPLE_FILES.items():
        if rel.endswith(".py"):
            compile(text, rel, "exec")
    assert "def compute_backoff" in smoke_stack.SAMPLE_FILES["sample/retry.py"]
    assert smoke_stack.SYMBOL_ID.endswith("RetryPolicy.compute_backoff")
    assert len(smoke_stack.EXPECTED_MCP_TOOLS) == 8
