"""Outbox compact-due partial index, metric observed index and inbox backfill.

- `ix_outbox_event_compact_due`: the compaction candidate scan previously
  sequenced over the whole monotonically growing `outbox_event` table; the
  partial index covers due full events only (compacted tombstones keep
  `compact_after_at_utc` cleared, so they never enter the index).
- `ix_outbox_metric_observed`: metric prune deletes by `observed_at_utc`.
- `notification_inbox` backfill: every pre-existing user gets exactly one
  inbox row (next seq 1, read-through 0), except retired accounts whose inbox
  removal was permanent (retirement tombstone).

Revision ID: 0040_compact_due_inbox_backfill
Revises: 0039_chat_release_merge
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import text

from alembic import op

revision: str = "0040_compact_due_inbox_backfill"
down_revision: str | None = "0039_chat_release_merge"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_COMPACT_DUE_PREDICATE = "storage_state = 'full' AND compact_after_at_utc IS NOT NULL"


def upgrade() -> None:
    op.create_index(
        "ix_outbox_event_compact_due",
        "outbox_event",
        ["compact_after_at_utc"],
        postgresql_where=text(_COMPACT_DUE_PREDICATE),
        sqlite_where=text(_COMPACT_DUE_PREDICATE),
    )
    op.create_index("ix_outbox_metric_observed", "outbox_metric", ["observed_at_utc"])
    op.execute(
        "INSERT INTO notification_inbox "
        "(recipient_user_id, next_notification_seq, read_through_seq, "
        "read_all_at_utc, version, retired) "
        "SELECT u.id, 1, 0, NULL, 1, FALSE FROM identity_user u "
        "WHERE NOT EXISTS ("
        "SELECT 1 FROM notification_inbox i WHERE i.recipient_user_id = u.id) "
        "AND NOT EXISTS ("
        "SELECT 1 FROM outbox_account_retirement_tombstone t "
        "WHERE t.recipient_user_id = u.id)"
    )


def downgrade() -> None:
    op.drop_index("ix_outbox_metric_observed", table_name="outbox_metric")
    op.drop_index("ix_outbox_event_compact_due", table_name="outbox_event")
