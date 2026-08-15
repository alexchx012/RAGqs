"""Regular notification retention maintenance.

Per user, only the newest 50 unexpired notifications stay online. When a
notification expires (`retire_after_at`) or exceeds the cap, one transaction
first writes/verifies the permanent delivery receipt, then removes the
notification and its context ack. Sequence and watermark never regress.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.engine import Connection, Engine

from app.platform.errors import PlatformError

from .compaction import canonical_receipt_fingerprint
from .schema import (
    notification_context_ack_table,
    notification_delivery_receipt_table,
    notification_table,
)

_logger = logging.getLogger(__name__)

MAX_ONLINE_NOTIFICATIONS = 50


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class NotificationRetentionMaintenance:
    """Scans and retires expired or over-cap notifications for all users."""

    def __init__(
        self,
        engine: Engine,
        *,
        now: Callable[[], datetime],
        max_online: int = MAX_ONLINE_NOTIFICATIONS,
    ) -> None:
        self._engine = engine
        self._now = now
        self._max_online = max_online

    def run_once(self, *, limit: int = 1000) -> int:
        """Retire due notifications; returns the number of retired rows."""
        with self._engine.begin() as connection:
            now = _utc(self._now())
            return self._retire_due(connection, now=now, limit=limit)

    def run_forever(
        self,
        *,
        interval_seconds: float = 3600.0,
        limit: int = 1000,
        stop: Any = None,
    ) -> None:
        """Continuous retention loop: retire, compact, prune, sleep."""
        import time

        while True:
            if stop is not None and stop():
                return
            try:
                self.run_once(limit=limit)
            except Exception:
                _logger.exception("notification retention loop iteration failed")
            time.sleep(interval_seconds)

    def _retire_due(self, connection: Connection, *, now: datetime, limit: int) -> int:
        retired = 0
        # Expired notifications first (receipt + delete + ack delete).
        expired = (
            connection.execute(
                select(notification_table.c.id)
                .where(notification_table.c.retire_after_at_utc <= now)
                .limit(limit)
            )
            .scalars()
            .all()
        )
        for notification_id in expired:
            if self._retire_notification(connection, notification_id, now):
                retired += 1
        # Over-cap newest-first trim per user.
        users = (
            connection.execute(
                select(notification_table.c.recipient_user_id)
                .group_by(notification_table.c.recipient_user_id)
                .having(func.count() > self._max_online)
            )
            .scalars()
            .all()
        )
        for user_id in users:
            if retired >= limit:
                break
            over = (
                connection.execute(
                    select(
                        notification_table.c.id,
                        notification_table.c.notification_seq,
                    )
                    .where(
                        notification_table.c.recipient_user_id == user_id,
                        notification_table.c.retire_after_at_utc > now,
                    )
                    .order_by(notification_table.c.notification_seq.desc())
                    .offset(self._max_online)
                )
                .mappings()
                .all()
            )
            for row in over:
                if self._retire_notification(connection, str(row["id"]), now):
                    retired += 1
        return retired

    def _retire_notification(
        self,
        connection: Connection,
        notification_id: str,
        now: datetime,
    ) -> bool:
        """Atomically write/verify the receipt, then delete notification + ack."""
        row = (
            connection.execute(
                select(
                    notification_table.c.event_id,
                    notification_table.c.recipient_user_id,
                    notification_table.c.notification_seq,
                    notification_table.c.event_occurred_at_utc,
                    notification_table.c.materialized_at_utc,
                ).where(notification_table.c.id == notification_id)
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            return False
        event_id = str(row["event_id"])
        user_id = str(row["recipient_user_id"])
        seq = int(row["notification_seq"])
        existing = (
            connection.execute(
                select(
                    notification_delivery_receipt_table.c.outcome,
                    notification_delivery_receipt_table.c.original_notification_seq,
                    notification_delivery_receipt_table.c.fingerprint,
                ).where(
                    notification_delivery_receipt_table.c.event_id == event_id,
                    notification_delivery_receipt_table.c.recipient_user_id == user_id,
                )
            )
            .mappings()
            .one_or_none()
        )
        if existing is None:
            connection.execute(
                notification_delivery_receipt_table.insert().values(
                    event_id=event_id,
                    recipient_user_id=user_id,
                    outcome="materialized",
                    original_notification_seq=seq,
                    occurred_at_utc=row["event_occurred_at_utc"],
                    materialized_at_utc=row["materialized_at_utc"],
                    retired_at_utc=now,
                    fingerprint=canonical_receipt_fingerprint(
                        event_id,
                        user_id,
                        "materialized",
                        seq,
                    ),
                )
            )
        else:
            # An existing receipt must be fully verified: outcome, sequence and
            # fingerprint are immutable. A mismatch is an invariant violation
            # and the projection is NOT deleted.
            if (
                str(existing["outcome"]) != "materialized"
                or int(existing["original_notification_seq"]) != seq
                or str(existing["fingerprint"])
                != canonical_receipt_fingerprint(event_id, user_id, "materialized", seq)
            ):
                raise PlatformError(
                    "receipt_fingerprint_mismatch",
                    "Delivery receipt does not match the immutable receipt record",
                    {},
                    409,
                )
        # Delete the projection and its context ack; inbox watermark is untouched.
        connection.execute(
            delete(notification_table).where(notification_table.c.id == notification_id)
        )
        connection.execute(
            delete(notification_context_ack_table).where(
                notification_context_ack_table.c.event_id == event_id,
                notification_context_ack_table.c.recipient_user_id == user_id,
            )
        )
        return True
