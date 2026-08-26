"""Merge the ab-golden-seed and documents-write-idempotency migration heads.

Revision ID: 0036_merge_documents_heads
Revises: 0035_ab_golden_seed, 0035_documents_write_idempotency
"""

revision: str = "0036_merge_documents_heads"
down_revision: tuple[str, str] = (
    "0035_ab_golden_seed",
    "0035_documents_write_idempotency",
)
branch_labels: tuple[str, ...] | None = None
depends_on: str | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
