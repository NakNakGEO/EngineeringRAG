"""Liveness and readiness endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Request, Response, status

from eios_core.health import HealthReport, build_report

router = APIRouter(prefix="/health", tags=["health"])

SERVICE_NAME = "engineering-api"


@router.get("/live", response_model=HealthReport)
async def live() -> HealthReport:
    """The process is up. Never touches dependencies."""
    return build_report(SERVICE_NAME)


@router.get("/ready", response_model=HealthReport)
async def ready(request: Request, response: Response) -> HealthReport:
    """The process can serve traffic: all dependencies (the Engineering OS database) are healthy."""
    checks = [await check() for check in request.app.state.readiness_checks]
    report = build_report(SERVICE_NAME, checks)
    if report.status != "ok":
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return report
