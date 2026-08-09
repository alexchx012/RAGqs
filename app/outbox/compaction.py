"""Controlled compaction: the single full -> compacted transition.

Compaction preserves the event's opaque identity, fingerprint, occurred time,
compacted_at and each consumer's delivered summary, and deletes every
full-only artifact: payload/trace/schema, recipient rows, delivery rows,
attempt rows and suppressions. Before any deletion, every suppressed recipient
receives its permanent delivery receipt so no terminal outcome is lost.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime

from sqlalchemy import delete, func, select, update
from sqlalchemy.engine import Connection

from app.platform.errors import PlatformError

from .schema import (
    notification_delivery_receipt_table,
    notification_suppression_table,
    outbox_delivery_attempt_table,
    outbox_delivery_table,
    outbox_event_table,
    outbox_recipient_table,
)


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def canonical_receipt_fingerprint(
    event_id: str,
    recipient_user_id: str,
    outcome: str,
    seq: int | None,
) -> str:
    encoded = json.dumps(
        {
            "event_id": event_id,
            "recipient_user_id": recipient_user_id,
            "outcome": outcome,
            "seq": seq,
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(b"outbox-receipt-v1\0" + encoded.encode("utf-8")).hexdigest()


def _write_suppression_receipts(connection: Connection, event_id: str, now: datetime) -> None:
    event = connection.execute(
        select(outbox_event_table.c.occurred_at_utc).where(
            outbox_event_table.c.event_id == event_id
        )
    ).scalar_one_or_none()
    if event is None:
        return
    suppressions = (
        connection.execute(
            select(
                notification_suppression_table.c.recipient_user_id,
                notification_suppression_table.c.reason,
            ).where(notification_suppression_table.c.event_id == event_id)
        )
        .mappings()
        .all()
    )
    for row in suppressions:
        user_id = str(row["recipient_user_id"])
        outcome = (
            "recipient_inactive"
            if row["reason"] == "recipient_inactive"
            else "recipient_unauthorized"
        )
        fingerprint = canonical_receipt_fingerprint(event_id, user_id, outcome, None)
        existing = (
            connection.execute(
                select(
                    notification_delivery_receipt_table.c.outcome,
                    notification_delivery_receipt_table.c.original_notification_seq,
                    notification_delivery_receipt_table.c.fingerprint,
                    notification_delivery_receipt_table.c.occurred_at_utc,
                    notification_delivery_receipt_table.c.materialized_at_utc,
                ).where(
                    notification_delivery_receipt_table.c.event_id == event_id,
                    notification_delivery_receipt_table.c.recipient_user_id == user_id,
                )
            )
            .mappings()
            .one_or_none()
        )
        if existing is not None:
            # An existing receipt must be fully verified before the suppression
            # row is deleted: outcome, fingerprint, the occurred-at fact and
            # the materialized-time fact (a suppression receipt can never
            # carry a materialized time).
            if (
                str(existing["outcome"]) != outcome
                or existing["original_notification_seq"] is not None
                or str(existing["fingerprint"]) != fingerprint
                or _utc(existing["occurred_at_utc"]) != _utc(event)
                or existing["materialized_at_utc"] is not None
            ):
                raise PlatformError(
                    "receipt_fingerprint_mismatch",
                    "Delivery receipt does not match the immutable receipt record",
                    {},
                    409,
                )
            continue
        connection.execute(
            notification_delivery_receipt_table.insert().values(
                event_id=event_id,
                recipient_user_id=user_id,
                outcome=outcome,
                original_notification_seq=None,
                occurred_at_utc=_utc(event),
                materialized_at_utc=None,
                retired_at_utc=now,
                fingerprint=fingerprint,
            )
        )


def compact_event(
    connection: Connection,
    event_id: str,
    now: datetime,
) -> bool:
    """Compress one full event whose every delivery is delivered."""
    non_delivered = connection.execute(
        select(func.count())
        .select_from(outbox_delivery_table)
        .where(
            outbox_delivery_table.c.event_id == event_id,
            outbox_delivery_table.c.status != "delivered",
        )
    ).scalar_one()
    if int(non_delivered) != 0:
        return False
    event = (
        connection.execute(
            select(outbox_event_table.c.compacted_at_utc).where(
                outbox_event_table.c.event_id == event_id
            )
        )
        .mappings()
        .one()
    )
    if event["compacted_at_utc"] is not None:
        return False
    deliveries = (
        connection.execute(
            select(
                outbox_delivery_table.c.consumer_name,
                outbox_delivery_table.c.delivered_at_utc,
                outbox_delivery_table.c.attempt_number,
                outbox_delivery_table.c.replay_generation,
            ).where(outbox_delivery_table.c.event_id == event_id)
        )
        .mappings()
        .all()
    )
    summary = [
        {
            "consumer_name": row["consumer_name"],
            "delivered_at": (
                row["delivered_at_utc"].isoformat() if row["delivered_at_utc"] is not None else None
            ),
            "attempt_number": row["attempt_number"],
            "replay_generation": row["replay_generation"],
        }
        for row in deliveries
    ]
    # Suppressed recipients get their permanent receipt before deletion.
    _write_suppression_receipts(connection, event_id, _utc(now))
    updated = connection.execute(
        update(outbox_event_table)
        .where(
            outbox_event_table.c.event_id == event_id,
            outbox_event_table.c.storage_state == "full",
            outbox_event_table.c.compacted_at_utc.is_(None),
        )
        .values(
            storage_state="compacted",
            payload_json=None,
            trace_id=None,
            schema_version=None,
            compacted_delivery_summary_json=summary,
            compacted_at_utc=now,
        )
    ).rowcount
    if updated != 1:
        return False
    connection.execute(
        delete(outbox_recipient_table).where(outbox_recipient_table.c.event_id == event_id)
    )
    connection.execute(
        delete(outbox_delivery_table).where(outbox_delivery_table.c.event_id == event_id)
    )
    connection.execute(
        delete(outbox_delivery_attempt_table).where(
            outbox_delivery_attempt_table.c.event_id == event_id
        )
    )
    connection.execute(
        delete(notification_suppression_table).where(
            notification_suppression_table.c.event_id == event_id
        )
    )
    return True
