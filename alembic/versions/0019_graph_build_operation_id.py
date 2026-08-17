"""Expand graph operation identifiers for the public idempotency-key boundary."""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "0019_graph_build_operation_id"
down_revision: str | None = "0018_retention_ops"
branch_labels: tuple[str, ...] | None = None
depends_on: str | None = None


def upgrade() -> None:
    with op.batch_alter_table("graph_build_operations") as batch_op:
        batch_op.alter_column(
            "operation_id",
            existing_type=sa.String(length=64),
            type_=sa.String(length=266),
            existing_nullable=False,
        )


def downgrade() -> None:
    with op.batch_alter_table("graph_build_operations") as batch_op:
        batch_op.alter_column(
            "operation_id",
            existing_type=sa.String(length=266),
            type_=sa.String(length=64),
            existing_nullable=False,
        )
