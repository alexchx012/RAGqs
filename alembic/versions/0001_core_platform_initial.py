"""Create core platform shared primitives.

Revision ID: 0001_core_platform_initial
Revises:
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0001_core_platform_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "platform_audit",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("actor_id", sa.String(length=128), nullable=False),
        sa.Column("resource_type", sa.String(length=128), nullable=False),
        sa.Column("resource_id", sa.String(length=256), nullable=False),
        sa.Column("request_id", sa.String(length=128), nullable=False),
        sa.Column("occurred_at_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column("result", sa.String(length=64), nullable=False),
        sa.Column("details_json", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_platform_audit_request_id", "platform_audit", ["request_id"])
    op.create_table(
        "platform_idempotency",
        sa.Column("scope", sa.String(length=128), nullable=False),
        sa.Column("idempotency_key", sa.String(length=256), nullable=False),
        sa.Column("request_hash", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("response_json", sa.JSON(), nullable=True),
        sa.Column("created_at_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at_utc", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("scope", "idempotency_key"),
    )
    op.create_table(
        "platform_lease",
        sa.Column("resource", sa.String(length=256), nullable=False),
        sa.Column("owner", sa.String(length=128), nullable=False),
        sa.Column("fence_token", sa.BigInteger(), nullable=False),
        sa.Column("expires_at_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at_utc", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("resource"),
    )
    op.create_table(
        "platform_observability_sample",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("observed_at_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column("route_template", sa.String(length=256), nullable=False),
        sa.Column("method", sa.String(length=16), nullable=False),
        sa.Column("outcome_class", sa.String(length=64), nullable=False),
        sa.Column("status_family", sa.String(length=16), nullable=False),
        sa.Column("latency_ms", sa.Integer(), nullable=False),
        sa.Column("sample_weight", sa.Float(), nullable=False),
        sa.Column("retention_days", sa.Integer(), nullable=False),
        sa.Column("expires_at_utc", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_platform_observability_sample_observed_at",
        "platform_observability_sample",
        ["observed_at_utc"],
    )
    op.create_index(
        "ix_platform_observability_sample_expires_at",
        "platform_observability_sample",
        ["expires_at_utc"],
    )
    op.create_table(
        "platform_observability_aggregate",
        sa.Column("bucket_start_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column("route_template", sa.String(length=256), nullable=False),
        sa.Column("method", sa.String(length=16), nullable=False),
        sa.Column("outcome_class", sa.String(length=64), nullable=False),
        sa.Column("status_family", sa.String(length=16), nullable=False),
        sa.Column("latency_bucket_ms", sa.Integer(), nullable=False),
        sa.Column("retention_days", sa.Integer(), nullable=False),
        sa.Column("expires_at_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sample_weight", sa.Float(), nullable=False),
        sa.Column("sample_count", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint(
            "bucket_start_utc",
            "route_template",
            "method",
            "outcome_class",
            "status_family",
            "latency_bucket_ms",
            "retention_days",
        ),
    )
    op.create_index(
        "ix_platform_observability_aggregate_expires_at",
        "platform_observability_aggregate",
        ["expires_at_utc"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_platform_observability_aggregate_expires_at",
        "platform_observability_aggregate",
    )
    op.drop_table("platform_observability_aggregate")
    op.drop_index(
        "ix_platform_observability_sample_expires_at",
        "platform_observability_sample",
    )
    op.drop_index("ix_platform_observability_sample_observed_at", "platform_observability_sample")
    op.drop_table("platform_observability_sample")
    op.drop_table("platform_lease")
    op.drop_table("platform_idempotency")
    op.drop_index("ix_platform_audit_request_id", "platform_audit")
    op.drop_table("platform_audit")
