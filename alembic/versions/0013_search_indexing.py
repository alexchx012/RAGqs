"""Create the rebuildable search-indexing domain tables."""

from __future__ import annotations

from alembic import op
from app.indexing.schema import indexing_metadata

revision: str = "0013_search_indexing"
down_revision: str | None = "0012_documents_ingestion"
branch_labels: tuple[str, ...] | None = None
depends_on: str | None = None


def upgrade() -> None:
    indexing_metadata.create_all(op.get_bind())


def downgrade() -> None:
    indexing_metadata.drop_all(op.get_bind())
