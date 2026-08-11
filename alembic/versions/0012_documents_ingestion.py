"""Create the greenfield documents-ingestion domain tables."""

from __future__ import annotations

from alembic import op
from app.documents.schema import documents_metadata

revision: str = "0012_documents_ingestion"
down_revision: str | None = "0011_usage_quota"
branch_labels: tuple[str, ...] | None = None
depends_on: str | None = None


def upgrade() -> None:
    documents_metadata.create_all(op.get_bind())


def downgrade() -> None:
    documents_metadata.drop_all(op.get_bind())
