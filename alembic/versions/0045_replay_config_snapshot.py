"""Freeze the replay-time processing config snapshot on ingestion jobs.

Revision ID: 0045_replay_config_snapshot
Revises: 0044_ab_candidate_config_mapping
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0045_replay_config_snapshot"
down_revision: str | None = "0044_ab_candidate_config_mapping"
branch_labels: tuple[str, ...] | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _column_exists(bind, table: str, column: str) -> bool:
    # documents_metadata.create_all 的新装库已含该列，只有从旧版本升级的库
    # 需要补列，因此本迁移必须幂等。
    inspector = sa.inspect(bind)
    return any(col["name"] == column for col in inspector.get_columns(table))


def upgrade() -> None:
    bind = op.get_bind()
    if not _column_exists(bind, "ingestion_jobs", "replay_config_snapshot_json"):
        with op.batch_alter_table("ingestion_jobs") as batch:
            batch.add_column(sa.Column("replay_config_snapshot_json", sa.JSON(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    if _column_exists(bind, "ingestion_jobs", "replay_config_snapshot_json"):
        with op.batch_alter_table("ingestion_jobs") as batch:
            batch.drop_column("replay_config_snapshot_json")
