"""Notification materialization for the in_app_notification consumer.

Materialization happens inside the dispatcher's fenced transaction: every
recipient ends in exactly one terminal outcome (notification, suppression or
delivery receipt) and the delivery row is committed in the same transaction.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select, text
from sqlalchemy.engine import Connection, Engine

from app.identity.schema import identity_user_table

from .maintenance import MAX_ONLINE_NOTIFICATIONS, retire_notification_by_id
from .ports import DeliveryMaterialization
from .schema import (
    notification_context_ack_table,
    notification_inbox_table,
    notification_suppression_table,
    notification_table,
    outbox_account_retirement_tombstone_table,
)

DELETED_DOCUMENT_TITLE = "Deleted document"


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class NotificationMaterializer:
    """Materializes one event for one recipient into the notification read model."""

    def __init__(
        self,
        engine: Engine,
        *,
        notification_retention_days: int = 90,
    ) -> None:
        del engine
        self.notification_retention_days = notification_retention_days

    def materialize(
        self,
        connection: Connection,
        *,
        event: dict[str, object],
        recipient: dict[str, object],
        notification_id: str,
        notification_type: str,
        title: str,
        document_id: str | None,
        document_version_id: str | None,
        redacted: bool,
        now: datetime,
    ) -> DeliveryMaterialization | None:
        """Return the materialized notification, or None when suppressed."""
        now = _utc(now)
        # Serialize with read-all and retirement on the same per-user lock;
        # inside the lock the identity lifecycle is re-read so a concurrent
        # retirement can never race a materialization.
        if connection.dialect.name == "postgresql":
            connection.execute(
                text("SELECT pg_advisory_xact_lock(hashtext(:lock))"),
                {"lock": f"ragqs:notifications:read-all:{recipient['recipient_user_id']}"},
            )
        account = (
            connection.execute(
                select(identity_user_table.c.role, identity_user_table.c.lifecycle_status).where(
                    identity_user_table.c.id == recipient["recipient_user_id"]
                )
            )
            .mappings()
            .one_or_none()
        )
        if account is None or account["lifecycle_status"] != "active":
            self._suppress(
                connection,
                event_id=str(event["event_id"]),
                recipient_user_id=str(recipient["recipient_user_id"]),
                reason="recipient_inactive",
                now=now,
            )
            return None
        if (
            recipient["recipient_kind"] == "role_snapshot"
            and recipient["required_role"] is not None
            and account["role"] != recipient["required_role"]
        ):
            self._suppress(
                connection,
                event_id=str(event["event_id"]),
                recipient_user_id=str(recipient["recipient_user_id"]),
                reason="recipient_unauthorized",
                now=now,
            )
            return None
        event_id = str(event["event_id"])
        recipient_user_id = str(recipient["recipient_user_id"])
        existing = connection.execute(
            select(notification_table.c.id).where(
                notification_table.c.event_id == event_id,
                notification_table.c.recipient_user_id == recipient_user_id,
            )
        ).scalar_one_or_none()
        if existing is not None:
            return self._existing_notification(
                connection,
                event_id=event_id,
                recipient_user_id=recipient_user_id,
            )
        # A retired account must never rebuild a notification or reuse a
        # sequence: the permanent retirement tombstone blocks materialization
        # even after the inbox row itself was removed.
        tombstone = connection.execute(
            select(outbox_account_retirement_tombstone_table.c.recipient_user_id).where(
                outbox_account_retirement_tombstone_table.c.recipient_user_id == recipient_user_id
            )
        ).scalar_one_or_none()
        if tombstone is not None:
            self._suppress(
                connection,
                event_id=event_id,
                recipient_user_id=recipient_user_id,
                reason="recipient_inactive",
                now=now,
            )
            return None
        inbox = (
            connection.execute(
                select(
                    notification_inbox_table.c.next_notification_seq,
                    notification_inbox_table.c.read_through_seq,
                    notification_inbox_table.c.version,
                    notification_inbox_table.c.retired,
                ).where(notification_inbox_table.c.recipient_user_id == recipient_user_id)
            )
            .mappings()
            .one_or_none()
        )
        if inbox is not None and bool(inbox["retired"]):
            self._suppress(
                connection,
                event_id=event_id,
                recipient_user_id=recipient_user_id,
                reason="recipient_inactive",
                now=now,
            )
            return None
        return self._materialize_new(
            connection,
            event=event,
            recipient_user_id=recipient_user_id,
            notification_id=notification_id,
            notification_type=notification_type,
            title=title,
            document_id=document_id,
            document_version_id=document_version_id,
            redacted=redacted,
            now=now,
        )

    @staticmethod
    def _existing_notification(
        connection: Connection,
        *,
        event_id: str,
        recipient_user_id: str,
    ) -> DeliveryMaterialization:
        row = (
            connection.execute(
                select(notification_table).where(
                    notification_table.c.event_id == event_id,
                    notification_table.c.recipient_user_id == recipient_user_id,
                )
            )
            .mappings()
            .one()
        )
        return DeliveryMaterialization(
            event_id=event_id,
            recipient_user_id=recipient_user_id,
            notification_id=str(row["id"]),
            notification_type=str(row["notification_type"]),
            title=str(row["title"]),
            payload=dict(row["payload_json"]),
            notification_seq=int(row["notification_seq"]),
            read_at=_utc(row["read_at_utc"]) if row["read_at_utc"] is not None else None,
        )

    def _materialize_new(
        self,
        connection: Connection,
        *,
        event: dict[str, object],
        recipient_user_id: str,
        notification_id: str,
        notification_type: str,
        title: str,
        document_id: str | None,
        document_version_id: str | None,
        redacted: bool,
        now: datetime,
    ) -> DeliveryMaterialization:
        # Serialize with read-all: both lock the recipient inbox so a
        # concurrent read-all either sees this notification or leaves it unread.
        if connection.dialect.name == "postgresql":
            connection.execute(
                text("SELECT pg_advisory_xact_lock(hashtext(:lock))"),
                {"lock": f"ragqs:notifications:read-all:{recipient_user_id}"},
            )
        inbox = (
            connection.execute(
                select(notification_inbox_table).where(
                    notification_inbox_table.c.recipient_user_id == recipient_user_id
                )
            )
            .mappings()
            .one_or_none()
        )
        ack = connection.execute(
            select(notification_context_ack_table.c.acked_at_utc).where(
                notification_context_ack_table.c.event_id == event["event_id"],
                notification_context_ack_table.c.recipient_user_id == recipient_user_id,
            )
        ).scalar_one_or_none()
        acked_at = _utc(ack) if ack is not None else None
        if inbox is None:
            connection.execute(
                notification_inbox_table.insert().values(
                    recipient_user_id=recipient_user_id,
                    next_notification_seq=2,
                    read_through_seq=0,
                    read_all_at_utc=None,
                    version=1,
                    retired=False,
                )
            )
            notification_seq = 1
        else:
            notification_seq = int(inbox["next_notification_seq"])
            connection.execute(
                notification_inbox_table.update()
                .where(notification_inbox_table.c.recipient_user_id == recipient_user_id)
                .values(
                    next_notification_seq=notification_seq + 1,
                    version=int(inbox["version"]) + 1,
                )
            )

        occurred_at = event.get("occurred_at_utc")
        event_occurred_at = _utc(occurred_at) if isinstance(occurred_at, datetime) else _utc(now)
        if redacted:
            # Deleted-document projections keep only opaque identifiers and
            # never restore filename/title/snippet or free text.
            payload: dict[str, object] = {}
        else:
            raw_payload = event.get("payload_json")
            payload = dict(raw_payload) if isinstance(raw_payload, dict) else {}
        connection.execute(
            notification_table.insert().values(
                id=notification_id,
                event_id=event["event_id"],
                recipient_user_id=recipient_user_id,
                notification_type=notification_type,
                title=title,
                payload_json=payload,
                document_id=document_id,
                document_version_id=document_version_id,
                event_occurred_at_utc=event_occurred_at,
                materialized_at_utc=now,
                notification_seq=notification_seq,
                read_at_utc=acked_at,
                retire_after_at_utc=now + timedelta(days=self.notification_retention_days),
                redacted=redacted,
            )
        )
        # 50 条上限在物化事务内生效：此时本事务已持有 inbox 行更新（PG 路径还
        # 持有 per-user advisory lock），按 seq 倒序排名超过上限的未到期通知
        # 原子退休，未读计数因此永远受上限约束；后台 retire 任务保留为存量兜底。
        self._trim_over_cap(connection, recipient_user_id=recipient_user_id, now=now)
        return DeliveryMaterialization(
            event_id=str(event["event_id"]),
            recipient_user_id=recipient_user_id,
            notification_id=notification_id,
            notification_type=notification_type,
            title=title,
            payload=payload,
            notification_seq=notification_seq,
            read_at=acked_at,
        )

    @staticmethod
    def _trim_over_cap(
        connection: Connection,
        *,
        recipient_user_id: str,
        now: datetime,
    ) -> None:
        """Retire every unexpired notification ranked above the online cap."""
        over = (
            connection.execute(
                select(notification_table.c.id)
                .where(
                    notification_table.c.recipient_user_id == recipient_user_id,
                    notification_table.c.retire_after_at_utc > now,
                )
                .order_by(notification_table.c.notification_seq.desc())
                .offset(MAX_ONLINE_NOTIFICATIONS)
            )
            .scalars()
            .all()
        )
        for notification_id in over:
            retire_notification_by_id(connection, str(notification_id), now)

    @staticmethod
    def _suppress(
        connection: Connection,
        *,
        event_id: str,
        recipient_user_id: str,
        reason: str,
        now: datetime,
    ) -> None:
        connection.execute(
            notification_suppression_table.insert().values(
                event_id=event_id,
                recipient_user_id=recipient_user_id,
                reason=reason,
                suppressed_at_utc=now,
            )
        )
