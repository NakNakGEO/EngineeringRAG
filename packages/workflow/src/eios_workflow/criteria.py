"""Deterministic acceptance-criteria evaluation. A model's claim is never enough."""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from eios_domain.knowledge import EvidenceRecord
from eios_domain.workflow import Criterion, CriterionResult

EvidenceLookup = Callable[[list[uuid.UUID]], Awaitable[list[EvidenceRecord]]]
CapabilityLookup = Callable[[str], Awaitable[bool]]


def _empty(value: Any) -> bool:
    return value is None or value == "" or value == [] or value == {}


async def evaluate_criteria(
    criteria: list[Criterion],
    outputs: dict[str, Any],
    evidence_ids: list[uuid.UUID],
    *,
    lookup_evidence: EvidenceLookup,
    capability_completed: CapabilityLookup,
    project_id: uuid.UUID | None,
) -> list[CriterionResult]:
    results: list[CriterionResult] = []
    records: list[EvidenceRecord] | None = None
    for c in criteria:
        if c.kind == "field":
            present = c.field in outputs and (c.allow_empty or not _empty(outputs[c.field]))
            results.append(
                CriterionResult(
                    id=c.id,
                    passed=bool(present),
                    detail="" if present else f"output '{c.field}' is missing or empty",
                )
            )
        elif c.kind == "equals":
            ok = c.field in outputs and outputs[c.field] == c.value
            results.append(
                CriterionResult(
                    id=c.id, passed=ok, detail="" if ok else f"'{c.field}' must equal {c.value!r}"
                )
            )
        elif c.kind == "evidence":
            if records is None:
                records = await lookup_evidence(evidence_ids) if evidence_ids else []
            valid = [
                r
                for r in records
                if (r.project_id is None or project_id is None or r.project_id == project_id)
                and (c.source_tool is None or r.tool_id == c.source_tool)
            ]
            ok = len(valid) >= c.min_count
            results.append(
                CriterionResult(
                    id=c.id,
                    passed=ok,
                    detail=""
                    if ok
                    else f"{len(valid)} valid evidence record(s), {c.min_count} required "
                    "(unknown ids and other projects' evidence do not count)",
                )
            )
        else:  # capability
            assert c.capability is not None  # noqa: S101 - validated by the model
            ok = await capability_completed(c.capability)
            results.append(
                CriterionResult(
                    id=c.id,
                    passed=ok,
                    detail="" if ok else f"no completed '{c.capability}' call recorded on this run",
                )
            )
    return results
