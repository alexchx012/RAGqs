"""Merge the chat SSE contracts and outbox compact/inbox migration heads."""

from __future__ import annotations

from collections.abc import Sequence

revision: str = "0041_merge_chat_sse_compact"
down_revision: tuple[str, str] = (
    "0040_chat_sse_contracts",
    "0040_compact_due_inbox_backfill",
)
branch_labels: tuple[str, ...] | Sequence[str] | None = None
depends_on: tuple[str, ...] | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
