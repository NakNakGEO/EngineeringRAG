"""Measure retrieval against independent ground truth on a live Engineering OS instance.

The questions in tests/evals/dogfood/questions.json were written by engineers who read this
repository directly (not by asking the system) together with the files a competent engineer would
need. This script indexes nothing: point it at a running API whose project is this repository.

    python scripts/dogfood_eval.py --project <uuid> [--url http://127.0.0.1:8000] [--no-hints]
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
import urllib.request
from collections import defaultdict
from pathlib import Path
from typing import Any

QUESTIONS = Path(__file__).resolve().parents[1] / "tests" / "evals" / "dogfood" / "questions.json"


def post(url: str, body: dict[str, Any]) -> dict[str, Any]:
    req = urllib.request.Request(  # noqa: S310 - operator-supplied http(s) URL
        url, data=json.dumps(body).encode(), headers={"content-type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=120) as resp:  # noqa: S310 - operator-supplied URL
        data: dict[str, Any] = json.loads(resp.read())
        return data


def paths_of(items: list[dict[str, Any]]) -> set[str]:
    return {
        str(i["candidate"]["path"])
        for i in items
        if i.get("candidate") and i["candidate"].get("path")
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", required=True)
    ap.add_argument("--url", default="http://127.0.0.1:8000")
    ap.add_argument("--no-hints", action="store_true", help="do not pass symbol hints")
    ap.add_argument("--json", help="write per-question results here")
    args = ap.parse_args()

    questions: list[dict[str, Any]] = json.loads(QUESTIONS.read_text())
    rows: list[dict[str, Any]] = []
    for q in questions:
        symbols = [] if args.no_hints else list(q.get("symbols") or [])
        t0 = time.monotonic()
        body = post(
            f"{args.url}/context/build",
            {
                "text": q["text"],
                "project_id": args.project,
                "symbols": symbols,
                "risk": "medium",
                "token_budget": 12000,
                "record_run": False,
            },
        )
        latency = (time.monotonic() - t0) * 1000
        pack = body["pack"]
        found = paths_of(pack["items"])
        search = post(
            f"{args.url}/retrieval/search",
            {"text": q["text"], "project_id": args.project, "limit": 10},
        )
        top10 = paths_of(search["items"])
        expected = set(q["expected_paths"])
        rows.append(
            {
                "kind": q["kind"],
                "text": q["text"],
                "expected": sorted(expected),
                "pack_found": sorted(expected & found),
                "pack_missing": sorted(expected - found),
                "search10_found": sorted(expected & top10),
                "ready": bool(pack["gap"]["ready_to_act"]),
                "level": pack["level"],
                "levels_tried": len(pack.get("levels_tried") or []),
                "coverage": pack["gap"]["coverage"],
                "confidence": pack["gap"]["confidence"],
                "report": pack.get("coverage"),
                "missing": sorted({m["kind"] for m in pack["gap"]["missing_context"]}),
                "multi_source": sum(1 for i in pack["items"] if len(i["sources"]) >= 2),
                "items": len(pack["items"]),
                "paths": len(found),
                "latency_ms": round(latency),
            }
        )

    def recall(sel: list[dict[str, Any]], key: str) -> float:
        total = sum(len(r["expected"]) for r in sel)
        return sum(len(r[key]) for r in sel) / max(total, 1)

    print(f"questions={len(rows)} hints={'off' if args.no_hints else 'on'}")
    print(f"pack recall      {recall(rows, 'pack_found'):.3f}")
    print(f"search@10 recall {recall(rows, 'search10_found'):.3f}")
    by_kind: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        by_kind[str(r["kind"])].append(r)
    for kind, sel in sorted(by_kind.items()):
        print(
            f"  {kind:7s} n={len(sel):2d} pack={recall(sel, 'pack_found'):.2f} "
            f"search10={recall(sel, 'search10_found'):.2f}"
        )
    liars = [r for r in rows if r["ready"] and r["pack_missing"]]
    print(f"ready_to_act but files missing: {len(liars)}/{len(rows)}")
    print(f"median latency {statistics.median(r['latency_ms'] for r in rows):.0f} ms")
    if args.json:
        Path(args.json).write_text(json.dumps(rows, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
