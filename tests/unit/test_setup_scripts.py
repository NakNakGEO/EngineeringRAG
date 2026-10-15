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
eios_cli = _load("eios_cli")


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


def test_cli_maps_workspace_folders_to_container_paths() -> None:
    assert eios_cli.to_container_path("my-service") == "/workspace/my-service"
    assert eios_cli.to_container_path("team\\my-service/") == "/workspace/team/my-service"
    assert eios_cli.to_container_path("/workspace/x") == "/workspace/x"


def test_cli_picks_projects_by_id_prefix_or_name_and_refuses_ambiguity() -> None:
    projects = [
        {"id": "aaaa1111-0000", "name": "orders"},
        {"id": "aaaa2222-0000", "name": "Billing"},
        {"id": "bbbb3333-0000", "name": "orders-2"},
    ]
    assert eios_cli.pick_project(projects, "billing")["id"] == "aaaa2222-0000"
    assert eios_cli.pick_project(projects, "bbbb")["name"] == "orders-2"
    assert eios_cli.pick_project(projects, "bill")["id"] == "aaaa2222-0000"  # unique name prefix
    with pytest.raises(SystemExit, match="matches 2 projects"):
        eios_cli.pick_project(projects, "aaaa")
    with pytest.raises(SystemExit, match="Known projects"):
        eios_cli.pick_project(projects, "nothing")


def test_cli_strips_terminal_control_characters_from_api_text() -> None:
    hostile = "ok\x1b[2J\x1b]0;pwned\x07\rname"
    cleaned = eios_cli.clean(hostile)
    assert "\x1b" not in cleaned and "\x07" not in cleaned and "\r" not in cleaned
    assert cleaned.startswith("ok") and cleaned.endswith("name")


def test_cli_refuses_host_paths_instead_of_mangling_them() -> None:
    for host_path in ("C:\\src\\app", "c:/src/app", "D:app"):
        with pytest.raises(SystemExit, match="host path"):
            eios_cli.to_container_path(host_path)


def test_cli_encodes_the_impact_query(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []

    def fake_call(api: str, method: str, path: str, body: object = None, **_: object) -> object:
        seen.append(path)
        return [{"id": "abc", "name": "p"}] if path == "/projects" else {}

    monkeypatch.setattr(eios_cli, "call", fake_call)
    args = argparse.Namespace(
        api="http://x", project="p", path=["a b&c.py"], symbol=["X#y"], json=True, top=5
    )
    eios_cli.cmd_impact(args)
    assert seen[-1] == "/projects/abc/impact?path=a+b%26c.py&symbol=X%23y"


def test_cli_add_reports_a_bootstrap_that_did_not_index(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(eios_cli, "call", lambda *a, **k: {"project": None, "sync": None})
    args = argparse.Namespace(api="http://x", path="nope", json=False)
    with pytest.raises(SystemExit, match="was not indexed"):
        eios_cli.cmd_add(args)


def test_http_ignores_system_proxy_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Loopback traffic must never be sent to a corporate or sandbox proxy."""
    import http.server
    import threading

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"direct": true}')

        def log_message(self, *_: object) -> None:
            return

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        monkeypatch.setenv("http_proxy", "http://127.0.0.1:9")  # nothing listens there
        monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
        status, _, data = smoke_stack.http("GET", f"http://127.0.0.1:{server.server_port}/")
    finally:
        server.shutdown()
    assert status == 200 and data == {"direct": True}


def test_workspace_resolution_rejects_dangerous_or_unusable_values(tmp_path: Path) -> None:
    with pytest.raises(SystemExit, match="empty"):
        smoke_stack.resolve_workspace("  ")
    for bad in ("/data/my #repos", "/data/$HOME/x", '/data/"q"', "/data/it's"):
        with pytest.raises(SystemExit, match="Docker Compose"):
            smoke_stack.resolve_workspace(bad)
    with pytest.raises(SystemExit, match="home directory or a drive root"):
        smoke_stack.resolve_workspace(str(Path.home()))
    with pytest.raises(SystemExit, match="home directory or a drive root"):
        smoke_stack.resolve_workspace("/")
    assert smoke_stack.resolve_workspace(str(tmp_path)) == tmp_path
    relative = smoke_stack.resolve_workspace("workspace")
    assert relative.is_absolute() and relative == (smoke_stack.ROOT / "workspace").resolve()


def test_setup_refuses_a_home_directory_workspace_and_writes_nothing(checkout: Path) -> None:
    with pytest.raises(SystemExit, match="home directory"):
        setup_local.prepare_env(_args(workspace_dir=str(Path.home())))
    assert not (checkout / ".env").exists()


def test_a_new_env_is_private_and_never_follows_a_symlink(checkout: Path) -> None:
    setup_local.prepare_env(_args())
    mode = (checkout / ".env").stat().st_mode & 0o777
    if sys.platform != "win32":
        assert mode == 0o600
    (checkout / ".env").unlink()
    (checkout / ".env").symlink_to(checkout / "elsewhere")
    with pytest.raises(SystemExit, match="symbolic link"):
        setup_local.write_new_env("A=1\n")
    assert not (checkout / "elsewhere").exists()
    (checkout / ".env").unlink()
    setup_local.write_new_env("A=1\n")
    with pytest.raises(FileExistsError):  # exclusive create: never replaces a file made meanwhile
        setup_local.write_new_env("A=2\n")


def test_env_only_creates_the_file_and_starts_nothing(
    checkout: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(checkout)  # main() changes directory; the fixture restores it afterwards
    monkeypatch.setattr(sys, "argv", ["setup_local.py", "--env-only"])
    monkeypatch.setattr(setup_local, "start_stack", lambda: pytest.fail("must not start"))
    monkeypatch.setattr(setup_local, "preflight", lambda *a, **k: pytest.fail("no preflight"))
    assert setup_local.main() == 0
    values = smoke_stack.read_env(checkout / ".env")
    assert values["EIOS_POSTGRES_PASSWORD"] != "change-me-locally"


@pytest.mark.parametrize("name", ["setup_local", "smoke_stack", "eios_cli"])
def test_the_helper_scripts_parse_as_python_3_9(name: str) -> None:
    """They run on whatever Python the user has; no match statements or 3.10+ syntax."""
    import ast

    source = (SCRIPTS / f"{name}.py").read_text(encoding="utf-8")
    ast.parse(source, feature_version=(3, 9))
    assert "from __future__ import annotations" in source
