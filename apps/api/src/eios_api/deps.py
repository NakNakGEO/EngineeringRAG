"""FastAPI dependencies."""

from __future__ import annotations

import hmac
from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request, status

from eios_runtime import Container


def get_container(request: Request) -> Container:
    container: Container = request.app.state.container
    return container


ContainerDep = Annotated[Container, Depends(get_container)]
AdminTokenHeader = Annotated[str | None, Header(alias="X-EIOS-Admin-Token")]


def check_admin_token(
    container: Container,
    token: str | None,
    *,
    disabled: str,
    invalid: str = "invalid admin token",
) -> None:
    """The single admin-token gate: 403 when no token is configured (the feature is off),
    401 when the supplied one does not match. Constant-time comparison."""
    configured = container.settings.admin_token
    if configured is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, disabled)
    if token is None or not hmac.compare_digest(
        token.encode(), configured.get_secret_value().encode()
    ):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, invalid)
