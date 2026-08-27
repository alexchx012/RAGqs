"""Merge the chat generation recovery and retrieval release gate heads."""

from __future__ import annotations

from collections.abc import Sequence

revision: str = "0039_merge_chat_recovery_and_release_gate"
down_revision: tuple[str, str] = (
    "0035_chat_generation_reconciling",
    "0038_retrieval_release_gate",
)
branch_labels: tuple[str, ...] | Sequence[str] | None = None
depends_on: tuple[str, ...] | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
