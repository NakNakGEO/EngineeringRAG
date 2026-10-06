"""Operator endpoints: portable export/import, audit export, retention.

All require the admin token. They are *not* exposed through MCP and no model can call them.
The portable package contains the Default Vault only - never Project Vault data.
"""

from __future__ import annotations

import base64
import hmac
from typing import Annotated, Any

from fastapi import APIRouter, Header, HTTPException, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from eios_api.deps import ContainerDep
from eios_policy.audit import export_ndjson
from eios_portability import (
    ImportRejectedError,
    PackageError,
    export_default_vault,
    import_package,
    read_header,
)

router = APIRouter(prefix="/admin", tags=["admin"])
AdminToken = Annotated[str | None, Header(alias="X-EIOS-Admin-Token")]


def _require_admin(container: ContainerDep, token: str | None) -> None:
    configured = container.settings.admin_token
    if configured is None:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "admin endpoints are disabled (no admin token)"
        )
    if token is None or not hmac.compare_digest(
        token.encode(), configured.get_secret_value().encode()
    ):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid admin token")


class ExportBody(BaseModel):
    passphrase: str = Field(min_length=12, max_length=500)


class ImportBody(BaseModel):
    package_base64: str = Field(max_length=400_000_000)
    passphrase: str = Field(min_length=1, max_length=500)
    dry_run: bool = False


@router.post("/export")
async def export_vault(
    body: ExportBody, container: ContainerDep, token: AdminToken = None
) -> dict[str, Any]:
    """Encrypted Default Vault package (base64); project/company data is excluded."""
    _require_admin(container, token)
    try:
        data = await export_default_vault(
            container.engine,
            body.passphrase,
            blobs=container.blobs,
            registry=container.registry.store,
        )
    except PackageError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    header = read_header(data)
    return {
        "package_base64": base64.b64encode(data).decode(),
        "counts": header["counts"],
        "bytes": len(data),
    }


@router.post("/import")
async def import_vault(
    body: ImportBody, container: ContainerDep, token: AdminToken = None
) -> dict[str, Any]:
    _require_admin(container, token)
    try:
        data = base64.b64decode(body.package_base64, validate=True)
        report = await import_package(
            data,
            body.passphrase,
            container.engine,
            blobs=container.blobs,
            registry=container.registry,
            dry_run=body.dry_run,
        )
    except ImportRejectedError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    except (PackageError, ValueError) as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    return report.__dict__


@router.get("/audit/export")
async def export_audit(container: ContainerDep, token: AdminToken = None) -> StreamingResponse:
    """Hash-chained NDJSON of the whole audit log (verify with eios_policy.audit.verify_ndjson)."""
    _require_admin(container, token)
    return StreamingResponse(export_ndjson(container.engine), media_type="application/x-ndjson")


@router.post("/retention")
async def run_retention(container: ContainerDep, token: AdminToken = None) -> dict[str, int]:
    _require_admin(container, token)
    return dict((await container.retention.run()).deleted)
