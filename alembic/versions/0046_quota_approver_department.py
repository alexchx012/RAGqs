"""Freeze the approver department snapshot on quota requests.

Revision ID: 0046_quota_approver_department
Revises: 0045_replay_config_snapshot
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0046_quota_approver_department"
down_revision: str | None = "0045_replay_config_snapshot"
branch_labels: tuple[str, ...] | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _column_exists(bind, table: str, column: str) -> bool:
    # usage_metadata.create_all 的新装库已含该列，只有从旧版本升级的库
    # 需要补列，因此本迁移必须幂等。
    inspector = sa.inspect(bind)
    return any(col["name"] == column for col in inspector.get_columns(table))


def upgrade() -> None:
    bind = op.get_bind()
    if not _column_exists(bind, "quota_request", "approver_department_id"):
        with op.batch_alter_table("quota_request") as batch:
            batch.add_column(sa.Column("approver_department_id", sa.String(length=64)))


def downgrade() -> None:
    bind = op.get_bind()
    if _column_exists(bind, "quota_request", "approver_department_id"):
        with op.batch_alter_table("quota_request") as batch:
            batch.drop_column("approver_department_id")
