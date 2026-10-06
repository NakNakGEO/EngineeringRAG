"""Knowledge governance (lifecycle history, dependencies, contradictions) and evaluation runs."""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

from eios_storage.metadata import metadata
from eios_storage.tables._common import in_list

KNOWLEDGE = "knowledge"
EVALUATION = "evaluation"
SUBJECT_KINDS = ("item", "decision")
CHANGE_KINDS = ("trust", "health", "supersede", "contradiction", "status", "create")
DEP_KINDS = ("file", "symbol", "item")

lifecycle = sa.Table(
    "lifecycle",
    metadata,
    sa.Column("id", UUID(as_uuid=True), primary_key=True),
    sa.Column("subject_kind", sa.Text, nullable=False),
    sa.Column("subject_id", UUID(as_uuid=True), nullable=False),
    sa.Column("change", sa.Text, nullable=False),
    sa.Column("from_value", sa.Text, nullable=True),
    sa.Column("to_value", sa.Text, nullable=False),
    sa.Column("actor", sa.Text, nullable=False),
    sa.Column("reason", sa.Text, nullable=False, server_default=""),
    sa.Column("evidence_ids", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
    sa.Column("approval", JSONB, nullable=True),
    sa.Column("at", sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint(in_list("subject_kind", SUBJECT_KINDS), name="subject_kind_valid"),
    sa.CheckConstraint(in_list("change", CHANGE_KINDS), name="change_valid"),
    sa.Index("ix_lifecycle_subject", "subject_kind", "subject_id", "at"),
    schema=KNOWLEDGE,
)

dependency = sa.Table(
    "dependency",
    metadata,
    sa.Column("item_id", UUID(as_uuid=True), nullable=False),
    sa.Column("dep_kind", sa.Text, nullable=False),
    sa.Column("dep_key", sa.Text, nullable=False),
    sa.Column("dep_hash", sa.Text, nullable=True),
    sa.Column("project_id", UUID(as_uuid=True), nullable=True),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint("item_id", "dep_kind", "dep_key", name="pk_dependency"),
    sa.ForeignKeyConstraint(
        ["item_id"], ["knowledge.item.id"], ondelete="CASCADE", name="fk_dependency_item"
    ),
    sa.CheckConstraint(in_list("dep_kind", DEP_KINDS), name="dep_kind_valid"),
    sa.Index("ix_dependency_lookup", "project_id", "dep_kind", "dep_key"),
    schema=KNOWLEDGE,
)

contradiction = sa.Table(
    "contradiction",
    metadata,
    sa.Column("id", UUID(as_uuid=True), primary_key=True),
    sa.Column("item_a", UUID(as_uuid=True), nullable=False),
    sa.Column("item_b", UUID(as_uuid=True), nullable=False),
    sa.Column("reason", sa.Text, nullable=False),
    sa.Column("status", sa.Text, nullable=False),
    sa.Column("detected_by", sa.Text, nullable=False),
    sa.Column("resolved_by", sa.Text, nullable=True),
    sa.Column("resolution", sa.Text, nullable=True),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(
        ["item_a"], ["knowledge.item.id"], ondelete="CASCADE", name="fk_contradiction_a"
    ),
    sa.ForeignKeyConstraint(
        ["item_b"], ["knowledge.item.id"], ondelete="CASCADE", name="fk_contradiction_b"
    ),
    sa.CheckConstraint("item_a <> item_b", name="distinct_items"),
    sa.CheckConstraint("status in ('open','resolved')", name="status_valid"),
    sa.Index("ix_contradiction_status", "status"),
    sa.UniqueConstraint("item_a", "item_b", name="uq_contradiction_pair"),
    schema=KNOWLEDGE,
)

evaluation_run = sa.Table(
    "run",
    metadata,
    sa.Column("id", UUID(as_uuid=True), primary_key=True),
    sa.Column("kind", sa.Text, nullable=False),
    sa.Column("subject", sa.Text, nullable=False),
    sa.Column("subject_version", sa.Text, nullable=True),
    sa.Column("capability_id", sa.Text, nullable=True),
    sa.Column("score", sa.Float, nullable=True),
    sa.Column("samples", sa.Integer, nullable=False, server_default="0"),
    sa.Column("passed", sa.Integer, nullable=False, server_default="0"),
    sa.Column("details", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
    sa.Column("evaluator", sa.Text, nullable=False),
    sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("finished_at", sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint("score IS NULL OR (score >= 0 AND score <= 1)", name="score_range"),
    sa.CheckConstraint("passed >= 0 AND passed <= samples", name="passed_range"),
    sa.Index("ix_eval_subject", "subject", "started_at"),
    schema=EVALUATION,
)
