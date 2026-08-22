"""Merge chat-reconcile-drop with the quota/ingestion/ab-vote migration chain.

Revision ID: 0028_merge_chat_reconcile_drop
Revises: 0027_merge_ab_vote_idempotency, 0024_chat_reconcile_drop
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0028_merge_chat_reconcile_drop"
down_revision: tuple[str, str] | Sequence[str] | None = (
    "0027_merge_ab_vote_idempotency",
    "0024_chat_reconcile_drop",
)
branch_labels: tuple[str, ...] | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
