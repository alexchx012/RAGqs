"""Add cancellation time and replay operator columns to ingestion jobs.

Revision ID: 0043_doc_lifecycle_invariants
Revises: 0042_submission_contracts

``cancelled_at_utc`` records the cancellation transaction time independently of
``updated_at_utc``; ``replayed_by_user_id`` records the ops operator of the
current replay generation without rewriting the original ``created_by_user_id``.
Databases created after this revision get both columns from
``documents_metadata`` directly, so the inspect guards keep this a no-op there.
Historical rows keep NULL: no backfill.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0043_doc_lifecycle_invariants"
down_revision: str | None = "0042_submission_contracts"
branch_labels: tuple[str, ...] | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_COLUMNS = (
    ("cancelled_at_utc", sa.DateTime(timezone=True)),
    ("replayed_by_user_id", sa.String(length=64)),
)


def _existing_columns(bind, table: str) -> set[str]:
    inspector = sa.inspect(bind)
    return {column["name"] for column in inspector.get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    existing = _existing_columns(bind, "ingestion_jobs")
    for name, column in _COLUMNS:
        if name not in existing:
            op.add_column("ingestion_jobs", sa.Column(name, column, nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    existing = _existing_columns(bind, "ingestion_jobs")
    for name, _column in reversed(_COLUMNS):
        if name in existing:
            op.drop_column("ingestion_jobs", name)
