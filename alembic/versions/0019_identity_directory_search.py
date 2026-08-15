"""Persist normalized user-directory search text.

Revision ID: 0019_identity_directory_search
Revises: 0018_retention_ops
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0019_identity_directory_search"
down_revision: str | None = "0018_retention_ops"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _directory_search_text(row: Mapping[str, object]) -> str:
    return " ".join(
        (
            str(row["username"]),
            str(row["real_name"]),
            str(row["display_name"]),
            str(row["role"]),
        )
    ).casefold()


def upgrade() -> None:
    op.add_column(
        "identity_user",
        sa.Column(
            "directory_search_text",
            sa.String(length=4096),
            nullable=False,
            server_default="",
        ),
    )
    identity_user = sa.table(
        "identity_user",
        sa.column("id", sa.String(length=64)),
        sa.column("username", sa.String(length=128)),
        sa.column("real_name", sa.String(length=256)),
        sa.column("display_name", sa.String(length=256)),
        sa.column("role", sa.String(length=32)),
        sa.column("directory_search_text", sa.String(length=4096)),
    )
    bind = op.get_bind()
    rows = bind.execute(
        sa.select(
            identity_user.c.id,
            identity_user.c.username,
            identity_user.c.real_name,
            identity_user.c.display_name,
            identity_user.c.role,
        )
    ).mappings()
    for row in rows:
        bind.execute(
            sa.update(identity_user)
            .where(identity_user.c.id == row["id"])
            .values(directory_search_text=_directory_search_text(row))
        )


def downgrade() -> None:
    op.drop_column("identity_user", "directory_search_text")
