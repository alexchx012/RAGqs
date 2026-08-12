"""Create the public graph build-run domain tables (greenfield schema)."""

from __future__ import annotations

from alembic import op
from app.graph.schema import graph_metadata

revision: str = "0015_public_graph_builds"
down_revision: str | None = "0014_expand_documents_media_kind"
branch_labels: tuple[str, ...] | None = None
depends_on: str | None = None


def upgrade() -> None:
    graph_metadata.create_all(op.get_bind())


def downgrade() -> None:
    graph_metadata.drop_all(op.get_bind())
