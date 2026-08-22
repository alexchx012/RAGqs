"""Account-deletion archive and cleanup-target bookkeeping.

Revision ID: 0028_account_deletion_archive
Revises: 0027_merge_ab_vote_idempotency
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0028_account_deletion_archive"
down_revision: str | None = "0027_merge_ab_vote_idempotency"
branch_labels: tuple[str, ...] | Sequence[str] | None = None
depends_on: tuple[str, ...] | Sequence[str] | None = None


def _columns(bind, table: str) -> set[str]:
    # 0012 用各域 metadata.create_all 建表：新装库的 identity 表已随 schema.py
    # 含这些列，只有从旧版本升级的库需要补齐，因此本迁移必须幂等。
    inspector = sa.inspect(bind)
    return {column["name"] for column in inspector.get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    existing = _columns(bind, "identity_deletion_workflow")
    with op.batch_alter_table("identity_deletion_workflow") as batch:
        if "archive_dir_snapshot" not in existing:
            batch.add_column(
                sa.Column("archive_dir_snapshot", sa.String(length=1024), nullable=True)
            )
        if "archive_file_name" not in existing:
            batch.add_column(sa.Column("archive_file_name", sa.String(length=256), nullable=True))
        if "archive_size_bytes" not in existing:
            batch.add_column(sa.Column("archive_size_bytes", sa.Integer(), nullable=True))
        if "archive_sha256" not in existing:
            batch.add_column(sa.Column("archive_sha256", sa.String(length=128), nullable=True))
        if "archive_completed_at_utc" not in existing:
            batch.add_column(
                sa.Column("archive_completed_at_utc", sa.DateTime(timezone=True), nullable=True)
            )
        if "archive_failed_at_utc" not in existing:
            batch.add_column(
                sa.Column("archive_failed_at_utc", sa.DateTime(timezone=True), nullable=True)
            )
        if "archive_alert" not in existing:
            batch.add_column(sa.Column("archive_alert", sa.String(length=64), nullable=True))
    table_names = set(sa.inspect(bind).get_table_names())
    if "identity_account_cleanup_target" not in table_names:
        op.create_table(
            "identity_account_cleanup_target",
            sa.Column("deletion_id", sa.String(length=128), primary_key=True),
            sa.Column("backend_kind", sa.String(length=64), primary_key=True),
            sa.Column("resource_id", sa.String(length=256), primary_key=True),
            sa.Column("status", sa.String(length=32), nullable=False),
            sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("last_error", sa.String(length=512), nullable=True),
            sa.Column("created_at_utc", sa.DateTime(timezone=True), nullable=False),
            sa.Column("completed_at_utc", sa.DateTime(timezone=True), nullable=True),
            sa.Column("updated_at_utc", sa.DateTime(timezone=True), nullable=False),
        )


def downgrade() -> None:
    bind = op.get_bind()
    table_names = set(sa.inspect(bind).get_table_names())
    if "identity_account_cleanup_target" in table_names:
        op.drop_table("identity_account_cleanup_target")
    with op.batch_alter_table("identity_deletion_workflow") as batch:
        for column in (
            "archive_alert",
            "archive_failed_at_utc",
            "archive_completed_at_utc",
            "archive_sha256",
            "archive_size_bytes",
            "archive_file_name",
            "archive_dir_snapshot",
        ):
            batch.drop_column(column)
