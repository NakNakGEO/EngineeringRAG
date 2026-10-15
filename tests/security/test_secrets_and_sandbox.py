from __future__ import annotations

import hashlib
import stat
from pathlib import Path

import pytest

from eios_policy import (
    SandboxSpec,
    SandboxUnavailableError,
    SandboxViolationError,
    SecretError,
    SecretsBroker,
    SubprocessSandbox,
    load_root_policy,
)

REPO = Path(__file__).resolve().parents[2]


# ---- secrets broker ----
def test_secret_requires_grant_and_never_reprs_the_value() -> None:
    broker = SecretsBroker({"EIOS_SECRET_API_TOKEN": "s3cr3t-value"})
    with pytest.raises(SecretError, match="not granted"):
        broker.get("API_TOKEN", granted=[])
    value = broker.get("API_TOKEN", granted=["API_TOKEN"])
    assert value.reveal() == "s3cr3t-value"
    assert "s3cr3t" not in repr(value) and "s3cr3t" not in str(value)
    assert broker.names() == ["API_TOKEN"]
    with pytest.raises(SecretError, match="not configured"):
        broker.get("OTHER", granted=["OTHER"])


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("PROD_DATABASE_PASSWORD", "x"),
        ("DB_PASSWORD", "x"),
        ("POSTGRES_PASSWORD", "x"),
        ("SQLSERVER_CONNECTION_STRING", "x"),
        ("MYSQL_PWD", "x"),
        ("API_KEY", "postgresql://u:p@host/db"),
        ("API_KEY", "mongodb+srv://u:p@cluster/db"),
        ("SETTING", "Server=sql01;Database=app;User Id=sa;Password=hunter2;"),
        ("REDIS_URL", "redis://:pw@cache:6379"),
    ],
)
def test_database_credentials_are_never_handed_out(name: str, value: str) -> None:
    broker = SecretsBroker({f"EIOS_SECRET_{name}": value})
    with pytest.raises(SecretError):
        broker.get(name, granted=[name])
    assert name not in broker.names()


def test_invalid_secret_names_are_rejected() -> None:
    broker = SecretsBroker({})
    for bad in ("../x", "a b", "", "1abc", "x" * 100):
        with pytest.raises(SecretError):
            broker.get(bad, granted=[bad])


# ---- sandbox ----
def script(tmp: Path, body: str, name: str = "tool.sh") -> tuple[Path, str]:
    path = tmp / name
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(0o755)
    return path, hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def sandbox() -> SubprocessSandbox:
    return SubprocessSandbox(
        load_root_policy(REPO / "policy" / "root_policy.yaml"), require_network_isolation=False
    )


async def test_runs_pinned_script_with_scrubbed_environment(
    tmp_path: Path, sandbox: SubprocessSandbox, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EIOS_DATABASE_URL", "postgresql://leak")
    monkeypatch.setenv("SOME_TOKEN", "leak")
    path, digest = script(tmp_path, "cat; echo; env | sort\n")
    result = await sandbox.run(SandboxSpec(argv=[str(path)], sha256=digest, stdin=b"hello"))
    out = result.stdout.decode()
    assert result.exit_code == 0 and "hello" in out
    assert "leak" not in out and "EIOS_" not in out and "SOME_TOKEN" not in out


async def test_hash_mismatch_relative_paths_and_db_clients_are_refused(
    tmp_path: Path, sandbox: SubprocessSandbox
) -> None:
    path, digest = script(tmp_path, "echo hi\n")
    with pytest.raises(SandboxViolationError, match="SHA-256"):
        await sandbox.run(SandboxSpec(argv=[str(path)], sha256="0" * 64))
    path.write_text("#!/bin/sh\necho tampered\n")
    with pytest.raises(SandboxViolationError, match="SHA-256"):
        await sandbox.run(SandboxSpec(argv=[str(path)], sha256=digest))
    with pytest.raises(SandboxViolationError, match="absolute"):
        await sandbox.run(SandboxSpec(argv=["tool.sh"], sha256=digest))
    psql, pdigest = script(tmp_path, "echo connecting\n", name="psql")
    with pytest.raises(SandboxViolationError, match="database client"):
        await sandbox.run(SandboxSpec(argv=[str(psql)], sha256=pdigest))
    with pytest.raises(SandboxViolationError, match="database client"):
        await sandbox.run(SandboxSpec(argv=[str(path), "/usr/bin/psql"], sha256=digest))


async def test_world_writable_executables_are_refused(
    tmp_path: Path, sandbox: SubprocessSandbox
) -> None:
    path, digest = script(tmp_path, "echo hi\n")
    path.chmod(path.stat().st_mode | stat.S_IWOTH)
    with pytest.raises(SandboxViolationError, match="world-writable"):
        await sandbox.run(SandboxSpec(argv=[str(path)], sha256=digest))


async def test_timeout_kills_the_whole_process_group(
    tmp_path: Path, sandbox: SubprocessSandbox
) -> None:
    path, digest = script(tmp_path, "sleep 30 &\nsleep 30\n")
    result = await sandbox.run(SandboxSpec(argv=[str(path)], sha256=digest, timeout_seconds=0.5))
    assert result.timed_out and result.duration_ms < 5000


async def test_output_is_capped(tmp_path: Path, sandbox: SubprocessSandbox) -> None:
    path, digest = script(tmp_path, "yes aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n")
    result = await sandbox.run(
        SandboxSpec(argv=[str(path)], sha256=digest, max_output_bytes=10_000)
    )
    assert result.truncated and len(result.stdout) <= 10_000


async def test_network_isolation_blocks_connections_or_fails_closed(tmp_path: Path) -> None:
    root = load_root_policy(REPO / "policy" / "root_policy.yaml")
    strict = SubprocessSandbox(root, require_network_isolation=True)
    path, digest = script(
        tmp_path,
        "if command -v python3 >/dev/null; then python3 - <<'EOF'\n"
        "import socket\ntry:\n"
        " socket.create_connection(('93.184.216.34',80),timeout=2);print('CONNECTED')\n"
        "except OSError as e:\n print('BLOCKED')\nEOF\nelse echo BLOCKED; fi\n",
    )
    if await strict.network_isolation_available():
        result = await strict.run(SandboxSpec(argv=[str(path)], sha256=digest))
        assert result.network_isolated and b"BLOCKED" in result.stdout
    else:
        with pytest.raises(SandboxUnavailableError):
            await strict.run(SandboxSpec(argv=[str(path)], sha256=digest))
