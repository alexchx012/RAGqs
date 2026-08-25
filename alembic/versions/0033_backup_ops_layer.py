"""Backup operations layer: policy, schedule occurrences, idempotency commands,
write gate and cleanup targets, plus backup purge marker and the durable
single-active-restore index.

Revision ID: 0033_backup_ops_layer
Revises: 0032_backup_restore_ops

0032 creates its tables from the shared ``backup_metadata``, so fresh databases
already carry every column/index defined in the current schema module; all
statements here are inspect-guarded to stay idempotent for both fresh and
pre-0033 databases.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.backup.schema import (
    backup_cleanup_targets_table,
    backup_metadata,
    backup_policy_table,
    backup_schedule_occurrences_table,
    backup_write_gate_table,
    ops_idempotency_commands_table,
)

revision: str = "0033_backup_ops_layer"
down_revision: str | None = "0032_backup_restore_ops"
branch_labels: tuple[str, ...] | Sequence[str] | None = None
depends_on: tuple[str, ...] | Sequence[str] | None = None

_NEW_TABLES = [
    backup_policy_table,
    backup_schedule_occurrences_table,
    ops_idempotency_commands_table,
    backup_write_gate_table,
    backup_cleanup_targets_table,
]
_ACTIVE_RESTORE_WHERE = sa.text("status IN ('accepted','running','blocked')")


def _columns(bind, table: str) -> set[str]:
    inspector = sa.inspect(bind)
    return {column["name"] for column in inspector.get_columns(table)}


def _indexes(bind, table: str) -> set[str]:
    inspector = sa.inspect(bind)
    return {index["name"] for index in inspector.get_indexes(table)}


def upgrade() -> None:
    bind = op.get_bind()
    backup_metadata.create_all(bind, tables=_NEW_TABLES)
    if "purged_at_utc" not in _columns(bind, "backup_sets"):
        with op.batch_alter_table("backup_sets") as batch:
            batch.add_column(sa.Column("purged_at_utc", sa.DateTime(timezone=True), nullable=True))
    if "uq_restore_sessions_active" not in _indexes(bind, "restore_sessions"):
        op.create_index(
            "uq_restore_sessions_active",
            "restore_sessions",
            ["status"],
            unique=True,
            sqlite_where=_ACTIVE_RESTORE_WHERE,
            postgresql_where=_ACTIVE_RESTORE_WHERE,
        )


def downgrade() -> None:
    bind = op.get_bind()
    if "uq_restore_sessions_active" in _indexes(bind, "restore_sessions"):
        op.drop_index("uq_restore_sessions_active", table_name="restore_sessions")
    if "purged_at_utc" in _columns(bind, "backup_sets"):
        with op.batch_alter_table("backup_sets") as batch:
            batch.drop_column("purged_at_utc")
    for table in reversed(_NEW_TABLES):
        op.drop_table(table.name)
