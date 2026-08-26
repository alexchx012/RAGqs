"""Add the read-lease token to document_read_leases.

Revision ID: 0035_document_read_lease_token
Revises: 0034_backup_skipped_missed

Read leases become renewable document_version_reference leases keyed by
``(reference_id, owner_id, lease_token)``.  Databases created after this
revision get the column from ``documents_metadata`` directly; the inspect
guard keeps this a no-op there and only adds it where 0012 already ran.
Rows that predate the token keep the empty default, which matches no
generated token, so they simply expire as the short request leases they are.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0035_document_read_lease_token"
down_revision: str | None = "0034_backup_skipped_missed"
branch_labels: tuple[str, ...] | Sequence[str] | None = None
depends_on: tuple[str, ...] | Sequence[str] | None = None

_TABLE = "document_read_leases"
_COLUMN = "lease_token"


def _has_column(bind) -> bool:
    inspector = sa.inspect(bind)
    return _COLUMN in {column["name"] for column in inspector.get_columns(_TABLE)}


def upgrade() -> None:
    if _has_column(op.get_bind()):
        return
    with op.batch_alter_table(_TABLE) as batch:
        batch.add_column(sa.Column(_COLUMN, sa.String(128), nullable=False, server_default=""))


def downgrade() -> None:
    if not _has_column(op.get_bind()):
        return
    with op.batch_alter_table(_TABLE) as batch:
        batch.drop_column(_COLUMN)
