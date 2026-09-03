"""Secondary indexes for upload-batch, ingestion-job and retire-due scans.

Revision ID: 0048_doc_ingest_retire_idx
Revises: 0047_drop_notification_seq_idx
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0048_doc_ingest_retire_idx"
down_revision: str | None = "0047_drop_notification_seq_idx"
branch_labels: tuple[str, ...] | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# (table, index name, columns)
_INDEXES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("upload_batch_items", "ix_upload_batch_items_batch", ("upload_batch_id",)),
    ("ingestion_jobs", "ix_ingestion_jobs_document", ("document_id",)),
    # notification 行随 retire 删除且 retire_after_at_utc NOT NULL，到期扫描的
    # 索引无需 partial predicate（对照 outbox compact 扫描的 due 索引）。
    ("notification", "ix_notification_retire_due", ("retire_after_at_utc",)),
)


def _indexes(bind, table: str) -> set[str]:
    # 0012 等迁移用 metadata.create_all 建表：新装库的 documents 表已随当前
    # schema.py 带上这些索引，守卫避免重复创建；notification 表（0003 冻结
    # 定义）则由本迁移统一补建。守卫令 upgrade/downgrade 幂等对称
    # （0026 既有写法）。
    inspector = sa.inspect(bind)
    return {index["name"] for index in inspector.get_indexes(table) if index.get("name")}


def upgrade() -> None:
    bind = op.get_bind()
    for table, name, columns in _INDEXES:
        if name not in _indexes(bind, table):
            op.create_index(name, table, list(columns))


def downgrade() -> None:
    bind = op.get_bind()
    for table, name, _columns in reversed(_INDEXES):
        if name in _indexes(bind, table):
            op.drop_index(name, table_name=table)
