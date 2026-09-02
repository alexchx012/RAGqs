"""Drop the duplicate notification recipient-sequence index.

Revision ID: 0047_drop_notification_seq_idx
Revises: 0046_quota_approver_department
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0047_drop_notification_seq_idx"
down_revision: str | None = "0046_quota_approver_department"
branch_labels: tuple[str, ...] | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _index_exists(bind, table: str, index: str) -> bool:
    # outbox_metadata.create_all 的新装库按当前 schema 建表，不再创建该索引；
    # 只有从旧版本升级的库需要 drop，因此本迁移必须幂等。
    inspector = sa.inspect(bind)
    return any(ix["name"] == index for ix in inspector.get_indexes(table))


def upgrade() -> None:
    bind = op.get_bind()
    if _index_exists(bind, "notification", "ix_notification_recipient_seq"):
        op.drop_index("ix_notification_recipient_seq", table_name="notification")


def downgrade() -> None:
    bind = op.get_bind()
    if not _index_exists(bind, "notification", "ix_notification_recipient_seq"):
        op.create_index(
            "ix_notification_recipient_seq",
            "notification",
            ["recipient_user_id", "notification_seq"],
        )
