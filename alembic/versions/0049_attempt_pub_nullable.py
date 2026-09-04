"""Relax ingestion_attempts.publication_id for idempotent staged-publication reclaim.

Revision ID: 0049_attempt_pub_nullable
Revises: 0048_doc_ingest_retire_idx
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0049_attempt_pub_nullable"
down_revision: str | None = "0048_doc_ingest_retire_idx"
branch_labels: tuple[str, ...] | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 终态回收（fail/cancel/lease 过期）删除 staged publication 以满足
    # uq_publications_version_generation_status 的同键幂等（重试 attempt 的
    # 第二行 discarded 会撞唯一键，曾令摄取 worker 全循环崩溃）；删除前先
    # 解除 attempt 的引用，因此该列放宽为 nullable。
    # batch 模式：SQLite 无法原位 ALTER COLUMN（迁移测试跑 SQLite），PG 走原生 ALTER。
    with op.batch_alter_table("ingestion_attempts") as batch:
        batch.alter_column("publication_id", existing_type=sa.String(128), nullable=True)


def downgrade() -> None:
    bind = op.get_bind()
    null_rows = bind.execute(
        sa.text("SELECT count(*) FROM ingestion_attempts WHERE publication_id IS NULL")
    ).scalar_one()
    if null_rows:
        raise RuntimeError(
            "cannot re-apply NOT NULL on ingestion_attempts.publication_id: "
            f"{null_rows} terminal attempt rows hold NULL references"
        )
    with op.batch_alter_table("ingestion_attempts") as batch:
        batch.alter_column("publication_id", existing_type=sa.String(128), nullable=False)
