"""Record cancellation operator on ingestion jobs.

Revision ID: 0024_ingestion_cancel_operator
Revises: 0023_ledger_secondary_indexes
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0024_ingestion_cancel_operator"
down_revision: str | None = "0023_ledger_secondary_indexes"
branch_labels: tuple[str, ...] | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _column_exists(bind, table: str, column: str) -> bool:
    # 0012 用 documents_metadata.create_all 建表：新装库已含该列，
    # 只有从旧版本升级的库需要补列，因此本迁移必须幂等。
    inspector = sa.inspect(bind)
    return any(col["name"] == column for col in inspector.get_columns(table))


def upgrade() -> None:
    bind = op.get_bind()
    if not _column_exists(bind, "ingestion_jobs", "cancelled_by_user_id"):
        op.add_column(
            "ingestion_jobs",
            sa.Column("cancelled_by_user_id", sa.String(length=64), nullable=True),
        )


def downgrade() -> None:
    bind = op.get_bind()
    if _column_exists(bind, "ingestion_jobs", "cancelled_by_user_id"):
        op.drop_column("ingestion_jobs", "cancelled_by_user_id")
