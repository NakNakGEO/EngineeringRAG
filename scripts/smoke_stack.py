"""End-to-end smoke test for a RUNNING Engineering OS stack (``docker compose up`` or host-run).

Standard library only, so it runs on Windows, macOS and Linux with any Python 3.9+ and git.

    python scripts/smoke_stack.py                    # reads ports and workspace from .env
    python scripts/smoke_stack.py --api http://127.0.0.1:8000 --mcp http://127.0.0.1:8082/mcp

What it checks, in order (each check reports PASS / FAIL / SKIP, the exit code is non-zero on any
FAIL): readiness of the API, MCP server and web UI (including the database behind them); that a
small git project placed in the workspace can be indexed; that context for a named function comes
back ready and with its real source; that an unknown name is reported as missing instead of being
papered over; search and impact analysis; that the Root Policy denies an external-database request;
a knowledge round trip; the run's event log being gap-free; the human-approval gate; and the MCP
tool surface (initialize, list the eight tools, call get_context).

It writes one folder, ``eios-smoke-sample``, into the workspace directory (reused on later runs)
and creates one project and one knowledge note in the Engineering OS database. It never touches
anything else and has no way to reach a database other than through the Engineering OS API.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SAMPLE_NAME = "eios-smoke-sample"
SAMPLE_MARKER = ".eios-smoke-sample"
EXPECTED_MCP_TOOLS = {
    "bootstrap_project",
    "get_context",
    "search_knowledge",
    "request_capability",
    "request_specialist",
    "get_evidence",
    "report_result",
    "get_run_state",
}
SAMPLE_FILES = {
    "README.md": "# Smoke sample\n\nA tiny project used to verify Engineering OS.\n",
    "docs/adr/0001-retry-backoff.md": (
        "# ADR 0001: Retry transient failures with exponential backoff\n\n"
        "Status: Accepted\n\nPayment calls fail transiently, so the gateway retries with an "
        "exponentially growing delay. We chose backoff over fixed delays because it protects the "
        "provider during an outage.\n"
    ),
    "sample/__init__.py": "",
    "sample/clock.py": "import time\n\n\ndef now_ms():\n    return int(time.time() * 1000)\n",
    "sample/retry.py": (
        "from sample.clock import now_ms\n\n\nclass RetryPolicy:\n"
        "    def __init__(self, base_ms=100):\n        self.base_ms = base_ms\n\n"
        "    def compute_backoff(self, attempt):\n"
        "        started = now_ms()\n        return started + self.base_ms * (2 ** attempt)\n"
    ),
    "sample/gateway.py": (
        "from sample.retry import RetryPolicy\n\n\n"
        "def charge(amount):\n    policy = RetryPolicy()\n    return policy.compute_backoff(1)\n"
    ),
    "tests/test_retry.py": (
        "from sample.retry import RetryPolicy\n\n\n"
        "def test_backoff_grows():\n    p = RetryPolicy()\n"
        "    assert p.compute_backoff(2) > p.compute_backoff(1)\n"
    ),
}
SYMBOL = "RetryPolicy.compute_backoff"
SYMBOL_ID = "symbol:sample/retry.py::RetryPolicy.compute_backoff"


class SkipError(Exception):
    """A check that cannot run because something it depends on already failed."""


class CheckError(Exception):
    """A check whose expectation was not met."""


@dataclass
class Result:
    name: str
    outcome: str  # PASS | FAIL | SKIP
    detail: str = ""
    seconds: float = 0.0


@dataclass
class Context:
    api: str
    mcp: str
    web: str
    workspace_dir: Path
    container_prefix: str
    admin_token: str | None
    state: dict[str, Any] = field(default_factory=dict)


# -- tiny HTTP client ----------------------------------------------------------------------------
# Everything here talks to this computer. A system or environment proxy must never see it: it would
# break the checks and receive the admin and MCP tokens.
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def http(
    method: str,
    url: str,
    body: Any = None,
    headers: dict[str, str] | None = None,
    timeout: float = 120.0,
) -> tuple[int, dict[str, str], Any]:
    """Returns (status, lower-cased headers, parsed JSON | text). Never raises on HTTP errors."""
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(url, data=data, method=method)  # noqa: S310 - local URLs
    request.add_header("accept", "application/json")
    if data is not None:
        request.add_header("content-type", "application/json")
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    try:
        with OPENER.open(request, timeout=timeout) as response:
            status, raw, got = response.status, response.read(), dict(response.headers.items())
    except urllib.error.HTTPError as exc:
        status, raw, got = exc.code, exc.read(), dict(exc.headers.items())
    lowered = {k.lower(): v for k, v in got.items()}
    text = raw.decode("utf-8", "replace")
    try:
        return status, lowered, json.loads(text)
    except ValueError:
        return status, lowered, text


def expect(condition: bool, message: str) -> None:
    if not condition:
        raise CheckError(message)


def need(ctx: Context, key: str) -> Any:
    if key not in ctx.state:
        raise SkipError(f"an earlier check did not produce {key}")
    return ctx.state[key]


# -- the checks ----------------------------------------------------------------------------------
def check_api_ready(ctx: Context) -> str:
    status, _, body = http("GET", ctx.api + "/health/ready", timeout=15)
    expect(status == 200, f"API /health/ready returned {status}: {str(body)[:200]}")
    checks = body.get("checks", []) if isinstance(body, dict) else []
    names = ", ".join("{}={}".format(c.get("name"), c.get("status")) for c in checks)
    expect(all(c.get("status") == "ok" for c in checks), "a dependency is unhealthy: " + names)
    return names or "ok"


def check_mcp_ready(ctx: Context) -> str:
    base = ctx.mcp.rsplit("/mcp", 1)[0]
    status, _, body = http("GET", base + "/health/ready", timeout=15)
    expect(status == 200, f"MCP /health/ready returned {status}: {str(body)[:200]}")
    return "ok"


def check_web_ui(ctx: Context) -> str:
    status, _, body = http("GET", ctx.web + "/", timeout=15)
    expect(
        status == 200 and "<html" in str(body).lower(),
        f"web UI did not serve HTML ({status})",
    )
    status, _, _ = http("GET", ctx.web + "/api/health/ready", timeout=15)
    expect(status == 200, f"web UI could not reach the API through /api ({status})")
    return "UI served; /api proxy reaches the API"


def create_sample(workspace: Path) -> Path:
    target = workspace / SAMPLE_NAME
    marker = target / SAMPLE_MARKER
    if marker.exists() and (target / ".git" / "HEAD").exists():
        return target  # reuse: same project identity, no clutter
    if marker.exists():  # an earlier run stopped half-way: start the sample over
        shutil.rmtree(target)
    if target.exists() and any(target.iterdir()):
        raise CheckError(f"{target} exists and was not created by this script; move it away")
    try:
        for rel, text in SAMPLE_FILES.items():
            path = target / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(text.encode("utf-8"))  # LF endings on every OS (and Python 3.9)
        (target / SAMPLE_MARKER).write_text("created by scripts/smoke_stack.py\n", encoding="utf-8")
    except OSError as exc:
        raise CheckError(
            f"cannot write to the workspace directory {workspace} ({exc}). "
            "Set EIOS_WORKSPACE_DIR to a folder you can write to, or pass --workspace-dir."
        ) from exc
    env = {
        k: v
        for k, v in os.environ.items()
        if k in {"PATH", "SYSTEMROOT", "HOME", "USERPROFILE", "TMP", "TEMP"}
    }
    env.update(
        GIT_CONFIG_NOSYSTEM="1",
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_AUTHOR_NAME="smoke",
        GIT_AUTHOR_EMAIL="smoke@example.invalid",
        GIT_COMMITTER_NAME="smoke",
        GIT_COMMITTER_EMAIL="smoke@example.invalid",
        GIT_TERMINAL_PROMPT="0",
    )
    steps = [
        ["init", "-q"],
        ["symbolic-ref", "HEAD", "refs/heads/main"],  # `init -b` needs git 2.28
        ["add", "-A"],
        ["commit", "-q", "-m", "smoke sample"],
    ]
    for step in steps:
        try:
            done = subprocess.run(  # noqa: S603
                ["git", "-C", str(target), *step],  # noqa: S607
                capture_output=True,
                text=True,
                env=env,
                check=False,
            )
        except FileNotFoundError:
            raise CheckError(
                "git is not installed or not on PATH (needed to create the sample)"
            ) from None
        if done.returncode != 0:
            raise CheckError("git {} failed: {}".format(" ".join(step), done.stderr.strip()[:200]))
    return target


def check_index_sample(ctx: Context) -> str:
    create_sample(ctx.workspace_dir)
    path = ctx.container_prefix.rstrip("/") + "/" + SAMPLE_NAME
    status, _, body = http(
        "POST",
        ctx.api + "/projects/bootstrap",
        {"path": path, "sync": "always", "wait": True},
        timeout=300,
    )
    expect(
        status == 200,
        f"bootstrap of {path} returned {status}: {str(body)[:300]}. Typical causes: the path is "
        "outside the approved workspace (EIOS_WORKSPACE_DIR in .env does not match the folder "
        f"mounted at {ctx.container_prefix}), or the containers (user id 10001) cannot read the "
        "folder: see Troubleshooting in docs/operations/local-setup.md.",
    )
    expect(
        bool(isinstance(body, dict) and body.get("project") is not None and body.get("sync")),
        f"bootstrap did not index the sample: {str(body)[:300]}",
    )
    project, sync = body["project"], body.get("sync") or {}
    expect(sync.get("symbols_total", 0) >= 5, f"indexed too few symbols: {sync}")
    ctx.state["project_id"] = project["id"]
    return "{} files, {} symbols, state {}".format(
        sync.get("files_total"),
        sync.get("symbols_total"),
        sync.get("state"),
    )


def check_symbol_lookup(ctx: Context) -> str:
    pid = need(ctx, "project_id")
    status, _, rows = http("GET", f"{ctx.api}/projects/{pid}/symbols?q=compute_backoff")
    expect(status == 200 and isinstance(rows, list), f"symbol search failed ({status})")
    names = [r.get("qualified_name") for r in rows]
    expect(SYMBOL in names, f"{SYMBOL} not found among {names}")
    return "found " + SYMBOL


def check_context_ready(ctx: Context) -> str:
    pid = need(ctx, "project_id")
    status, _, body = http(
        "POST",
        ctx.api + "/context/build",
        {
            "text": f"What does `{SYMBOL}` do and what does it depend on?",
            "project_id": pid,
            "risk": "low",
        },
        timeout=120,
    )
    expect(status == 200, f"context build returned {status}: {str(body)[:300]}")
    pack = body["pack"]
    ctx.state["run_id"] = body.get("run_id")
    expect(pack["gap"]["ready_to_act"], "pack not ready: {}".format(pack["gap"]["missing_context"]))
    first = pack["items"][0]["candidate"]
    expect(first["id"] == SYMBOL_ID, "top item is {}, expected {}".format(first["id"], SYMBOL_ID))
    expect("def compute_backoff" in first["content"], "the real source is not in the pack")
    expect(
        "symbol:sample/clock.py::now_ms" in pack["critical_evidence_ids"],
        "its dependency now_ms is not protected evidence",
    )
    coverage = pack["gap"]["coverage"]
    return f"ready at {pack['level']}, coverage {coverage:.2f}, {len(pack['items'])} items"


def check_unknown_name_is_reported(ctx: Context) -> str:
    pid = need(ctx, "project_id")
    status, _, body = http(
        "POST",
        ctx.api + "/context/build",
        {
            "text": "Explain how QuantumLedgerSynchronizer works",
            "project_id": pid,
            "symbols": ["QuantumLedgerSynchronizer"],
            "record_run": False,
        },
        timeout=120,
    )
    expect(status == 200, f"context build returned {status}")
    gap = body["pack"]["gap"]
    kinds = {m["kind"] for m in gap["missing_context"]}
    expect(not gap["ready_to_act"], "an unknown name was reported as ready")
    expect("unresolved_entity" in kinds, f"the missing name was not reported: {sorted(kinds)}")
    return "not ready, gap reported: " + ", ".join(sorted(kinds))


def check_search(ctx: Context) -> str:
    pid = need(ctx, "project_id")
    status, _, body = http(
        "POST",
        ctx.api + "/retrieval/search",
        {"text": "retry backoff delay", "project_id": pid, "limit": 10},
    )
    expect(status == 200, f"search returned {status}")
    ids = [i["candidate"]["id"] for i in body["items"]]
    expect(any("sample/retry.py" in i for i in ids), f"retry.py not in results: {ids[:5]}")
    return f"{len(ids)} results; retry.py found"


def check_impact(ctx: Context) -> str:
    pid = need(ctx, "project_id")
    status, _, report = http("GET", f"{ctx.api}/projects/{pid}/impact?path=sample/retry.py")
    expect(status == 200 and report.get("resolved") is True, "impact did not resolve the file")
    dependents = {e["path"] for e in report["dependents"]}
    expect("sample/gateway.py" in dependents, "gateway.py should depend on retry.py")
    expect("tests/test_retry.py" in report["tests"], "its test was not found")
    return "risk {}; dependents {}; tests {}".format(
        report["risk"],
        sorted(p for p in dependents if p),
        report["tests"],
    )


def check_policy_denies_external_database(ctx: Context) -> str:
    status, _, root = http("GET", ctx.api + "/policy/root")
    expect(status == 200, f"Root Policy summary returned {status}")
    status, _, decision = http(
        "POST",
        ctx.api + "/policy/evaluate",
        {
            "action": "capability.invoke",
            "capability": "external_db_connector",
            "target": "prod-sql-01:1433",
        },
    )
    expect(status == 200, f"policy evaluate returned {status}")
    expect(
        decision.get("effect") == "deny",
        f"an external-database request was NOT denied: {str(decision)[:300]}",
    )
    return "denied by {} (root policy {})".format(
        decision.get("rule_id"),
        str(root.get("version", "?")),
    )


def check_knowledge_round_trip(ctx: Context) -> str:
    pid = need(ctx, "project_id")
    token = uuid.uuid4().hex[:10]
    status, _, item = http(
        "POST",
        ctx.api + "/knowledge",
        {
            "project_id": pid,
            "title": f"Smoke note {token}",
            "content": f"The smoke test marker {token} proves knowledge storage works.",
            "created_by": "smoke-test",
            "provenance": [{"source": "scripts/smoke_stack.py", "actor": "smoke-test"}],
        },
    )
    expect(status == 201, f"creating knowledge returned {status}: {str(item)[:300]}")
    expect(item["trust"] in {"RAW", "OBSERVED"}, "a client-created note got trust " + item["trust"])
    status, _, hits = http(
        "POST", ctx.api + "/knowledge/search", {"query": token, "project_id": pid, "limit": 5}
    )
    expect(status == 200, f"knowledge search returned {status}")
    expect(any(h["item"]["id"] == item["id"] for h in hits), "the stored note was not found")
    return "stored with trust {} and found again".format(item["trust"])


def check_event_log(ctx: Context) -> str:
    run_id = need(ctx, "run_id")
    status, _, page = http("GET", f"{ctx.api}/runs/{run_id}/events?limit=500")
    expect(status == 200, f"events returned {status}")
    seqs = [e["seq"] for e in page["items"]]
    expect(len(seqs) >= 3, f"too few events recorded: {seqs}")
    expect(seqs == list(range(1, len(seqs) + 1)), f"the event sequence has gaps: {seqs}")
    return f"{len(seqs)} events, seq 1..{len(seqs)} with no gaps"


def check_approval_gate(ctx: Context) -> str:
    target = f"{ctx.api}/approvals/{uuid.uuid4()}/decision"
    body = {"approve": True, "decided_by": "smoke-test"}
    status, _, _ = http("POST", target, body)
    if not ctx.admin_token:
        expect(status == 403, f"approvals should be disabled without a token, got {status}")
        return "approvals disabled (EIOS_ADMIN_TOKEN empty): the secure default"
    expect(status == 401, f"a missing admin token should give 401, got {status}")
    status, _, _ = http("POST", target, body, {"X-EIOS-Admin-Token": ctx.admin_token + "x"})
    expect(status == 401, f"a wrong admin token should give 401, got {status}")
    status, _, _ = http("POST", target, body, {"X-EIOS-Admin-Token": ctx.admin_token})
    expect(
        status == 404,
        f"the right token should pass auth (404 unknown approval), got {status}",
    )
    return "401 without or with a wrong token; the right token passes (404: no such approval)"


class McpSession:
    """Just enough of MCP streamable HTTP to list and call tools."""

    def __init__(self, url: str, token: str | None) -> None:
        self.url = url
        self.headers = {"accept": "application/json, text/event-stream"}
        if token:
            self.headers["authorization"] = "Bearer " + token
        self.session: str | None = None
        self._id = 0

    def rpc(self, method: str, params: dict[str, Any] | None = None, notify: bool = False) -> Any:
        message: dict[str, Any] = {"jsonrpc": "2.0", "method": method, "params": params or {}}
        if not notify:
            self._id += 1
            message["id"] = self._id
        headers = dict(self.headers)
        if self.session:
            headers["mcp-session-id"] = self.session
        status, got, body = http("POST", self.url, message, headers, timeout=120)
        if got.get("mcp-session-id"):
            self.session = got["mcp-session-id"]
        if notify:
            expect(status in {200, 202}, f"MCP {method} returned {status}")
            return None
        expect(status == 200, f"MCP {method} returned {status}: {str(body)[:300]}")
        payload = body if isinstance(body, dict) else self._from_sse(str(body))
        expect("error" not in payload, "MCP {} failed: {}".format(method, payload.get("error")))
        return payload["result"]

    @staticmethod
    def _from_sse(text: str) -> dict[str, Any]:
        for line in text.splitlines():
            if line.startswith("data:"):
                try:
                    parsed = json.loads(line[5:].strip())
                except ValueError:
                    continue
                if isinstance(parsed, dict):
                    return parsed
        raise CheckError("no JSON-RPC message in the MCP response: " + text[:200])


def check_mcp_tools(ctx: Context) -> str:
    if ctx.state.get("mcp_token"):
        status, _, _ = http("POST", ctx.mcp, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        expect(status == 401, f"MCP answered {status} without its token (expected 401)")
    session = McpSession(ctx.mcp, ctx.state.get("mcp_token"))
    session.rpc(
        "initialize",
        {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "smoke-test", "version": "1"},
        },
    )
    session.rpc("notifications/initialized", notify=True)
    tools = session.rpc("tools/list")["tools"]
    names = {t["name"] for t in tools}
    # the server also exposes a `health` tool; the eight engineering tools must all be present
    expect(
        names >= EXPECTED_MCP_TOOLS,
        f"missing MCP tools: {sorted(EXPECTED_MCP_TOOLS - names)}",
    )
    forbidden = {n for n in names if any(w in n for w in ("sql", "shell", "exec", "database"))}
    expect(not forbidden, f"an unexpected tool is exposed: {sorted(forbidden)}")
    ctx.state["mcp_names"] = sorted(names)
    return "tools: " + ", ".join(sorted(names))


def check_mcp_get_context(ctx: Context) -> str:
    pid = need(ctx, "project_id")
    need(ctx, "mcp_names")
    session = McpSession(ctx.mcp, ctx.state.get("mcp_token"))
    session.rpc(
        "initialize",
        {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "smoke-test", "version": "1"},
        },
    )
    session.rpc("notifications/initialized", notify=True)
    boot = session.rpc(
        "tools/call",
        {
            "name": "bootstrap_project",
            "arguments": {
                "path": ctx.container_prefix.rstrip("/") + "/" + SAMPLE_NAME,
                "sync": "if_needed",
            },
        },
    )
    expect(not boot.get("isError"), f"bootstrap_project returned an error: {str(boot)[:300]}")
    boot_text = "".join(c.get("text", "") for c in boot.get("content", []))
    booted = boot.get("structuredContent") or (
        json.loads(boot_text) if boot_text.strip().startswith("{") else {}
    )
    expect(
        str(booted.get("project_id", "")) == pid,
        f"bootstrap_project named another project: {str(boot)[:300]}",
    )
    result = session.rpc(
        "tools/call",
        {
            "name": "get_context",
            "arguments": {
                "text": f"What does `{SYMBOL}` do?",
                "project_id": pid,
                "risk": "low",
            },
        },
    )
    expect(not result.get("isError"), f"get_context returned an error: {str(result)[:300]}")
    text = "".join(c.get("text", "") for c in result.get("content", []))
    payload = json.loads(text) if text.strip().startswith("{") else {}
    structured = result.get("structuredContent") or payload
    ready = structured.get("ready_to_act")
    expect(ready is True, f"MCP get_context was not ready: {text[:300]}")
    return "ready_to_act through MCP"


CHECKS: list[tuple[str, Callable[[Context], str]]] = [
    ("API ready (database reachable)", check_api_ready),
    ("MCP server ready", check_mcp_ready),
    ("Web UI served and proxying the API", check_web_ui),
    ("Index a sample project from the workspace", check_index_sample),
    ("Symbol lookup", check_symbol_lookup),
    ("Context for a named function is ready and has its source", check_context_ready),
    ("An unknown name is reported as missing, not guessed", check_unknown_name_is_reported),
    ("Search", check_search),
    ("Impact analysis (dependents and tests)", check_impact),
    ("Root Policy denies an external-database request", check_policy_denies_external_database),
    ("Knowledge round trip (trust capped for clients)", check_knowledge_round_trip),
    ("Run event log is gap-free", check_event_log),
    ("Human-approval gate", check_approval_gate),
    ("MCP: initialize and list tools", check_mcp_tools),
    ("MCP: get_context", check_mcp_get_context),
]


# -- plumbing -----------------------------------------------------------------------------------
def resolve_workspace(value: str) -> Path:
    """The workspace folder as an absolute path, or SystemExit with what is wrong with it.

    Used by the setup helper and the smoke test so both agree. It must exist as a real folder,
    must not be your home directory or a drive root (everything below it can be indexed as text),
    and must not contain characters Docker Compose would mangle in ``.env``.
    """
    raw = (value or "").strip()
    if not raw:
        raise SystemExit("the workspace folder is empty: set EIOS_WORKSPACE_DIR in .env")
    if re.search(r"\s#|\$|[\"']", raw):
        raise SystemExit(
            f"the workspace folder '{raw}' contains ' #', '$' or a quote, which Docker Compose "
            "reads wrongly in .env. Pick another folder or a symlink to it."
        )
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = (ROOT / path).resolve()
    if sys.platform == "win32" and raw.replace("\\", "/").startswith("/mnt/"):
        raise SystemExit(f"'{raw}' is a WSL path but this is Windows Python: use C:/... instead")
    if path == Path.home() or path == Path(path.anchor):
        raise SystemExit(
            f"'{path}' is your home directory or a drive root. Everything below the workspace "
            "folder can be indexed as text; choose the folder that holds only your repositories."
        )
    return path


def read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def wait_for_ready(api: str, seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while True:
        try:
            status, _, _ = http("GET", api + "/health/ready", timeout=5)
            if status == 200:
                return True
        except (urllib.error.URLError, OSError):
            pass
        if time.monotonic() >= deadline:
            return False
        time.sleep(2)


PREREQUISITES = ("API ready", "Index a sample project")  # always run: the others build on them


def run(ctx: Context, only: str | None) -> list[Result]:
    results: list[Result] = []
    for name, fn in CHECKS:
        wanted = not only or only.lower() in name.lower() or name.startswith(PREREQUISITES)
        if not wanted:
            continue
        started = time.monotonic()
        try:
            detail = fn(ctx)
            outcome = "PASS"
        except SkipError as exc:
            outcome, detail = "SKIP", str(exc)
        except CheckError as exc:
            outcome, detail = "FAIL", str(exc)
        except (urllib.error.URLError, OSError) as exc:
            outcome, detail = "FAIL", f"cannot reach the service: {exc}"
        except Exception as exc:  # a broken check must be reported, not crash the run
            outcome, detail = "FAIL", f"{type(exc).__name__}: {exc}"
        results.append(Result(name, outcome, detail, time.monotonic() - started))
        print(f"{outcome:<4}  {name:<60} {detail if outcome != 'PASS' else ''}")
        if outcome == "PASS":
            print("      " + detail)
    return results


def main() -> int:
    env = {
        **read_env(ROOT / ".env"),
        **{k: v for k, v in os.environ.items() if k.startswith("EIOS_")},
    }
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--api", default="http://127.0.0.1:{}".format(env.get("EIOS_API_PORT", "8000"))
    )
    parser.add_argument(
        "--mcp", default="http://127.0.0.1:{}/mcp".format(env.get("EIOS_MCP_PORT", "8082"))
    )
    parser.add_argument(
        "--web", default="http://127.0.0.1:{}".format(env.get("EIOS_WEB_PORT", "8080"))
    )
    parser.add_argument(
        "--workspace-dir",
        default=env.get("EIOS_WORKSPACE_DIR", "./workspace"),
        help="host folder that is mounted at /workspace (default: EIOS_WORKSPACE_DIR)",
    )
    parser.add_argument(
        "--container-prefix",
        default="/workspace",
        help="where the API sees that folder (an absolute host path for a host-run API)",
    )
    parser.add_argument("--wait", type=float, default=90.0, help="seconds to wait for readiness")
    parser.add_argument("--only", help="run only checks whose name contains this text")
    parser.add_argument("--json", help="write the results to this file")
    args = parser.parse_args()

    workspace = resolve_workspace(args.workspace_dir)
    if not workspace.is_dir():
        raise SystemExit(f"the workspace folder {workspace} does not exist")
    ctx = Context(
        api=args.api.rstrip("/"),
        mcp=args.mcp.rstrip("/"),
        web=args.web.rstrip("/"),
        workspace_dir=workspace,
        container_prefix=args.container_prefix,
        admin_token=env.get("EIOS_ADMIN_TOKEN") or None,
    )
    if env.get("EIOS_MCP_TOKEN"):
        ctx.state["mcp_token"] = env["EIOS_MCP_TOKEN"]
    print("Engineering OS smoke test")
    print(f"  API {ctx.api} | MCP {ctx.mcp} | UI {ctx.web}")
    print(f"  workspace {ctx.workspace_dir} -> seen by the API as {ctx.container_prefix}")
    if not wait_for_ready(ctx.api, args.wait):
        print(f"FAIL  the API did not become ready within {args.wait:.0f} s: is the stack up?")
        print("      try:  docker compose ps   and   docker compose logs api")
        return 2
    results = run(ctx, args.only)
    failed = [r for r in results if r.outcome == "FAIL"]
    skipped = [r for r in results if r.outcome == "SKIP"]
    passed = [r for r in results if r.outcome == "PASS"]
    print()
    print(f"{len(passed)} passed, {len(failed)} failed, {len(skipped)} skipped")
    if args.json:
        Path(args.json).write_text(
            json.dumps([r.__dict__ for r in results], indent=1), encoding="utf-8"
        )
    if failed:
        print("NOT READY TO USE: fix the FAIL lines above (docs/operations/local-setup.md).")
        return 1
    if skipped or not passed:
        print("NOT CONFIRMED: some checks did not run, so this says nothing about the rest.")
        return 1
    if args.only:
        print(f"Selected checks passed ('{args.only}'). Run without --only for the full verdict.")
        return 0
    print("READY TO USE.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
