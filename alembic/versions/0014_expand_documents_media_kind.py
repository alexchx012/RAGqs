"""Expand Documents media-kind columns for standard Office MIME values."""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "0014_expand_documents_media_kind"
down_revision: str | None = "0013_search_indexing"
branch_labels: tuple[str, ...] | None = None
depends_on: str | None = None


def upgrade() -> None:
    for table_name, nullable in (
        ("documents", True),
        ("document_versions", True),
        ("knowledge_submissions", False),
        ("index_chunks", False),
    ):
        with op.batch_alter_table(table_name) as batch:
            batch.alter_column(
                "media_kind",
                existing_type=sa.String(length=64),
                type_=sa.String(length=128),
                existing_nullable=nullable,
            )


def downgrade() -> None:
    for table_name, nullable in (
        ("documents", True),
        ("document_versions", True),
        ("knowledge_submissions", False),
        ("index_chunks", False),
    ):
        with op.batch_alter_table(table_name) as batch:
            batch.alter_column(
                "media_kind",
                existing_type=sa.String(length=128),
                type_=sa.String(length=64),
                existing_nullable=nullable,
            )
