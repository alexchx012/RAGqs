"""Notification query and read-state service contract."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import select, update

from app.outbox.dispatcher import OutboxDispatcher
from app.outbox.notifications import NotificationMaterializer
from app.outbox.schema import (
    notification_context_ack_table,
    notification_inbox_table,
    notification_table,
)
from app.outbox.service import NotificationService
from app.platform.errors import PlatformError
from tests._support import (
    build_engine,
    build_identity_service,
    fixed_now,
    make_publisher,
    provision_user,
)


def as_utc(value):
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def make_dispatcher(engine, *, now=None):
    materializer = NotificationMaterializer(engine, notification_retention_days=90)
    return OutboxDispatcher(
        engine,
        consumers={"in_app_notification": materializer},
        now=now or (lambda: fixed_now()),
        retention_days=30,
        notification_retention_days=90,
    )


def deliver(engine, *, user_ids, now=None, event_id="evt_1", materialize=True):
    publisher = make_publisher(engine, now=lambda: fixed_now())
    from app.outbox.ports import OutboxPublishCommand, RecipientSelection

    command = OutboxPublishCommand(
        event_id=event_id,
        event_type="ingestion_completed",
        caller_principal="ingestion",
        schema_version=1,
        aggregate_type="ingestion_job",
        aggregate_id=f"job_{event_id}",
        transition_version=1,
        occurred_at=fixed_now(),
        payload={
            "job_id": f"job_{event_id}",
            "document_id": f"doc_{event_id}",
            "document_version_id": f"docv_{event_id}",
            "publication_id": f"pub_{event_id}",
        },
        trace_id="trace_x",
        recipients=tuple(RecipientSelection(recipient_user_id=u) for u in user_ids),
    )
    with engine.begin() as connection:
        publisher.publish(command, connection=connection)
    if not materialize:
        return
    dispatcher = make_dispatcher(engine, now=now)
    claim = dispatcher.claim_one(owner="worker-1")
    assert claim is not None
    dispatcher.run_consumer_and_finalize(claim, owner="worker-1")


def notification_id_of(engine, *, event_id, user_id) -> str:
    with engine.connect() as connection:
        value = connection.execute(
            select(notification_table.c.id).where(
                notification_table.c.event_id == event_id,
                notification_table.c.recipient_user_id == user_id,
            )
        ).scalar_one()
    return str(value)


def make_service(engine, *, now=None):
    return NotificationService(engine, now=now or (lambda: fixed_now()))


def test_list_notifications_returns_retained_records_newest_first() -> None:
    engine = build_engine()
    identity = build_identity_service(engine)
    alice = provision_user(identity, username="alice")
    deliver(engine, user_ids=(alice,), event_id="evt_1")
    deliver(engine, user_ids=(alice,), event_id="evt_2")
    service = make_service(engine)

    items = service.list_notifications(alice, limit=50)

    assert len(items) == 2
    # Newest first: evt_2 materialized after evt_1.
    assert items[0]["payload"]["document_id"] == "doc_evt_2"
    assert items[1]["payload"]["document_id"] == "doc_evt_1"
    # The response contract strips sequence and materialization internals.
    assert "notification_seq" not in items[0]
    assert "materialized_at" not in items[0]
    assert items[0]["read"] is False
    assert items[0]["type"] == "ingestion_completed"


def test_list_notifications_applies_the_limit_and_skips_retired() -> None:
    engine = build_engine()
    identity = build_identity_service(engine)
    alice = provision_user(identity, username="alice")
    deliver(engine, user_ids=(alice,), event_id="evt_1")
    deliver(engine, user_ids=(alice,), event_id="evt_2")
    service = make_service(engine)

    limited = service.list_notifications(alice, limit=1)
    assert len(limited) == 1
    assert limited[0]["payload"]["document_id"] == "doc_evt_2"

    # Retired records disappear from the list.
    with engine.begin() as connection:
        connection.execute(
            update(notification_table)
            .where(notification_table.c.event_id == "evt_1")
            .values(retire_after_at_utc=datetime(2000, 1, 1, tzinfo=UTC))
        )
    remaining = service.list_notifications(alice, limit=50)
    assert len(remaining) == 1
    assert remaining[0]["payload"]["document_id"] == "doc_evt_2"


def test_read_marks_first_write_only_and_repeats_are_204() -> None:
    engine = build_engine()
    identity = build_identity_service(engine)
    alice = provision_user(identity, username="alice")
    deliver(engine, user_ids=(alice,), event_id="evt_1")
    service = make_service(engine, now=lambda: datetime(2026, 8, 6, tzinfo=UTC))
    notification_id = notification_id_of(engine, event_id="evt_1", user_id=alice)

    first = service.mark_read(alice, notification_id)
    second = service.mark_read(alice, notification_id)

    assert first is True
    assert second is True
    with engine.connect() as connection:
        row = (
            connection.execute(
                select(notification_table).where(notification_table.c.id == notification_id)
            )
            .mappings()
            .one()
        )
        assert as_utc(row["read_at_utc"]) == datetime(2026, 8, 6, tzinfo=UTC)


def test_read_rejects_another_users_notification_and_missing_ids() -> None:
    engine = build_engine()
    identity = build_identity_service(engine)
    alice = provision_user(identity, username="alice")
    bob = provision_user(identity, username="bob")
    deliver(engine, user_ids=(alice,), event_id="evt_1")
    service = make_service(engine)
    notification_id = notification_id_of(engine, event_id="evt_1", user_id=alice)

    with pytest.raises(PlatformError) as other:
        service.mark_read(bob, notification_id)
    assert other.value.status_code == 404

    with pytest.raises(PlatformError) as missing:
        service.mark_read(alice, "notification_missing")
    assert missing.value.status_code == 404


def test_read_all_advances_the_watermark_and_keeps_later_notifications_unread() -> None:
    engine = build_engine()
    identity = build_identity_service(engine)
    alice = provision_user(identity, username="alice")
    deliver(engine, user_ids=(alice,), event_id="evt_1")
    deliver(engine, user_ids=(alice,), event_id="evt_2")
    service = make_service(engine)

    service.read_all(alice)
    with engine.connect() as connection:
        inbox = (
            connection.execute(
                select(notification_inbox_table).where(
                    notification_inbox_table.c.recipient_user_id == alice
                )
            )
            .mappings()
            .one()
        )
        assert inbox["read_through_seq"] == 2
        assert inbox["read_all_at_utc"] is not None

    # A later notification stays unread.
    deliver(engine, user_ids=(alice,), event_id="evt_3")
    items = service.list_notifications(alice, limit=50)
    assert len(items) == 3
    assert [item["read"] for item in items] == [False, True, True]

    # Repeat read-all stays a 204 with no watermark regression.
    service.read_all(alice)
    with engine.connect() as connection:
        inbox = (
            connection.execute(
                select(notification_inbox_table).where(
                    notification_inbox_table.c.recipient_user_id == alice
                )
            )
            .mappings()
            .one()
        )
        assert inbox["read_through_seq"] == 3


def test_unread_count_is_computed_in_one_snapshot() -> None:
    engine = build_engine()
    identity = build_identity_service(engine)
    alice = provision_user(identity, username="alice")
    deliver(engine, user_ids=(alice,), event_id="evt_1")
    deliver(engine, user_ids=(alice,), event_id="evt_2")
    service = make_service(engine)
    notification_id = notification_id_of(engine, event_id="evt_1", user_id=alice)

    assert service.unread_count(alice) == 2
    service.mark_read(alice, notification_id)
    assert service.unread_count(alice) == 1
    service.read_all(alice)
    assert service.unread_count(alice) == 0


def test_ack_marks_read_on_the_recipients_notification() -> None:
    engine = build_engine()
    identity = build_identity_service(engine)
    alice = provision_user(identity, username="alice")
    deliver(engine, user_ids=(alice,), event_id="evt_1")
    service = make_service(engine, now=lambda: datetime(2026, 8, 7, tzinfo=UTC))

    service.ack_event(alice, "evt_1")

    with engine.connect() as connection:
        row = (
            connection.execute(
                select(notification_table).where(
                    notification_table.c.event_id == "evt_1",
                    notification_table.c.recipient_user_id == alice,
                )
            )
            .mappings()
            .one()
        )
        assert as_utc(row["read_at_utc"]) == datetime(2026, 8, 7, tzinfo=UTC)


def test_ack_before_materialization_reads_the_future_notification() -> None:
    engine = build_engine()
    identity = build_identity_service(engine)
    alice = provision_user(identity, username="alice")
    # The event exists (published) but has not been materialized yet.
    deliver(engine, user_ids=(alice,), event_id="evt_1", materialize=False)
    service = make_service(engine, now=lambda: datetime(2026, 8, 7, tzinfo=UTC))

    service.ack_event(alice, "evt_1")

    # The ack pre-created the context ack record; materialization is idempotent.
    with engine.connect() as connection:
        ack = (
            connection.execute(
                select(notification_context_ack_table).where(
                    notification_context_ack_table.c.event_id == "evt_1",
                    notification_context_ack_table.c.recipient_user_id == alice,
                )
            )
            .mappings()
            .one()
        )
        assert as_utc(ack["acked_at_utc"]) == datetime(2026, 8, 7, tzinfo=UTC)

    deliver(engine, user_ids=(alice,), event_id="evt_1")
    notification_id = notification_id_of(engine, event_id="evt_1", user_id=alice)
    with engine.connect() as connection:
        row = (
            connection.execute(
                select(notification_table).where(notification_table.c.id == notification_id)
            )
            .mappings()
            .one()
        )
        assert as_utc(row["read_at_utc"]) == datetime(2026, 8, 7, tzinfo=UTC)


def test_ack_errors_follow_the_spec_contract() -> None:
    engine = build_engine()
    identity = build_identity_service(engine)
    alice = provision_user(identity, username="alice")
    bob = provision_user(identity, username="bob")
    deliver(engine, user_ids=(alice,), event_id="evt_1")
    service = make_service(engine)

    with pytest.raises(PlatformError) as wrong_type:
        service.ack_event(alice, "evt_1", event_type="submission_approved")
    assert wrong_type.value.code == "notification_event_not_acknowledgeable"
    assert wrong_type.value.status_code == 409

    with pytest.raises(PlatformError) as not_recipient:
        service.ack_event(bob, "evt_1")
    assert not_recipient.value.status_code == 404

    with pytest.raises(PlatformError) as missing:
        service.ack_event(alice, "evt_missing")
    assert missing.value.status_code == 404


def test_ack_is_naturally_idempotent() -> None:
    engine = build_engine()
    identity = build_identity_service(engine)
    alice = provision_user(identity, username="alice")
    deliver(engine, user_ids=(alice,), event_id="evt_1")
    service = make_service(engine)

    service.ack_event(alice, "evt_1")
    service.ack_event(alice, "evt_1")

    with engine.connect() as connection:
        rows = connection.execute(
            select(notification_context_ack_table).where(
                notification_context_ack_table.c.event_id == "evt_1"
            )
        ).all()
        assert len(rows) == 1
