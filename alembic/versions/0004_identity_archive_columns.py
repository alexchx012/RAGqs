"""Add archive-proof and retirement columns to the identity deletion workflow.

Revision ID: 0004_identity_archive_columns
Revises: 0003_outbox_notifications
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0004_identity_archive_columns"
down_revision: str | None = "0003_outbox_notifications"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "identity_deletion_workflow",
        sa.Column("archive_ref", sa.String(length=512), nullable=True),
    )
    op.add_column(
        "identity_deletion_workflow",
        sa.Column("archive_checksum", sa.String(length=128), nullable=True),
    )
    op.add_column(
        "identity_deletion_workflow",
        sa.Column("retirement_receipt_id", sa.String(length=128), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("identity_deletion_workflow", "retirement_receipt_id")
    op.drop_column("identity_deletion_workflow", "archive_checksum")
    op.drop_column("identity_deletion_workflow", "archive_ref")
