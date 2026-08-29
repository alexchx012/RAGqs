"""Persist the server request identifier on chat generations."""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0040_chat_sse_contracts"
down_revision: str | None = "0039_chat_release_merge"
branch_labels: tuple[str, ...] | Sequence[str] | None = None
depends_on: tuple[str, ...] | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    columns = {column["name"] for column in sa.inspect(bind).get_columns("chat_generation")}
    if "request_id" in columns:
        return
    with op.batch_alter_table("chat_generation") as batch:
        batch.add_column(sa.Column("request_id", sa.String(length=128), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    columns = {column["name"] for column in sa.inspect(bind).get_columns("chat_generation")}
    if "request_id" not in columns:
        return
    with op.batch_alter_table("chat_generation") as batch:
        batch.drop_column("request_id")
