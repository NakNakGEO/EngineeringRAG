"""One-command local setup for Engineering OS: preflight, .env, build, start, smoke test.

Standard library only; works on Windows, macOS and Linux (run it with ``python`` or ``py``).

    python scripts/setup_local.py                       # everything
    python scripts/setup_local.py --workspace-dir ~/src # the folder that holds your repositories
    python scripts/setup_local.py --check               # only look at the machine, change nothing
    python scripts/setup_local.py --down                # stop the stack (data is kept)

It needs Docker with Compose and git. It never deletes data, never overwrites an existing .env,
and publishes every port on 127.0.0.1 only (the compose file enforces that).
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from smoke_stack import read_env, resolve_workspace

ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = ROOT / ".env"
ENV_EXAMPLE = ROOT / ".env.example"
DEFAULT_PORTS = {
    "EIOS_POSTGRES_PORT": 5432,
    "EIOS_API_PORT": 8000,
    "EIOS_MCP_PORT": 8082,
    "EIOS_WEB_PORT": 8080,
}
MIN_FREE_GB = 8.0  # three images of ~1.7 GB (shared layers), build cache, the database


def say(message: str = "") -> None:
    print(message, flush=True)


def run(
    cmd: list[str], check: bool = False, capture: bool = True, timeout: float | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        cmd,
        cwd=ROOT,
        capture_output=capture,
        text=True,
        check=check,
        timeout=timeout,
    )


def have(cmd: list[str]) -> tuple[bool, str]:
    try:
        done = run(cmd, timeout=30)
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
        return False, str(exc)
    return done.returncode == 0, (done.stdout or done.stderr).strip()


def port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def set_env_value(text: str, key: str, value: str) -> str:
    pattern = re.compile(rf"^{re.escape(key)}=.*$", re.M)
    line = f"{key}={value}"
    if pattern.search(text):
        return pattern.sub(lambda _m: line, text, count=1)
    return text.rstrip("\n") + "\n" + line + "\n"


# -- steps -------------------------------------------------------------------------------------
def preflight(ports: dict[str, int], stack_running: bool) -> list[str]:
    """Print what was found; return the list of blocking problems."""
    problems: list[str] = []
    say("1. Checking this machine")
    if sys.version_info < (3, 9):  # noqa: UP036 - the script itself must run on older hosts
        problems.append(
            f"Python 3.9+ is needed to run this script (found {sys.version.split()[0]})"
        )
    ok, out = have(["git", "--version"])
    say("   git            %s" % (out if ok else "NOT FOUND"))
    if not ok:
        problems.append("git is not installed (https://git-scm.com/downloads)")
    ok, out = have(["docker", "--version"])
    say("   docker         %s" % (out if ok else "NOT FOUND"))
    if not ok:
        problems.append("Docker is not installed (https://docs.docker.com/get-docker/)")
    else:
        ok_compose, out = have(["docker", "compose", "version"])
        say("   docker compose %s" % (out if ok_compose else "NOT FOUND"))
        if not ok_compose:
            problems.append("Docker Compose v2 is missing (`docker compose version` must work)")
        ok_daemon, out = have(["docker", "info", "--format", "{{.ServerVersion}}"])
        say("   docker daemon  %s" % (f"running, server {out}" if ok_daemon else "NOT RUNNING"))
        if not ok_daemon and "permission denied" in out.lower():
            problems.append(
                "Docker is running but your user may not use it. On Linux add yourself to the "
                "docker group (sudo usermod -aG docker $USER, then log out and in) or use "
                "rootless Docker. Do not run this script with sudo: it would leave root-owned "
                "files behind."
            )
        elif not ok_daemon:
            problems.append(
                "The Docker daemon is not running (start Docker Desktop, or: sudo systemctl "
                "start docker). Docker said: " + out.splitlines()[-1][:200]
                if out
                else ""
            )
    free_gb = shutil.disk_usage(ROOT).free / 1e9
    say(f"   free disk      {free_gb:.1f} GB (needs about {MIN_FREE_GB:.0f})")
    if free_gb < MIN_FREE_GB:
        problems.append(f"Less than {MIN_FREE_GB:.0f} GB of free disk space")
    if not stack_running:
        for key, port in ports.items():
            free = port_free(port)
            say(f"   port {port:<5d}     {'free' if free else 'IN USE'} ({key})")
            if not free:
                problems.append(
                    f"Port {port} ({key}) is already used by another program. Re-run with "
                    "--port-offset 100 to move all ports, or edit .env."
                )
    return problems


def write_new_env(text: str) -> None:
    """Create .env atomically, readable by the owner only; never through a symbolic link."""
    if ENV_FILE.is_symlink():
        raise SystemExit(".env is a symbolic link (to a missing file): remove it and re-run")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    fd = os.open(ENV_FILE, flags, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(text.encode("utf-8"))  # LF endings on every OS


def prepare_env(args: argparse.Namespace) -> tuple[dict[str, str], bool]:
    """Create .env if it does not exist. Returns (values, created)."""
    say("2. Configuration (.env)")
    created = False
    if ENV_FILE.exists() or ENV_FILE.is_symlink():
        say("   .env already exists: leaving it untouched")
        if args.port_offset or args.admin_token or args.workspace_dir:
            say("   (options that change .env were ignored; edit the file or remove it first)")
    else:
        if not ENV_EXAMPLE.exists():
            raise SystemExit(".env.example is missing: is this a complete checkout?")
        text = ENV_EXAMPLE.read_text(encoding="utf-8")
        password = secrets.token_urlsafe(24)  # letters, digits, '-' and '_': URL-safe
        user = read_env(ENV_EXAMPLE).get("EIOS_POSTGRES_USER", "eios")
        db = read_env(ENV_EXAMPLE).get("EIOS_POSTGRES_DB", "eios")
        ports = {k: v + args.port_offset for k, v in DEFAULT_PORTS.items()}
        text = set_env_value(text, "EIOS_POSTGRES_PASSWORD", password)
        text = set_env_value(
            text,
            "EIOS_DATABASE_URL",
            f"postgresql+psycopg://{user}:{password}@localhost:{ports['EIOS_POSTGRES_PORT']}/{db}",
        )
        for key, value in ports.items():
            text = set_env_value(text, key, str(value))
        if args.workspace_dir:
            workspace = resolve_workspace(str(Path(args.workspace_dir).expanduser().absolute()))
            text = set_env_value(text, "EIOS_WORKSPACE_DIR", workspace.as_posix())
            text = set_env_value(text, "EIOS_WORKSPACE_ROOTS", workspace.as_posix())
        if args.admin_token:
            text = set_env_value(text, "EIOS_ADMIN_TOKEN", secrets.token_urlsafe(24))
        write_new_env(text)
        created = True
        say("   wrote .env with a random database password (kept on this machine only)")
        if args.admin_token:
            say("   generated EIOS_ADMIN_TOKEN (see .env): never give it to an LLM or agent, and")
            say("   do not use it in a session where an agent can read files as you")
    values = read_env(ENV_FILE)
    raw = values.get("EIOS_WORKSPACE_DIR", "./workspace")
    if not Path(raw).expanduser().is_absolute():
        raw = str((ROOT / raw).resolve())
    workspace_dir = resolve_workspace(raw)
    workspace_dir.mkdir(parents=True, exist_ok=True)
    say(f"   workspace folder (mounted read-only at /workspace): {workspace_dir}")
    say("   Everything under it can be indexed as text, including config files: keep secrets")
    say("   out of tracked files, and point this at the folder that holds only your repositories.")
    return values, created


def compose_project_name() -> str:
    """The Compose project this checkout runs as (flag/env/`name:` precedence is Compose's own)."""
    ok, out = have(["docker", "compose", "config", "--format", "json"])
    if ok:
        with contextlib.suppress(ValueError, KeyError):
            return str(json.loads(out)["name"])
    return os.environ.get("COMPOSE_PROJECT_NAME", "engineering-intelligence-os")


def warn_stale_volume(created_env: bool) -> None:
    """A freshly generated password cannot open a database volume made with another one."""
    if not created_env:
        return
    volume = f"{compose_project_name()}_eios-postgres-data"
    ok, out = have(["docker", "volume", "ls", "-q", "--filter", f"name=^{volume}$"])
    if ok and volume in out.split():
        say(f"   WARNING: this install already has a database volume ({volume}).")
        say("   It was initialised with a different password and the new .env cannot open it.")
        say("   Either restore the old .env, or start fresh (DELETES that data):")
        say("       docker compose down -v")


def start_stack() -> bool:
    say("3. Building and starting the stack (the first build downloads about 2 GB)")
    done = run(["docker", "compose", "up", "-d", "--build", "--wait"], capture=False)
    if done.returncode == 0:
        say("   all services are healthy")
        return True
    say("   the stack did not become healthy. Service status:")
    run(["docker", "compose", "ps", "-a"], capture=False)
    say("   last log lines of the services that failed:")
    run(["docker", "compose", "logs", "--tail", "25", "migrate", "api"], capture=False)
    say("   Common causes:")
    say(
        "     'Too Many Requests' / 429      Docker Hub rate limit: run `docker login`, then re-run"
    )
    say("     'CERTIFICATE_VERIFY_FAILED'    a proxy re-signs HTTPS: see docs/development.md")
    say("     'port is already allocated'    use --port-offset 100 (only before .env exists)")
    say("     'password authentication'      an old database volume: see the warning above")
    say("   Full list: docs/operations/local-setup.md (Troubleshooting)")
    return False


def run_smoke(values: dict[str, str]) -> int:
    say("4. Smoke test (indexes a sample project, builds context, checks MCP, policy, events)")
    cmd = [sys.executable, str(ROOT / "scripts" / "smoke_stack.py"), "--wait", "120"]
    return subprocess.run(cmd, cwd=ROOT, check=False).returncode  # noqa: S603


def print_next_steps(values: dict[str, str]) -> None:
    api = values.get("EIOS_API_PORT", "8000")
    mcp = values.get("EIOS_MCP_PORT", "8082")
    web = values.get("EIOS_WEB_PORT", "8080")
    workspace = values.get("EIOS_WORKSPACE_DIR", "./workspace")
    say()
    say("Engineering OS is running (all ports are loopback-only):")
    say(f"   Web UI      http://127.0.0.1:{web}")
    say(f"   API + docs  http://127.0.0.1:{api}/docs")
    say(f"   MCP         http://127.0.0.1:{mcp}/mcp")
    say()
    say("Use it on your own project (works the same in PowerShell, cmd and bash):")
    say(f"   1. put or clone the repository inside {workspace}")
    say("   2. python scripts/eios_cli.py add <folder-name>")
    say('   3. python scripts/eios_cli.py context "your question" -p <folder-name>')
    say("   4. connect your model: docs/integrations/README.md")
    say()
    say("Stop: python scripts/setup_local.py --down     Logs: docker compose logs -f")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--check", action="store_true", help="only check this machine")
    parser.add_argument("--down", action="store_true", help="stop the stack (data is kept)")
    parser.add_argument("--no-smoke", action="store_true", help="skip the smoke test")
    parser.add_argument("--workspace-dir", help="folder that holds your repositories")
    parser.add_argument("--port-offset", type=int, default=0, help="add N to every host port")
    parser.add_argument("--admin-token", action="store_true", help="generate EIOS_ADMIN_TOKEN")
    parser.add_argument("--env-only", action="store_true", help="only create .env, start nothing")
    args = parser.parse_args()
    os.chdir(ROOT)

    if args.env_only:
        prepare_env(args)
        return 0

    if args.down:
        return run(["docker", "compose", "down"], capture=False).returncode

    values = read_env(ENV_FILE)
    ports = {
        key: int(values.get(key, str(default + (0 if ENV_FILE.exists() else args.port_offset))))
        for key, default in DEFAULT_PORTS.items()
    }
    running = False
    if shutil.which("docker"):
        ok, out = have(["docker", "compose", "ps", "-q"])
        running = ok and bool(out.strip())
    problems = preflight(ports, running)
    if problems:
        say()
        for problem in problems:
            say("PROBLEM: " + problem)
        return 1
    if args.check:
        say()
        say("Everything needed is in place. Run without --check to set up.")
        return 0

    values, created = prepare_env(args)
    warn_stale_volume(created)
    if not start_stack():
        return 1
    code = 0
    if not args.no_smoke:
        code = run_smoke(values)
    print_next_steps(values)
    return code


if __name__ == "__main__":
    sys.exit(main())
