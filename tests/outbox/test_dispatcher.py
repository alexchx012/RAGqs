"""Dispatcher claim, delivery, retry, lease and fencing contract."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from _helpers import (
    build_engine,
    build_identity_service,
    cap,
    fixed_now,
    make_publisher,
    provision_user,
)
from sqlalchemy import select, update

from app.graph.outbox import SqlAlchemyGraphBuildOutboxAdapter
from app.identity.schema import identity_user_table
from app.outbox.dispatcher import OutboxDispatcher, RetryPolicy
from app.outbox.notifications import NotificationMaterializer
from app.outbox.publisher import SqlAlchemyOutboxPublisher
from app.outbox.schema import (
    notification_delivery_receipt_table,
    notification_suppression_table,
    notification_table,
    outbox_delivery_attempt_table,
    outbox_delivery_table,
)
from app.platform.persistence import FenceViolation


def publish(engine, publisher: SqlAlchemyOutboxPublisher, *, user_ids: tuple[str, ...], **kwargs):
    from app.outbox.ports import OutboxPublishCommand, RecipientSelection

    event_id = kwargs.pop("event_id", "evt_1")
    aggregate_id = kwargs.pop("aggregate_id", "job_1")
    payload = kwargs.pop(
        "payload",
        {
            "job_id": "job_1",
            "document_id": "doc_1",
            "document_version_id": "docv_1",
            "publication_id": "pub_1",
        },
    )
    event_type = kwargs.pop("event_type", "ingestion_completed")
    command = OutboxPublishCommand(
        event_id=event_id,
        event_type=event_type,
        caller_principal="ingestion",
        capability=cap("ingestion"),
        schema_version=1,
        aggregate_type="ingestion_job",
        aggregate_id=aggregate_id,
        transition_version=1,
        occurred_at=fixed_now(),
        payload=payload,
        trace_id="trace_x",
        recipients=tuple(
            RecipientSelection(recipient_user_id=user_id, selection_reason="direct_operator")
            for user_id in user_ids
        ),
    )
    with engine.begin() as connection:
        return publisher.publish(command, connection=connection)


def make_dispatcher(engine, *, now=None, notification_retention_days=90, metrics=None):
    materializer = NotificationMaterializer(
        engine,
        notification_retention_days=notification_retention_days,
    )
    return OutboxDispatcher(
        engine,
        consumers={"in_app_notification": materializer},
        now=now or (lambda: fixed_now()),
        retention_days=30,
        notification_retention_days=notification_retention_days,
        metrics=metrics,
    )


class _MutableClock:
    def __init__(self, start: datetime) -> None:
        self._current = start

    def now(self) -> datetime:
        return self._current

    def advance(self, seconds: int) -> None:
        self._current = self._current + timedelta(seconds=seconds)


def as_utc(value):
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def test_claim_transitions_pending_to_running_and_writes_attempt() -> None:
    engine = build_engine()
    identity = build_identity_service(engine)
    alice = provision_user(identity, username="alice")
    publisher = make_publisher(engine, now=lambda: fixed_now())
    publish(engine, publisher, user_ids=(alice,))
    dispatcher = make_dispatcher(engine)

    claim = dispatcher.claim_one(owner="worker-1")

    assert claim is not None
    assert claim.event_id == "evt_1"
    assert claim.consumer_name == "in_app_notification"
    assert claim.fence_token == 1
    assert claim.attempt_number == 1
    assert claim.cycle_attempt_number == 1
    with engine.connect() as connection:
        delivery = (
            connection.execute(
                select(outbox_delivery_table).where(outbox_delivery_table.c.event_id == "evt_1")
            )
            .mappings()
            .one()
        )
        assert delivery["status"] == "running"
        assert delivery["version"] == 2
        assert delivery["attempt_number"] == 1
        assert delivery["lease_owner"] == "worker-1"
        assert delivery["fence_token"] == 1
        assert delivery["next_attempt_at_utc"] is None
        attempts = connection.execute(select(outbox_delivery_attempt_table)).mappings().all()
        assert len(attempts) == 1
        assert attempts[0]["status"] == "running"
        assert attempts[0]["fence_token"] == 1


def test_claim_does_not_double_claim_a_running_delivery() -> None:
    engine = build_engine()
    identity = build_identity_service(engine)
    alice = provision_user(identity, username="alice")
    publisher = make_publisher(engine, now=lambda: fixed_now())
    publish(engine, publisher, user_ids=(alice,))
    dispatcher = make_dispatcher(engine)

    first = dispatcher.claim_one(owner="worker-1")
    second = dispatcher.claim_one(owner="worker-2")

    assert first is not None
    assert second is None


def test_delivered_finalize_materializes_notification_and_fences_writes() -> None:
    engine = build_engine()
    identity = build_identity_service(engine)
    alice = provision_user(identity, username="alice")
    publisher = make_publisher(engine, now=lambda: fixed_now())
    publish(engine, publisher, user_ids=(alice,))
    dispatcher = make_dispatcher(engine, now=lambda: datetime(2026, 8, 5, 12, 0, 1, tzinfo=UTC))

    claim = dispatcher.claim_one(owner="worker-1")
    assert claim is not None
    outcome = dispatcher.run_consumer_and_finalize(claim, owner="worker-1")

    assert outcome.status == "delivered"
    with engine.connect() as connection:
        delivery = (
            connection.execute(
                select(outbox_delivery_table).where(outbox_delivery_table.c.event_id == "evt_1")
            )
            .mappings()
            .one()
        )
        assert delivery["status"] == "delivered"
        assert delivery["delivered_at_utc"] is not None
        assert delivery["lease_owner"] is None
        notification = (
            connection.execute(
                select(notification_table).where(notification_table.c.recipient_user_id == alice)
            )
            .mappings()
            .one()
        )
        assert notification["notification_seq"] == 1
        assert notification["notification_type"] == "ingestion_completed"
        assert notification["title"] == "Document ingestion completed"
        assert notification["payload_json"]["document_id"] == "doc_1"
        assert notification["read_at_utc"] is None
        assert as_utc(notification["retire_after_at_utc"]) == datetime(
            2026, 11, 3, 12, 0, 1, tzinfo=UTC
        )
        attempt = (
            connection.execute(
                select(outbox_delivery_attempt_table).where(
                    outbox_delivery_attempt_table.c.delivery_attempt_id == claim.attempt_id
                )
            )
            .mappings()
            .one()
        )
        assert attempt["status"] == "delivered"


def test_duplicate_delivery_does_not_create_a_duplicate_notification() -> None:
    engine = build_engine()
    identity = build_identity_service(engine)
    alice = provision_user(identity, username="alice")
    publisher = make_publisher(engine, now=lambda: fixed_now())
    publish(engine, publisher, user_ids=(alice,))
    dispatcher = make_dispatcher(engine)

    first = dispatcher.claim_one(owner="worker-1")
    assert first is not None
    dispatcher.run_consumer_and_finalize(first, owner="worker-1")

    # A stale runner replays the same claim: fence no longer current.
    with pytest.raises(FenceViolation):
        dispatcher.run_consumer_and_finalize(first, owner="worker-1")

    with engine.connect() as connection:
        assert connection.execute(select(notification_table)).all() == [
            connection.execute(select(notification_table)).all()[0]
        ]


def test_inactive_recipient_is_suppressed_without_a_notification() -> None:
    engine = build_engine()
    identity = build_identity_service(engine)
    alice = provision_user(identity, username="alice")
    publisher = make_publisher(engine, now=lambda: fixed_now())
    publish(engine, publisher, user_ids=(alice,))
    with engine.begin() as connection:
        connection.execute(
            update(
                __import__(
                    "app.identity.schema", fromlist=["identity_user_table"]
                ).identity_user_table
            )
            .where(
                __import__(
                    "app.identity.schema", fromlist=["identity_user_table"]
                ).identity_user_table.c.id
                == alice
            )
            .values(lifecycle_status="pending_delete")
        )
    dispatcher = make_dispatcher(engine)

    claim = dispatcher.claim_one(owner="worker-1")
    assert claim is not None
    dispatcher.run_consumer_and_finalize(claim, owner="worker-1")

    with engine.connect() as connection:
        from app.outbox.schema import notification_suppression_table

        suppression = (
            connection.execute(
                select(notification_suppression_table).where(
                    notification_suppression_table.c.event_id == "evt_1"
                )
            )
            .mappings()
            .one()
        )
        assert suppression["reason"] == "recipient_inactive"
        assert connection.execute(select(notification_table)).all() == []


def test_graph_event_for_existing_inactive_initiator_is_suppressed_and_receipted() -> None:
    engine = build_engine()
    identity = build_identity_service(engine)
    alice = provision_user(identity, username="alice")
    clock = _MutableClock(fixed_now())
    publisher = make_publisher(engine, now=clock.now)
    with engine.begin() as connection:
        connection.execute(
            update(identity_user_table)
            .where(identity_user_table.c.id == alice)
            .values(lifecycle_status="pending_delete")
        )
    adapter = SqlAlchemyGraphBuildOutboxAdapter(publisher)

    with engine.begin() as connection:
        event_id = adapter.publish_completed(
            graph_build_id="graph_1",
            status="failed",
            source_revision=1,
            transition_version=1,
            occurred_at=fixed_now(),
            recipient_user_id=alice,
            failure_class="index_error",
            connection=connection,
        )

    assert event_id == "evt_graph_build_completed_graph_1_1"
    dispatcher = make_dispatcher(engine, now=clock.now)
    claim = dispatcher.claim_one(owner="worker-1")
    assert claim is not None
    dispatcher.run_consumer_and_finalize(claim, owner="worker-1")
    with engine.connect() as connection:
        suppression = (
            connection.execute(
                select(notification_suppression_table).where(
                    notification_suppression_table.c.event_id == event_id
                )
            )
            .mappings()
            .one()
        )
        assert suppression["reason"] == "recipient_inactive"

    clock.advance(seconds=31 * 24 * 60 * 60)
    assert dispatcher.compact_due_events() == 1
    with engine.connect() as connection:
        receipt = (
            connection.execute(
                select(notification_delivery_receipt_table).where(
                    notification_delivery_receipt_table.c.event_id == event_id
                )
            )
            .mappings()
            .one()
        )
        assert receipt["outcome"] == "recipient_inactive"


def test_retryable_failure_schedules_retry_wait_with_jittered_delay() -> None:
    engine = build_engine()
    identity = build_identity_service(engine)
    alice = provision_user(identity, username="alice")
    publisher = make_publisher(engine, now=lambda: fixed_now())
    publish(engine, publisher, user_ids=(alice,))
    dispatcher = make_dispatcher(engine, now=lambda: datetime(2026, 8, 5, 12, 0, 2, tzinfo=UTC))

    claim = dispatcher.claim_one(owner="worker-1")
    assert claim is not None
    outcome = dispatcher.fail_and_schedule(
        claim,
        owner="worker-1",
        error_category="retryable",
        error_code="connection_error",
    )

    assert outcome.status == "failed"
    with engine.connect() as connection:
        delivery = (
            connection.execute(
                select(outbox_delivery_table).where(outbox_delivery_table.c.event_id == "evt_1")
            )
            .mappings()
            .one()
        )
        assert delivery["status"] == "retry_wait"
        assert delivery["error_category"] == "retryable"
        assert delivery["error_code"] == "connection_error"
        assert delivery["lease_owner"] is None
        next_at = as_utc(delivery["next_attempt_at_utc"])
        expected = datetime(2026, 8, 5, 12, 0, 2, tzinfo=UTC) + timedelta(seconds=5)
        assert abs((next_at - expected).total_seconds()) <= 1.0  # 5s +-20%


def test_eighth_attempt_dead_letters_the_delivery() -> None:
    engine = build_engine()
    identity = build_identity_service(engine)
    alice = provision_user(identity, username="alice")
    publisher = make_publisher(engine, now=lambda: fixed_now())
    publish(engine, publisher, user_ids=(alice,))
    clock = _MutableClock(datetime(2026, 8, 5, 12, 0, 3, tzinfo=UTC))
    dispatcher = make_dispatcher(engine, now=clock.now)
    policy = RetryPolicy()

    claim = dispatcher.claim_one(owner="worker-1")
    assert claim is not None
    # Cycle attempts 1..7 fail retryably, the 8th attempt dead-letters.
    for _ in range(7):
        outcome = dispatcher.fail_and_schedule(
            claim,
            owner="worker-1",
            error_category="retryable",
            error_code="connection_error",
        )
        assert outcome.status == "failed"
        clock.advance(seconds=100000)  # any backoff elapses
        claim = dispatcher.claim_one(owner="worker-1")
        assert claim is not None
    final = dispatcher.fail_and_schedule(
        claim,
        owner="worker-1",
        error_category="retryable",
        error_code="connection_error",
    )

    assert final.status == "dead_letter"
    assert policy.delays == (5, 30, 120, 600, 1800, 7200, 21600)
    with engine.connect() as connection:
        delivery = (
            connection.execute(
                select(outbox_delivery_table).where(outbox_delivery_table.c.event_id == "evt_1")
            )
            .mappings()
            .one()
        )
        assert delivery["status"] == "dead_letter"
        assert delivery["attempt_number"] == 8
        assert delivery["next_attempt_at_utc"] is None


def test_expired_lease_is_recycled_and_consumes_an_attempt() -> None:
    engine = build_engine()
    identity = build_identity_service(engine)
    alice = provision_user(identity, username="alice")
    publisher = make_publisher(engine, now=lambda: fixed_now())
    publish(engine, publisher, user_ids=(alice,))
    clock = _MutableClock(datetime(2026, 8, 5, 12, 0, 4, tzinfo=UTC))
    dispatcher = make_dispatcher(engine, now=clock.now)

    claim = dispatcher.claim_one(owner="worker-1")
    assert claim is not None
    clock.advance(seconds=120)  # lease (60s) expires
    recycled = dispatcher.recycle_expired_running()

    assert recycled == 1
    with engine.connect() as connection:
        delivery = (
            connection.execute(
                select(outbox_delivery_table).where(outbox_delivery_table.c.event_id == "evt_1")
            )
            .mappings()
            .one()
        )
        assert delivery["status"] == "retry_wait"
        assert delivery["lease_owner"] is None
        assert delivery["attempt_number"] == 1
        attempt = (
            connection.execute(
                select(outbox_delivery_attempt_table).where(
                    outbox_delivery_attempt_table.c.delivery_attempt_id == claim.attempt_id
                )
            )
            .mappings()
            .one()
        )
        assert attempt["status"] == "expired"
    clock.advance(seconds=10)  # backoff elapses
    second = dispatcher.claim_one(owner="worker-2")
    assert second is not None
    assert second.attempt_number == 2
    assert second.cycle_attempt_number == 2


def test_recycle_expired_running_respects_limit() -> None:
    engine = build_engine()
    identity = build_identity_service(engine)
    alice = provision_user(identity, username="alice")
    publisher = make_publisher(engine, now=lambda: fixed_now())
    for index in range(3):
        publish(
            engine,
            publisher,
            user_ids=(alice,),
            event_id=f"evt_{index}",
            aggregate_id=f"job_{index}",
            payload={
                "job_id": f"job_{index}",
                "document_id": f"doc_{index}",
                "document_version_id": f"docv_{index}",
                "publication_id": f"pub_{index}",
            },
        )
    clock = _MutableClock(datetime(2026, 8, 5, 12, 0, 4, tzinfo=UTC))
    dispatcher = make_dispatcher(engine, now=clock.now)

    claims = [dispatcher.claim_one(owner=f"worker-{index}") for index in range(3)]
    assert all(claim is not None for claim in claims)
    clock.advance(seconds=120)

    assert dispatcher.recycle_expired_running(limit=2) == 2
    with engine.connect() as connection:
        statuses = connection.execute(select(outbox_delivery_table.c.status)).scalars().all()
    assert statuses.count("retry_wait") == 2
    assert statuses.count("running") == 1
    assert dispatcher.recycle_expired_running(limit=2) == 1


def test_renew_extends_the_lease_with_the_same_fence() -> None:
    engine = build_engine()
    identity = build_identity_service(engine)
    alice = provision_user(identity, username="alice")
    publisher = make_publisher(engine, now=lambda: fixed_now())
    publish(engine, publisher, user_ids=(alice,))
    dispatcher = make_dispatcher(engine, now=lambda: datetime(2026, 8, 5, 12, 0, 5, tzinfo=UTC))

    claim = dispatcher.claim_one(owner="worker-1")
    assert claim is not None
    renewed = dispatcher.renew_lease(claim, owner="worker-1")

    assert renewed is not None
    assert renewed.fence_token == claim.fence_token
    with engine.connect() as connection:
        delivery = (
            connection.execute(
                select(outbox_delivery_table).where(outbox_delivery_table.c.event_id == "evt_1")
            )
            .mappings()
            .one()
        )
        assert as_utc(delivery["lease_expires_at_utc"]) == datetime(
            2026, 8, 5, 12, 1, 5, tzinfo=UTC
        )


def test_stale_fence_finalize_rolls_back_notification_writes() -> None:
    engine = build_engine()
    identity = build_identity_service(engine)
    alice = provision_user(identity, username="alice")
    publisher = make_publisher(engine, now=lambda: fixed_now())
    publish(engine, publisher, user_ids=(alice,))
    dispatcher = make_dispatcher(engine)

    claim = dispatcher.claim_one(owner="worker-1")
    assert claim is not None
    # Another worker steals the delivery by taking over the lease.
    with engine.begin() as connection:
        connection.execute(
            update(outbox_delivery_table)
            .where(outbox_delivery_table.c.event_id == "evt_1")
            .values(
                lease_owner="worker-2",
                fence_token=2,
                lease_expires_at_utc=datetime(2099, 1, 1, tzinfo=UTC),
            )
        )

    with pytest.raises(FenceViolation):
        dispatcher.run_consumer_and_finalize(claim, owner="worker-1")

    with engine.connect() as connection:
        assert connection.execute(select(notification_table)).all() == []
