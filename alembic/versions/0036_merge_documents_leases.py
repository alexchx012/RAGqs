"""Merge the golden-seed and documents-write-idempotency migration heads.

Revision ID: 0036_merge_documents_leases
Revises: 0035_ab_golden_seed, 0035_documents_write_idempotency
"""

from __future__ import annotations

from collections.abc import Sequence

revision: str = "0036_merge_documents_leases"
down_revision: tuple[str, str] = (
    "0035_ab_golden_seed",
    "0035_documents_write_idempotency",
)
branch_labels: tuple[str, ...] | Sequence[str] | None = None
depends_on: tuple[str, ...] | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
