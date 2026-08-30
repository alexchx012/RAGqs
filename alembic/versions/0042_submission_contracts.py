"""Align submission review persistence with the public review contracts."""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0042_submission_contracts"
down_revision: str | None = "0041_merge_chat_sse_compact"
branch_labels: tuple[str, ...] | Sequence[str] | None = None
depends_on: tuple[str, ...] | Sequence[str] | None = None


def upgrade() -> None:
    existing_columns = {
        column["name"] for column in sa.inspect(op.get_bind()).get_columns("knowledge_submissions")
    }
    with op.batch_alter_table("knowledge_submissions") as batch:
        if "submitter_role_snapshot" not in existing_columns:
            batch.add_column(
                sa.Column("submitter_role_snapshot", sa.String(length=32), nullable=True)
            )
        if "submitter_department_snapshot" not in existing_columns:
            batch.add_column(
                sa.Column("submitter_department_snapshot", sa.String(length=64), nullable=True)
            )
        if "submitter_display_name_snapshot" not in existing_columns:
            batch.add_column(
                sa.Column("submitter_display_name_snapshot", sa.String(length=256), nullable=True)
            )
        if "submitter_department_name_snapshot" not in existing_columns:
            batch.add_column(
                sa.Column(
                    "submitter_department_name_snapshot", sa.String(length=256), nullable=True
                )
            )
        if "invalidated_reason" not in existing_columns:
            batch.add_column(sa.Column("invalidated_reason", sa.String(length=256), nullable=True))
        if "invalidated_at" not in existing_columns:
            batch.add_column(sa.Column("invalidated_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("knowledge_submissions") as batch:
        batch.drop_column("invalidated_at")
        batch.drop_column("invalidated_reason")
        batch.drop_column("submitter_department_name_snapshot")
        batch.drop_column("submitter_display_name_snapshot")
        batch.drop_column("submitter_department_snapshot")
        batch.drop_column("submitter_role_snapshot")
