"""Widen quota projection counters to bigint.

Revision ID: 0019_usage_projection_bigint
Revises: 0018_retention_ops
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "0019_usage_projection_bigint"
down_revision: str | None = "0018_retention_ops"
branch_labels: tuple[str, ...] | None = None
depends_on: str | None = None


def _alter_projection_counters(
    from_type: sa.types.TypeEngine, to_type: sa.types.TypeEngine
) -> None:
    with op.batch_alter_table("quota_projection") as batch_op:
        for name in ("base_limit", "extra_granted", "used"):
            batch_op.alter_column(
                name,
                existing_type=from_type,
                type_=to_type,
                existing_nullable=False,
            )


def upgrade() -> None:
    _alter_projection_counters(sa.Integer(), sa.BigInteger())


def downgrade() -> None:
    _alter_projection_counters(sa.BigInteger(), sa.Integer())
