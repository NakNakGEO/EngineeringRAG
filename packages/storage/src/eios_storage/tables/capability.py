"""Schema ``capability``: catalog, providers (tools/adapters/subsystems), metrics, state history."""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

from eios_storage.metadata import metadata
from eios_storage.tables._common import in_list

SCHEMA = "capability"
REGISTRY_STATES = (
    "UNREGISTERED", "EXPERIMENTAL", "VERIFIED", "TRUSTED", "DISABLED", "BROKEN", "QUARANTINED",
)  # fmt: skip
ORIGINS = ("builtin", "plugin", "generated", "downloaded")

definition = sa.Table(
    "definition",
    metadata,
    sa.Column("id", sa.Text, primary_key=True),
    sa.Column("description", sa.Text, nullable=False),
    sa.Column("risk", sa.Text, nullable=False),
    sa.Column("inputs", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
    sa.Column("outputs", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
    sa.Column("allowed_scopes", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
    sa.Column("tags", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
    sa.Column("manifest", JSONB, nullable=False),
    sa.Column("manifest_hash", sa.Text, nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint("risk in ('low','medium','high','critical')", name="risk_valid"),
    sa.CheckConstraint("id !~ '^external_database'", name="not_external_database"),
    schema=SCHEMA,
)

provider = sa.Table(
    "provider",
    metadata,
    sa.Column("id", sa.Text, nullable=False),
    sa.Column("version", sa.Text, nullable=False),
    sa.Column("type", sa.Text, nullable=False),
    sa.Column("origin", sa.Text, nullable=False),
    sa.Column("state", sa.Text, nullable=False),
    sa.Column("maturity", sa.Text, nullable=False),
    sa.Column("trust", sa.Text, nullable=False, server_default="unrated"),
    sa.Column("verification_state", sa.Text, nullable=False, server_default="unverified"),
    sa.Column("priority", sa.Integer, nullable=False, server_default="50"),
    sa.Column("manifest", JSONB, nullable=False),
    sa.Column("manifest_hash", sa.Text, nullable=False),
    sa.Column("approved_by", sa.Text, nullable=True),
    sa.Column("approval_id", UUID(as_uuid=True), nullable=True),
    sa.Column("health_status", sa.Text, nullable=False, server_default="unknown"),
    sa.Column("health_checked_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("health_detail", sa.Text, nullable=False, server_default=""),
    sa.Column("state_reason", sa.Text, nullable=False, server_default=""),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint("id", "version", name="pk_provider"),
    sa.CheckConstraint(in_list("state", REGISTRY_STATES), name="state_valid"),
    sa.CheckConstraint(in_list("origin", ORIGINS), name="origin_valid"),
    sa.CheckConstraint("health_status in ('unknown','ok','fail')", name="health_valid"),
    sa.Index("ix_provider_state", "state"),
    schema=SCHEMA,
)

provider_capability = sa.Table(
    "provider_capability",
    metadata,
    sa.Column("provider_id", sa.Text, nullable=False),
    sa.Column("provider_version", sa.Text, nullable=False),
    sa.Column("capability_id", sa.Text, nullable=False),
    sa.PrimaryKeyConstraint(
        "provider_id", "provider_version", "capability_id", name="pk_provider_capability"
    ),
    sa.ForeignKeyConstraint(
        ["provider_id", "provider_version"],
        ["capability.provider.id", "capability.provider.version"],
        ondelete="CASCADE",
        name="fk_provider_capability_provider",
    ),
    sa.CheckConstraint("capability_id !~ '^external_database'", name="not_external_database"),
    sa.Index("ix_provider_capability_capability", "capability_id"),
    schema=SCHEMA,
)

provider_metric = sa.Table(
    "provider_metric",
    metadata,
    sa.Column("provider_id", sa.Text, nullable=False),
    sa.Column("provider_version", sa.Text, nullable=False),
    sa.Column("capability_id", sa.Text, nullable=False),
    sa.Column("successes", sa.Integer, nullable=False, server_default="0"),
    sa.Column("failures", sa.Integer, nullable=False, server_default="0"),
    sa.Column("total_latency_ms", sa.BigInteger, nullable=False, server_default="0"),
    sa.Column("tokens", sa.BigInteger, nullable=False, server_default="0"),
    sa.Column("eval_score", sa.Float, nullable=True),
    sa.Column("eval_samples", sa.Integer, nullable=False, server_default="0"),
    sa.Column("known_weaknesses", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
    sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("last_verified_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint(
        "provider_id", "provider_version", "capability_id", name="pk_provider_metric"
    ),
    sa.ForeignKeyConstraint(
        ["provider_id", "provider_version"],
        ["capability.provider.id", "capability.provider.version"],
        ondelete="CASCADE",
        name="fk_provider_metric_provider",
    ),
    sa.CheckConstraint("successes >= 0 AND failures >= 0", name="counts_nonnegative"),
    schema=SCHEMA,
)

state_history = sa.Table(
    "state_history",
    metadata,
    sa.Column("id", UUID(as_uuid=True), primary_key=True),
    sa.Column("kind", sa.Text, nullable=False),
    sa.Column("item_id", sa.Text, nullable=False),
    sa.Column("item_version", sa.Text, nullable=False),
    sa.Column("from_state", sa.Text, nullable=False),
    sa.Column("to_state", sa.Text, nullable=False),
    sa.Column("actor", sa.Text, nullable=False),
    sa.Column("reason", sa.Text, nullable=False, server_default=""),
    sa.Column("approval_id", UUID(as_uuid=True), nullable=True),
    sa.Column("at", sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint("kind in ('provider','agent','skill')", name="kind_valid"),
    sa.Index("ix_state_history_item", "kind", "item_id", "item_version"),
    schema=SCHEMA,
)
