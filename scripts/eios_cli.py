"""Small command-line client for a running Engineering OS (no curl or shell quoting needed).

Works the same in PowerShell, cmd, bash and zsh. Standard library only.

    python scripts/eios_cli.py status
    python scripts/eios_cli.py add my-service            # a folder inside the workspace
    python scripts/eios_cli.py projects
    python scripts/eios_cli.py context "What does OrderService.place_order do?" -p my-service
    python scripts/eios_cli.py context "Add a retry" -p my-service -s OrderService --risk high
    python scripts/eios_cli.py impact -p my-service --path src/orders.py
    python scripts/eios_cli.py impact -p my-service --symbol OrderService.place_order

Add ``--json`` to any command for the raw API response. The API address comes from ``.env``
(``EIOS_API_PORT``) or ``--api``.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from smoke_stack import ROOT, http, read_env

WORKSPACE_MOUNT = "/workspace"


def to_container_path(value: str) -> str:
    """``my-service`` or ``team/my-service`` -> ``/workspace/...``; absolute paths pass through."""
    cleaned = value.replace("\\", "/").strip()
    if cleaned.startswith("/"):
        return cleaned
    return f"{WORKSPACE_MOUNT}/{cleaned.strip('/')}"


def pick_project(projects: list[dict[str, Any]], wanted: str) -> dict[str, Any]:
    """A project by id prefix, name or unique name prefix (case-insensitive)."""
    lowered = wanted.lower()
    matches = [p for p in projects if str(p["id"]).lower().startswith(lowered)]
    if not matches:
        matches = [p for p in projects if str(p.get("name", "")).lower() == lowered]
    if not matches:
        matches = [p for p in projects if str(p.get("name", "")).lower().startswith(lowered)]
    if not matches:
        known = ", ".join(f"{p.get('name')} ({str(p['id'])[:8]})" for p in projects) or "none"
        raise SystemExit(f"no project matches '{wanted}'. Known projects: {known}")
    if len(matches) > 1:
        raise SystemExit(
            f"'{wanted}' matches {len(matches)} projects; use a longer id: "
            + ", ".join(str(p["id"])[:12] for p in matches)
        )
    return matches[0]


def call(api: str, method: str, path: str, body: Any = None, timeout: float = 300.0) -> Any:
    try:
        status, _, data = http(method, api + path, body, timeout=timeout)
    except OSError as exc:
        raise SystemExit(f"cannot reach {api} ({exc}). Is the stack up? docker compose ps") from exc
    if status >= 400:
        detail = data.get("detail") if isinstance(data, dict) else data
        raise SystemExit(f"{method} {path} failed ({status}): {detail}")
    return data


def project_id(api: str, wanted: str) -> str:
    return str(pick_project(call(api, "GET", "/projects"), wanted)["id"])


def show(data: Any, as_json: bool, render: Any) -> None:
    if as_json:
        print(json.dumps(data, indent=2))
    else:
        render(data)


def cmd_status(args: argparse.Namespace) -> None:
    ready = call(args.api, "GET", "/health/ready")
    projects = call(args.api, "GET", "/projects")

    def render(_: Any) -> None:
        print(f"API {args.api}: {ready['status']}")
        for check in ready.get("checks", []):
            print(f"  {check['name']}: {check['status']}")
        print(f"{len(projects)} project(s)")

    show({"health": ready, "projects": len(projects)}, args.json, render)


def cmd_projects(args: argparse.Namespace) -> None:
    projects = call(args.api, "GET", "/projects")

    def render(rows: list[dict[str, Any]]) -> None:
        for p in rows:
            print(
                f"{str(p['id'])[:8]}  {p.get('name'):<28} {p.get('bootstrap_state'):<10} "
                f"branch {p.get('last_branch')}  synced {p.get('last_synced_at')}"
            )
        if not rows:
            print("no projects yet: python scripts/eios_cli.py add <folder-in-workspace>")

    show(projects, args.json, render)


def cmd_add(args: argparse.Namespace) -> None:
    path = to_container_path(args.path)
    result = call(
        args.api,
        "POST",
        "/projects/bootstrap",
        {"path": path, "sync": "always", "wait": True},
    )

    def render(data: dict[str, Any]) -> None:
        project, sync = data["project"], data.get("sync") or {}
        print(f"{project['name']} ({str(project['id'])[:8]}) indexed from {path}")
        print(
            f"  branch {sync.get('branch')}, {sync.get('files_total')} files, "
            f"{sync.get('symbols_total')} symbols in {sync.get('duration_ms')} ms, "
            f"state {sync.get('state')}"
        )
        print(f'  next: python scripts/eios_cli.py context "your question" -p {project["name"]}')

    show(result, args.json, render)


def cmd_context(args: argparse.Namespace) -> None:
    body: dict[str, Any] = {
        "text": args.text,
        "project_id": project_id(args.api, args.project),
        "symbols": args.symbol or [],
        "paths": args.path or [],
        "risk": args.risk,
        "token_budget": args.budget,
    }
    data = call(args.api, "POST", "/context/build", body)

    def render(result: dict[str, Any]) -> None:
        pack = result["pack"]
        gap = pack["gap"]
        verdict = "READY TO ACT" if gap["ready_to_act"] else "NOT READY"
        print(
            f"{verdict} at {pack['level']} (levels tried: "
            f"{', '.join(pack.get('levels_tried', []))}); coverage {gap['coverage']:.2f}, "
            f"confidence {gap['confidence']:.2f}; "
            f"{pack['used_tokens']}/{pack['budget_tokens']} tokens"
        )
        for missing in gap["missing_context"]:
            flag = "BLOCKING" if missing.get("blocking") else "note    "
            print(f"  [{flag}] {missing['kind']}: {missing['description']}")
        for action in pack.get("required_expansions", []):
            print(f"  next: {action['action']} - {action['reason']}")
        print("context items (best first):")
        for item in pack["items"][: args.top]:
            c = item["candidate"]
            marker = "*" if item.get("critical") else " "
            line = f":{c['start_line']}" if c.get("start_line") else ""
            where = f"{c['path']}{line}" if c.get("path") else c["id"]
            print(f" {marker} {c['title'][:70]:<70} {where}")
        print("(* = protected evidence; use --json for the full pack with source text)")

    show(data, args.json, render)


def cmd_impact(args: argparse.Namespace) -> None:
    pid = project_id(args.api, args.project)
    query = "&".join(
        [f"path={p}" for p in args.path or []] + [f"symbol={s}" for s in args.symbol or []]
    )
    if not query:
        raise SystemExit("give at least one --path or --symbol")
    data = call(args.api, "GET", f"/projects/{pid}/impact?{query}")

    def render(report: dict[str, Any]) -> None:
        print(
            f"risk {report['risk'].upper()} (confidence {report['confidence']}); "
            f"{report['blast_radius']} dependent file(s); tests: "
            f"{', '.join(report['tests']) or 'NONE FOUND'}"
        )
        for factor in report["risk_factors"]:
            print(f"  - {factor}")
        for warning in report.get("warnings", []):
            print(f"  ! {warning}")
        for path in report["consumers"][: args.top]:
            print(f"  used by {path}")

    show(data, args.json, render)


def main() -> int:
    env = read_env(ROOT / ".env")
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--api", default=f"http://127.0.0.1:{env.get('EIOS_API_PORT', '8000')}")
    parser.add_argument("--json", action="store_true", help="print the raw API response")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status", help="health and project count").set_defaults(fn=cmd_status)
    sub.add_parser("projects", help="list indexed projects").set_defaults(fn=cmd_projects)
    add = sub.add_parser("add", help="index a folder from the workspace")
    add.add_argument("path", help="folder name under the workspace, or an absolute /workspace path")
    add.set_defaults(fn=cmd_add)
    ctx = sub.add_parser("context", help="build a context pack for a question")
    ctx.add_argument("text")
    ctx.add_argument("-p", "--project", required=True, help="project name or id (prefix)")
    ctx.add_argument("-s", "--symbol", action="append", help="a name you mean (repeatable)")
    ctx.add_argument("--path", action="append", help="a file you mean (repeatable)")
    ctx.add_argument("--risk", default="medium", choices=["low", "medium", "high", "critical"])
    ctx.add_argument("--budget", type=int, default=12000, help="token budget")
    ctx.add_argument("--top", type=int, default=15, help="items to print")
    ctx.set_defaults(fn=cmd_context)
    imp = sub.add_parser("impact", help="what a change could affect")
    imp.add_argument("-p", "--project", required=True)
    imp.add_argument("--path", action="append")
    imp.add_argument("-s", "--symbol", action="append")
    imp.add_argument("--top", type=int, default=15)
    imp.set_defaults(fn=cmd_impact)
    args = parser.parse_args()
    args.fn(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
