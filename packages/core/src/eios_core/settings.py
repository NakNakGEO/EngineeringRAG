"""Typed runtime configuration, read from ``EIOS_*`` environment variables."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from eios_core.database_policy import assert_internal_database_url

# Impact Analyzer path lists. Globs (fnmatch, case-insensitive) are tried against ``/<path>`` so a
# leading ``*/`` also matches a top-level directory. Owners override them per deployment with
# ``EIOS_IMPACT_*`` (JSON list); the analyzer itself carries no hard-coded paths.
DEFAULT_IMPACT_SECURITY_CRITICAL_PATHS: tuple[str, ...] = (
    "*/policy/*", "*/eios_policy/*", "*/security/*", "*/auth/*", "*/authentication/*",
    "*/authorization/*", "*/identity/*", "*/sandbox*", "*/secrets*", "*/crypto*",
    "*/routes/admin*", "*/routes/policy*", "*/routes/governance*", "*audit*",
)  # fmt: skip
DEFAULT_IMPACT_SCHEMA_PATHS: tuple[str, ...] = (
    "*/migrations/*", "*/alembic/*", "*/storage/tables/*", "*/eios_storage/tables/*",
    "*/db/schema*", "*.sql", "*.ddl",
)  # fmt: skip
DEFAULT_IMPACT_SCHEMA_TEST_MARKERS: tuple[str, ...] = ("migration", "schema", "alembic")
DEFAULT_IMPACT_ROUTE_PATHS: tuple[str, ...] = (
    "*/routes/*", "*/controllers/*", "*controller.cs", "*/endpoints/*",
)  # fmt: skip
DEFAULT_IMPACT_CONTRACT_PATHS: tuple[str, ...] = (
    "*/manifests/*", "*/events.py", "*/eventtypes.*", "*.proto", "*/openapi*", "*/contracts/*",
)  # fmt: skip


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

    impact_security_critical_paths: list[str] = Field(
        default_factory=lambda: list(DEFAULT_IMPACT_SECURITY_CRITICAL_PATHS),
        description="Globs of security-critical code: a change there is never rated below HIGH.",
    )
    impact_schema_paths: list[str] = Field(
        default_factory=lambda: list(DEFAULT_IMPACT_SCHEMA_PATHS),
        description="Globs of schema/migration files: counted as database changes.",
    )
    impact_schema_test_markers: list[str] = Field(
        default_factory=lambda: list(DEFAULT_IMPACT_SCHEMA_TEST_MARKERS),
        description="Words in a test file name that mark it as covering schema and migrations.",
    )
    impact_route_paths: list[str] = Field(
        default_factory=lambda: list(DEFAULT_IMPACT_ROUTE_PATHS),
        description="Globs of HTTP route modules (tested over HTTP rather than by import).",
    )
    impact_contract_paths: list[str] = Field(
        default_factory=lambda: list(DEFAULT_IMPACT_CONTRACT_PATHS),
        description="Globs of public contract surfaces besides routes (events, manifests...).",
    )

    manifests_dir: Path = Path("manifests")
    workflows_dir: Path = Path("workflows")
    evals_dir: Path = Path("evals")
    plugins_dir: Path | None = Field(
        default=None, description="Optional directory of owner-supplied plugin manifests."
    )

    # LLM gateway providers: each exists only if configured (nothing is guessed or defaulted).
    llm_local_base_url: str | None = None
    llm_local_model: str | None = None
    # Hosts agents may read web pages from (comma separated, "*.example.com" allowed). Empty = off.
    web_allowed_hosts: str = ""
    # the first call after a local server was idle loads the model (a minute or more)
    llm_local_timeout_seconds: float = Field(default=240.0, gt=0, le=1800)
    llm_openai_base_url: str = "https://api.openai.com/v1"
    llm_openai_api_key: SecretStr | None = None
    llm_openai_model: str | None = None
    llm_anthropic_api_key: SecretStr | None = None
    llm_anthropic_model: str | None = None
    llm_default_provider: str | None = None
    api_allowed_hosts: list[str] = Field(
        default_factory=lambda: ["127.0.0.1", "localhost", "api"],
        description="Host names the API answers to; the port is ignored. Requests naming any "
        "other host are refused (DNS-rebinding protection: a web page in your browser can make "
        "an attacker-owned name resolve to 127.0.0.1, but cannot make it send these names).",
    )
    mcp_token: SecretStr | None = Field(
        default=None, description="Optional bearer token required by the MCP server."
    )

    root_policy_path: Path = Path("policy/root_policy.yaml")
    admin_token: SecretStr | None = Field(
        default=None,
        description="Shared secret for the approval API. Unset = approvals cannot be decided.",
    )
    approval_ttl_seconds: int = Field(default=24 * 3600, ge=60)
    sandbox_require_network_isolation: bool = True
    sandbox_output_dir: Path = Path("data/sandbox")
    workshop_dir: Path = Path("data/workshop")
    tool_call_timeout_seconds: float = Field(default=120.0, gt=0, le=3600)

    retention_event_days: int | None = Field(default=90, ge=1)
    retention_audit_days: int | None = Field(default=None, ge=1, description="None = keep forever")
    retention_job_days: int | None = Field(default=14, ge=1)

    api_host: str = "127.0.0.1"
    api_port: int = Field(default=8000, ge=1, le=65535)

    worker_health_host: str = "127.0.0.1"
    worker_health_port: int = Field(default=8081, ge=1, le=65535)
    worker_heartbeat_seconds: float = Field(default=5.0, gt=0, le=3600)

    # Live index: how often the worker looks for changed files (0 turns the watcher off), and the
    # shortest gap between two automatic syncs of one project.
    project_watch_interval_seconds: float = Field(default=5.0, ge=0, le=3600)
    project_watch_min_gap_seconds: float = Field(default=20.0, ge=0, le=3600)

    sse_poll_interval_seconds: float = Field(default=0.25, gt=0, le=10)
    sse_keepalive_seconds: float = Field(default=15.0, gt=0, le=300)
    sse_max_seconds: float = Field(default=3600.0, gt=0)

    mcp_host: str = "127.0.0.1"
    mcp_port: int = Field(default=8082, ge=1, le=65535)

    @property
    def workspace_root_paths(self) -> list[Path]:
        raw = self.workspace_roots.replace(os.pathsep, ",")
        return [Path(p.strip()) for p in raw.split(",") if p.strip()]

    @field_validator(
        "admin_token", "mcp_token", "llm_openai_api_key", "llm_anthropic_api_key",
        "llm_local_base_url", "llm_local_model", "llm_openai_model", "llm_anthropic_model",
        "llm_default_provider", mode="before",
    )  # fmt: skip
    @classmethod
    def _blank_admin_token_means_unset(cls, value: object) -> object:
        if value is None or (isinstance(value, str) and not value.strip()):
            return None
        return value

    @field_validator("admin_token", "mcp_token", mode="after")
    @classmethod
    def _tokens_must_be_long(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None and len(value.get_secret_value()) < 12:
            raise ValueError("access tokens must be at least 12 characters")
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
