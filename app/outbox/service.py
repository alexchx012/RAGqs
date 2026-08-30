"""Notification read model service: list, read, read-all and event ack."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select, text, update
from sqlalchemy.engine import Connection, Engine

from app.platform.errors import PlatformError

from .ports import ACKNOWLEDGEABLE_EVENT_TYPES
from .schema import (
    notification_context_ack_table,
    notification_delivery_receipt_table,
    notification_inbox_table,
    notification_suppression_table,
    notification_table,
    outbox_event_table,
    outbox_recipient_table,
)


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class NotificationService:
    """Owns the current-user notification read model and read-state mutations."""

    def __init__(
        self,
        engine: Engine,
        *,
        now: Callable[[], datetime] | None = None,
        clock: Any = None,
    ) -> None:
        self._engine = engine
        self._now = now or (lambda: datetime.now(UTC))
        self._clock = clock

    def _current_time(self, connection: Connection) -> datetime:
        if self._clock is not None:
            value = self._clock.now_utc(connection)
            return value if isinstance(value, datetime) else _utc(self._now())
        return _utc(self._now())

    def list_notifications(self, user_id: str, *, limit: int) -> list[dict[str, object]]:
        with self._engine.connect() as connection:
            now = self._current_time(connection)
            rows = (
                connection.execute(
                    select(
                        notification_table.c.id,
                        notification_table.c.notification_type,
                        notification_table.c.title,
                        notification_table.c.payload_json,
                        notification_table.c.read_at_utc,
                        notification_table.c.event_occurred_at_utc,
                        notification_table.c.notification_seq,
                        notification_inbox_table.c.read_through_seq,
                    )
                    .outerjoin(
                        notification_inbox_table,
                        notification_inbox_table.c.recipient_user_id
                        == notification_table.c.recipient_user_id,
                    )
                    .where(
                        notification_table.c.recipient_user_id == user_id,
                        notification_table.c.retire_after_at_utc > now,
                    )
                    .order_by(notification_table.c.notification_seq.desc())
                    .limit(limit)
                )
                .mappings()
                .all()
            )
        items: list[dict[str, object]] = []
        for row in rows:
            seq = int(row["notification_seq"])
            read_through = int(row["read_through_seq"] or 0)
            read = row["read_at_utc"] is not None or seq <= read_through
            items.append(
                {
                    "id": row["id"],
                    "type": row["notification_type"],
                    "title": row["title"],
                    "payload": dict(row["payload_json"]),
                    "read": read,
                    "event_occurred_at": row["event_occurred_at_utc"],
                }
            )
        return items

    def unread_count(self, user_id: str) -> int:
        with self._engine.connect() as connection:
            now = self._current_time(connection)
            count = connection.execute(
                select(func.count())
                .select_from(notification_table)
                .outerjoin(
                    notification_inbox_table,
                    notification_inbox_table.c.recipient_user_id
                    == notification_table.c.recipient_user_id,
                )
                .where(
                    notification_table.c.recipient_user_id == user_id,
                    notification_table.c.retire_after_at_utc > now,
                    notification_table.c.read_at_utc.is_(None),
                    notification_table.c.notification_seq
                    > notification_inbox_table.c.read_through_seq,
                )
            ).scalar_one()
        return int(count)

    def mark_read(self, user_id: str, notification_id: str) -> bool:
        """Write the database time on first read; repeats are no-ops (both 204)."""
        with self._engine.begin() as connection:
            now = self._current_time(connection)
            row = connection.execute(
                select(notification_table.c.id).where(
                    notification_table.c.id == notification_id,
                    notification_table.c.recipient_user_id == user_id,
                )
            ).scalar_one_or_none()
            if row is None:
                raise PlatformError("not_found", "Notification was not found", {}, 404)
            connection.execute(
                update(notification_table)
                .where(
                    notification_table.c.id == notification_id,
                    notification_table.c.recipient_user_id == user_id,
                    notification_table.c.read_at_utc.is_(None),
                )
                .values(read_at_utc=now)
            )
        return True

    def read_all(self, user_id: str, *, connection: Connection | None = None) -> None:
        """Advance the read-through watermark to the highest materialized seq.

        The same advisory lock as materialization serializes the two so a
        notification materialized concurrently is either seen by read-all or
        stays unread; there is no torn watermark.

        An optional caller-supplied connection/transaction is supported for
        integration tests that must hold the advisory lock across a boundary;
        the production API routes always call without it (own transaction).
        """
        if connection is not None:
            self._read_all_in(connection, user_id)
            return
        with self._engine.begin() as active_connection:
            self._read_all_in(active_connection, user_id)

    def _read_all_in(self, connection: Connection, user_id: str) -> None:
        now = self._current_time(connection)
        if connection.dialect.name == "postgresql":
            connection.execute(
                text("SELECT pg_advisory_xact_lock(hashtext(:lock))"),
                {"lock": f"ragqs:notifications:read-all:{user_id}"},
            )
        inbox = (
            connection.execute(
                select(
                    notification_inbox_table.c.next_notification_seq,
                    notification_inbox_table.c.read_through_seq,
                    notification_inbox_table.c.version,
                ).where(notification_inbox_table.c.recipient_user_id == user_id)
            )
            .mappings()
            .one_or_none()
        )
        if inbox is None:
            # inbox 随账号创建事务建立并由迁移回填后，正常运行不会走到这里；
            # 兜底只为直插用户的测试保持可用，创建即一次性写入。
            highest = connection.execute(
                select(func.max(notification_table.c.notification_seq)).where(
                    notification_table.c.recipient_user_id == user_id
                )
            ).scalar_one()
            highest = int(highest or 0)
            connection.execute(
                notification_inbox_table.insert().values(
                    recipient_user_id=user_id,
                    next_notification_seq=highest + 1,
                    read_through_seq=highest,
                    read_all_at_utc=now,
                    version=1,
                    retired=False,
                )
            )
            return
        # 水位基准对齐设计：next_notification_seq - 1（含已退休尾通知，行为
        # 等价）。仅在真正推进水位时写 read_through_seq/read_all_at/version，
        # 无新通知的重复 read-all 逐字段零写入。
        target = int(inbox["next_notification_seq"]) - 1
        if target <= int(inbox["read_through_seq"]):
            return
        updated = connection.execute(
            update(notification_inbox_table)
            .where(
                notification_inbox_table.c.recipient_user_id == user_id,
                notification_inbox_table.c.version == int(inbox["version"]),
            )
            .values(
                read_through_seq=target,
                read_all_at_utc=now,
                version=int(inbox["version"]) + 1,
            )
        ).rowcount
        if updated != 1:
            # 不可达防御分支已删除：PostgreSQL 路径持有 per-user advisory
            # xact lock，SQLite 路径串行写；CAS 失败没有真实的并发来源。
            # 若未来出现真实并发写者，rowcount=0 会让事务以乐观并发语义失败。
            raise PlatformError(
                "inbox_version_conflict",
                "Notification inbox changed concurrently",
                {},
                409,
            )

    def ack_event(self, user_id: str, event_id: str, *, event_type: str | None = None) -> None:
        """Acknowledge an ingestion/ocr event; naturally idempotent per recipient."""
        with self._engine.begin() as connection:
            now = self._current_time(connection)
            event = (
                connection.execute(
                    select(
                        outbox_event_table.c.event_type,
                        outbox_event_table.c.storage_state,
                    ).where(outbox_event_table.c.event_id == event_id)
                )
                .mappings()
                .one_or_none()
            )
            if event is None:
                raise PlatformError("not_found", "Event was not found", {}, 404)
            actual_type = str(event["event_type"])
            if event_type is not None and event_type != actual_type:
                raise PlatformError(
                    "notification_event_not_acknowledgeable",
                    "This event type cannot be acknowledged",
                    {},
                    409,
                )
            if actual_type not in ACKNOWLEDGEABLE_EVENT_TYPES:
                raise PlatformError(
                    "notification_event_not_acknowledgeable",
                    "This event type cannot be acknowledged",
                    {},
                    409,
                )
            self._assert_recipient_evidence(connection, event_id=event_id, user_id=user_id)
            self._write_ack(connection, event_id=event_id, user_id=user_id, now=now)

    @staticmethod
    def _assert_recipient_evidence(
        connection: Connection,
        *,
        event_id: str,
        user_id: str,
    ) -> None:
        # A suppressed recipient never received a notification: ack is 404.
        suppressed = connection.execute(
            select(notification_suppression_table.c.event_id).where(
                notification_suppression_table.c.event_id == event_id,
                notification_suppression_table.c.recipient_user_id == user_id,
            )
        ).scalar_one_or_none()
        if suppressed is not None:
            raise PlatformError(
                "not_found",
                "The notification was suppressed for this recipient",
                {},
                404,
            )
        # A recipient row alone is valid evidence only when the notification is
        # not yet materialized (ack before materialization).
        recipient = connection.execute(
            select(outbox_recipient_table.c.recipient_user_id).where(
                outbox_recipient_table.c.event_id == event_id,
                outbox_recipient_table.c.recipient_user_id == user_id,
            )
        ).scalar_one_or_none()
        if recipient is not None:
            return
        notification = connection.execute(
            select(notification_table.c.id).where(
                notification_table.c.event_id == event_id,
                notification_table.c.recipient_user_id == user_id,
            )
        ).scalar_one_or_none()
        if notification is not None:
            return
        # Only a *materialized* receipt is evidence; suppressed receipts keep
        # the ack at 404 because no notification was ever delivered.
        receipt = connection.execute(
            select(notification_delivery_receipt_table.c.outcome).where(
                notification_delivery_receipt_table.c.event_id == event_id,
                notification_delivery_receipt_table.c.recipient_user_id == user_id,
            )
        ).scalar_one_or_none()
        if receipt == "materialized":
            return
        raise PlatformError(
            "not_found",
            "No materialized notification evidence exists for this event and user",
            {},
            404,
        )

    @staticmethod
    def _write_ack(
        connection: Connection,
        *,
        event_id: str,
        user_id: str,
        now: datetime,
    ) -> None:
        """Write the first acked_at and fill an empty read_at without overwriting.

        The insert is conflict-do-nothing so concurrent first-acks never race
        on the primary key. When the projection was already retired, only the
        materialized receipt remains as evidence: the ack is a 204 no-op and
        NO ack row is rebuilt.
        """
        projection = connection.execute(
            select(notification_table.c.id).where(
                notification_table.c.event_id == event_id,
                notification_table.c.recipient_user_id == user_id,
            )
        ).scalar_one_or_none()
        if projection is None:
            # No projection: either ack-before-materialization (write the ack
            # so the future notification is born read) or the projection was
            # retired (receipt-only evidence -> 204 no-op, never rebuild).
            receipt = connection.execute(
                select(notification_delivery_receipt_table.c.outcome).where(
                    notification_delivery_receipt_table.c.event_id == event_id,
                    notification_delivery_receipt_table.c.recipient_user_id == user_id,
                )
            ).scalar_one_or_none()
            if receipt == "materialized":
                return
        if connection.dialect.name == "postgresql":
            from sqlalchemy.dialects.postgresql import insert as postgresql_insert

            connection.execute(
                postgresql_insert(notification_context_ack_table)
                .values(
                    event_id=event_id,
                    recipient_user_id=user_id,
                    acked_at_utc=now,
                )
                .on_conflict_do_nothing(index_elements=["event_id", "recipient_user_id"])
            )
        else:
            existing = connection.execute(
                select(notification_context_ack_table.c.acked_at_utc).where(
                    notification_context_ack_table.c.event_id == event_id,
                    notification_context_ack_table.c.recipient_user_id == user_id,
                )
            ).scalar_one_or_none()
            if existing is None:
                connection.execute(
                    notification_context_ack_table.insert().values(
                        event_id=event_id,
                        recipient_user_id=user_id,
                        acked_at_utc=now,
                    )
                )
        connection.execute(
            update(notification_table)
            .where(
                notification_table.c.event_id == event_id,
                notification_table.c.recipient_user_id == user_id,
                notification_table.c.read_at_utc.is_(None),
            )
            .values(read_at_utc=now)
        )
