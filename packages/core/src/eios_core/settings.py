"""Typed runtime configuration, read from ``EIOS_*`` environment variables."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from eios_core.database_policy import assert_internal_database_url


class Settings(BaseSettings):
    """All configuration for Engineering OS processes.

    There is intentionally a single database setting: the dedicated Engineering OS PostgreSQL.
    No setting exists for any other database, and any value is validated by
    :func:`eios_core.database_policy.assert_internal_database_url`.
    """

    model_config = SettingsConfigDict(
        env_prefix="EIOS_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
    )

    environment: Literal["development", "test", "production"] = "development"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    log_json: bool = True

    database_url: SecretStr = Field(
        description="Internal Engineering OS PostgreSQL URL (postgresql+psycopg://...)."
    )
    database_connect_timeout_seconds: float = Field(default=3.0, gt=0, le=60)

    blob_dir: Path = Path("data/blobs")
    max_evidence_bytes: int = Field(default=50_000_000, ge=1)
    ephemeral_ttl_seconds: int = Field(default=7 * 24 * 3600, ge=60)
    embedding_model: str = "hashing-v1"

    workspace_roots: str = Field(
        default="",
        description="Approved workspace roots (comma- or os.pathsep-separated). Empty = none.",
    )
    max_indexed_file_bytes: int = Field(default=1_000_000, ge=1_000)
    overlay_ttl_seconds: int = Field(default=24 * 3600, ge=60)

    manifests_dir: Path = Path("manifests")
    workflows_dir: Path = Path("workflows")
    plugins_dir: Path | None = Field(
        default=None, description="Optional directory of owner-supplied plugin manifests."
    )

    root_policy_path: Path = Path("policy/root_policy.yaml")
    admin_token: SecretStr | None = Field(
        default=None,
        description="Shared secret for the approval API. Unset = approvals cannot be decided.",
    )
    approval_ttl_seconds: int = Field(default=24 * 3600, ge=60)
    sandbox_require_network_isolation: bool = True
    sandbox_output_dir: Path = Path("data/sandbox")
    tool_call_timeout_seconds: float = Field(default=120.0, gt=0, le=3600)

    api_host: str = "127.0.0.1"
    api_port: int = Field(default=8000, ge=1, le=65535)

    worker_health_host: str = "127.0.0.1"
    worker_health_port: int = Field(default=8081, ge=1, le=65535)
    worker_heartbeat_seconds: float = Field(default=5.0, gt=0, le=3600)

    sse_poll_interval_seconds: float = Field(default=0.25, gt=0, le=10)
    sse_keepalive_seconds: float = Field(default=15.0, gt=0, le=300)
    sse_max_seconds: float = Field(default=3600.0, gt=0)

    mcp_host: str = "127.0.0.1"
    mcp_port: int = Field(default=8082, ge=1, le=65535)

    @property
    def workspace_root_paths(self) -> list[Path]:
        raw = self.workspace_roots.replace(os.pathsep, ",")
        return [Path(p.strip()) for p in raw.split(",") if p.strip()]

    @field_validator("admin_token", mode="before")
    @classmethod
    def _blank_admin_token_means_unset(cls, value: object) -> object:
        if value is None or (isinstance(value, str) and not value.strip()):
            return None
        if isinstance(value, str) and len(value) < 12:
            raise ValueError("EIOS_ADMIN_TOKEN must be at least 12 characters")
        return value

    @field_validator("database_url")
    @classmethod
    def _database_must_be_internal(cls, value: SecretStr) -> SecretStr:
        assert_internal_database_url(value.get_secret_value())
        return value


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings. Tests should construct ``Settings`` explicitly instead."""
    return Settings()  # type: ignore[call-arg]  # database_url comes from the environment
