"""Health report models shared by every Engineering OS process (API, worker, MCP)."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Literal

from pydantic import BaseModel, Field

from eios_core import __version__

HealthStatus = Literal["ok", "fail"]


class ComponentHealth(BaseModel):
    """Result of one dependency check. ``detail`` must never contain secrets or DSNs."""

    name: str
    status: HealthStatus
    detail: str | None = None
    latency_ms: float | None = Field(default=None, ge=0)


class HealthReport(BaseModel):
    service: str
    version: str = __version__
    status: HealthStatus
    checks: list[ComponentHealth] = Field(default_factory=list)


def build_report(service: str, checks: Sequence[ComponentHealth] = ()) -> HealthReport:
    """Aggregate checks: the report is ``ok`` only if every check is ``ok``."""
    status: HealthStatus = "ok" if all(c.status == "ok" for c in checks) else "fail"
    return HealthReport(service=service, status=status, checks=list(checks))
