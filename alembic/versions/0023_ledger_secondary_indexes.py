"""Add secondary indexes for append-only ledger tables.

Revision ID: 0023_ledger_secondary_indexes
Revises: 0022_merge_graph_op_id
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0023_ledger_secondary_indexes"
down_revision: str | None = "0022_merge_graph_op_id"
branch_labels: tuple[str, ...] | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# (table, index_name, columns)：追加式账本/审计表的 rebuild、reversal 累计、
# reconcile stale 扫描与 compaction 回收路径此前全部走顺序扫描。
INDEXES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("quota_debit", "ix_quota_debit_subject_period", ("quota_subject_user_id", "quota_period")),
    ("quota_debit", "ix_quota_debit_kind_reference", ("entry_kind", "referenced_debit_id")),
    ("provider_call", "ix_provider_call_reconcile", ("status", "dispatching_at_utc")),
    ("outbox_delivery_attempt", "ix_outbox_delivery_attempt_event", ("event_id",)),
)


def upgrade() -> None:
    for table, name, columns in INDEXES:
        op.create_index(name, table, list(columns))


def downgrade() -> None:
    for table, name, _columns in INDEXES:
        op.drop_index(name, table_name=table)
