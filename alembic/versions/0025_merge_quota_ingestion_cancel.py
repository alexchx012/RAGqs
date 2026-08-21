"""Merge quota-transaction and ingestion-cancel migration heads.

Revision ID: 0025_merge_quota_ingestion_cancel
Revises: 0024_fix_quota_transaction, 0024_ingestion_cancel_operator
"""

revision: str = "0025_merge_quota_ingestion_cancel"
down_revision: tuple[str, str] = (
    "0024_fix_quota_transaction",
    "0024_ingestion_cancel_operator",
)
branch_labels: tuple[str, ...] | None = None
depends_on: str | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
