"""Create the greenfield chat-generation domain tables."""

from __future__ import annotations

from alembic import op
from app.chat.schema import chat_metadata

revision: str = "0016_chat_generation"
down_revision: str | None = "0015_public_graph_builds"
branch_labels: tuple[str, ...] | None = None
depends_on: str | None = None


def upgrade() -> None:
    chat_metadata.create_all(op.get_bind())


def downgrade() -> None:
    chat_metadata.drop_all(op.get_bind())
