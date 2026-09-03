"""Ops delivery view, replay and compaction contract."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from app.outbox.dispatcher import OutboxDispatcher
from app.outbox.notifications import NotificationMaterializer
from app.outbox.publisher import SqlAlchemyOutboxPublisher, SqlAlchemyPublicGraphSourceOutboxAdapter
from app.outbox.schema import (
    notification_table,
    outbox_delivery_attempt_table,
    outbox_delivery_table,
    outbox_event_table,
    outbox_recipient_table,
)
from app.platform.errors import PlatformError
from tests._support import (
    build_engine,
    build_identity_service,
    fixed_now,
    make_publisher,
    provision_user,
)


class _MutableClock:
    def __init__(self, start: datetime) -> None:
        self._current = start

    def now(self) -> datetime:
        return self._current

    def advance(self, seconds: int) -> None:
        self._current = self._current + timedelta(seconds=seconds)


def make_dispatcher(engine, *, now=None, retention_days=30):
    materializer = NotificationMaterializer(engine, notification_retention_days=90)
    return OutboxDispatcher(
        engine,
        consumers={"in_app_notification": materializer},
        now=now or (lambda: fixed_now()),
        retention_days=retention_days,
        notification_retention_days=90,
    )


def publish(engine, publisher: SqlAlchemyOutboxPublisher, *, user_ids, event_id="evt_1"):
    from app.outbox.ports import OutboxPublishCommand, RecipientSelection

    command = OutboxPublishCommand(
        event_id=event_id,
        event_type="ingestion_completed",
        caller_principal="ingestion",
        schema_version=1,
        aggregate_type="ingestion_job",
        aggregate_id="job_1",
        transition_version=1,
        occurred_at=fixed_now(),
        payload={
            "job_id": "job_1",
            "document_id": "doc_1",
            "document_version_id": "docv_1",
            "publication_id": "pub_1",
        },
        trace_id="trace_x",
        recipients=tuple(RecipientSelection(recipient_user_id=user_id) for user_id in user_ids),
    )
    with engine.begin() as connection:
        publisher.publish(command, connection=connection)


def dead_letter(engine, dispatcher, *, owner="worker-1"):
    claim = dispatcher.claim_one(owner=owner)
    assert claim is not None
    outcome = dispatcher.fail_and_schedule(
        claim,
        owner=owner,
        error_category="permanent",
        error_code="unsupported_schema",
    )
    assert outcome.status == "dead_letter"


def test_ops_view_reports_status_version_and_replayability() -> None:
    engine = build_engine()
    identity = build_identity_service(engine)
    alice = provision_user(identity, username="alice")
    publisher = make_publisher(engine, now=lambda: fixed_now())
    publish(engine, publisher, user_ids=(alice,))
    dispatcher = make_dispatcher(engine)

    pending = dispatcher.ops_view("evt_1", consumer_name="in_app_notification")
    assert pending is not None
    assert pending.status == "pending"
    assert pending.version == 1
    assert pending.replay_generation == 1
    assert pending.replayable is False

    dead_letter(engine, dispatcher)

    dead = dispatcher.ops_view("evt_1", consumer_name="in_app_notification")
    assert dead is not None
    assert dead.status == "dead_letter"
    assert dead.replayable is True
    assert dead.error_code == "unsupported_schema"


def test_ops_view_is_ops_safe_and_hides_payload_and_recipients() -> None:
    engine = build_engine()
    identity = build_identity_service(engine)
    alice = provision_user(identity, username="alice")
    publisher = make_publisher(engine, now=lambda: fixed_now())
    publish(engine, publisher, user_ids=(alice,))
    dispatcher = make_dispatcher(engine)

    view = dispatcher.ops_view("evt_1", consumer_name="in_app_notification")
    assert view is not None
    assert not hasattr(view, "payload")
    assert not hasattr(view, "recipients")


def test_replay_resets_dead_letter_into_a_fresh_pending_cycle() -> None:
    engine = build_engine()
    identity = build_identity_service(engine)
    alice = provision_user(identity, username="alice")
    publisher = make_publisher(engine, now=lambda: fixed_now())
    publish(engine, publisher, user_ids=(alice,))
    dispatcher = make_dispatcher(engine)
    dead_letter(engine, dispatcher)

    receipt = dispatcher.replay(
        "evt_1",
        consumer_name="in_app_notification",
        expected_version=2,
        idempotency_key="replay-1",
        request_hash="hash-1",
    )

    assert receipt.status == "pending"
    assert receipt.replay_generation == 2
    assert receipt.version == 3
    with engine.connect() as connection:
        delivery = (
            connection.execute(
                select(outbox_delivery_table).where(outbox_delivery_table.c.event_id == "evt_1")
            )
            .mappings()
            .one()
        )
        assert delivery["status"] == "pending"
        assert delivery["replay_generation"] == 2
        assert delivery["version"] == 3
        assert delivery["attempt_number"] == 1
        assert delivery["error_category"] is None
        assert delivery["lease_owner"] is None

    # The new cycle has a fresh 8-attempt budget and no duplicate notification.
    clock = _MutableClock(datetime(2026, 8, 5, 12, 1, 0, tzinfo=UTC))
    replay_dispatcher = make_dispatcher(engine, now=clock.now)
    claim = replay_dispatcher.claim_one(owner="worker-2")
    assert claim is not None
    assert claim.replay_generation == 2
    assert claim.attempt_number == 2
    assert claim.fence_token == 2
    replay_dispatcher.run_consumer_and_finalize(claim, owner="worker-2")
    with engine.connect() as connection:
        assert len(connection.execute(select(notification_table)).all()) == 1


def test_replay_is_idempotent_for_the_same_key_and_request() -> None:
    engine = build_engine()
    identity = build_identity_service(engine)
    alice = provision_user(identity, username="alice")
    publisher = make_publisher(engine, now=lambda: fixed_now())
    publish(engine, publisher, user_ids=(alice,))
    dispatcher = make_dispatcher(engine)
    dead_letter(engine, dispatcher)

    first = dispatcher.replay(
        "evt_1",
        consumer_name="in_app_notification",
        expected_version=2,
        idempotency_key="replay-same",
        request_hash="hash-same",
    )
    second = dispatcher.replay(
        "evt_1",
        consumer_name="in_app_notification",
        expected_version=2,
        idempotency_key="replay-same",
        request_hash="hash-same",
    )

    assert first.version == 3
    assert second.version == 3
    assert first.replay_generation == second.replay_generation


def test_replay_errors_follow_the_spec_contract() -> None:
    engine = build_engine()
    identity = build_identity_service(engine)
    alice = provision_user(identity, username="alice")
    publisher = make_publisher(engine, now=lambda: fixed_now())
    publish(engine, publisher, user_ids=(alice,))
    dispatcher = make_dispatcher(engine)
    dead_letter(engine, dispatcher)

    with pytest.raises(PlatformError) as missing:
        dispatcher.replay(
            "evt_missing",
            consumer_name="in_app_notification",
            expected_version=1,
            idempotency_key="k1",
            request_hash="h1",
        )
    assert missing.value.status_code == 404

    with pytest.raises(PlatformError) as wrong_key:
        dispatcher.replay(
            "evt_1",
            consumer_name="in_app_notification",
            expected_version=1,
            idempotency_key="k2",
            request_hash="h2",
        )
    assert wrong_key.value.code == "version_conflict"
    assert wrong_key.value.status_code == 409

    # Same key, different request: the first succeeds, the second conflicts.
    dispatcher.replay(
        "evt_1",
        consumer_name="in_app_notification",
        expected_version=2,
        idempotency_key="k3",
        request_hash="h3",
    )
    with pytest.raises(PlatformError) as conflicting:
        dispatcher.replay(
            "evt_1",
            consumer_name="in_app_notification",
            expected_version=2,
            idempotency_key="k3",
            request_hash="different-hash",
        )
    assert conflicting.value.code == "idempotency_key_conflict"
    assert conflicting.value.status_code == 409


def test_non_dead_letter_delivery_is_not_replayable() -> None:
    engine = build_engine()
    identity = build_identity_service(engine)
    alice = provision_user(identity, username="alice")
    publisher = make_publisher(engine, now=lambda: fixed_now())
    publish(engine, publisher, user_ids=(alice,))
    dispatcher = make_dispatcher(engine)

    with pytest.raises(PlatformError) as raised:
        dispatcher.replay(
            "evt_1",
            consumer_name="in_app_notification",
            expected_version=1,
            idempotency_key="k4",
            request_hash="h4",
        )
    assert raised.value.code == "outbox_delivery_not_replayable"


def test_compaction_only_after_every_delivery_is_delivered_and_retention_elapsed() -> None:
    engine = build_engine()
    identity = build_identity_service(engine)
    alice = provision_user(identity, username="alice")
    publisher = make_publisher(engine, now=lambda: fixed_now())
    publish(engine, publisher, user_ids=(alice,))
    clock = _MutableClock(datetime(2026, 8, 5, 12, 0, 0, tzinfo=UTC))
    dispatcher = make_dispatcher(engine, now=clock.now, retention_days=30)

    # Delivery is still pending: nothing compacts even long after retention.
    clock.advance(seconds=100000)
    assert dispatcher.compact_due_events() == 0

    claim = dispatcher.claim_one(owner="worker-1")
    assert claim is not None
    dispatcher.run_consumer_and_finalize(claim, owner="worker-1")

    # compact_after = delivered_at + 30 days; not yet due.
    assert dispatcher.compact_due_events() == 0

    clock.advance(seconds=31 * 24 * 3600)
    assert dispatcher.compact_due_events() == 1

    with engine.connect() as connection:
        event = (
            connection.execute(
                select(outbox_event_table).where(outbox_event_table.c.event_id == "evt_1")
            )
            .mappings()
            .one()
        )
        assert event["storage_state"] == "compacted"
        assert event["payload_json"] is None
        assert event["trace_id"] is None
        assert event["schema_version"] is None
        assert event["compact_after_at_utc"] is None
        assert event["compacted_at_utc"] is not None
        assert event["payload_fingerprint"] is not None
        summary = event["compacted_delivery_summary_json"]
        assert len(summary) == 1
        assert summary[0]["consumer_name"] == "in_app_notification"
        assert summary[0]["status"] == "delivered"
        assert summary[0]["version"] == 2
        assert summary[0]["delivered_at"] is not None
        assert connection.execute(select(outbox_recipient_table)).all() == []
        assert connection.execute(select(outbox_delivery_attempt_table)).all() == []


def test_zero_recipient_outbox_event_compacts_after_retention() -> None:
    engine = build_engine()
    clock = _MutableClock(fixed_now())
    publisher = make_publisher(engine, now=clock.now)
    adapter = SqlAlchemyPublicGraphSourceOutboxAdapter(publisher)
    with engine.begin() as connection:
        event_id = adapter.publish_public_graph_source_change(
            source_revision=1,
            source_manifest_id="manifest_1",
            source_manifest_hash="hash_1",
            document_id="doc_1",
            change_type="publish",
            occurred_at=clock.now(),
            connection=connection,
        )
    dispatcher = make_dispatcher(engine, now=clock.now, retention_days=30)

    clock.advance(seconds=31 * 24 * 60 * 60)
    assert dispatcher.compact_due_events() == 1

    with engine.connect() as connection:
        event = (
            connection.execute(
                select(outbox_event_table).where(outbox_event_table.c.event_id == event_id)
            )
            .mappings()
            .one()
        )
        assert event["storage_state"] == "compacted"
        assert event["payload_json"] is None
        assert event["compacted_delivery_summary_json"] == []


def test_dead_letter_events_never_compact() -> None:
    engine = build_engine()
    identity = build_identity_service(engine)
    alice = provision_user(identity, username="alice")
    publisher = make_publisher(engine, now=lambda: fixed_now())
    publish(engine, publisher, user_ids=(alice,))
    clock = _MutableClock(datetime(2026, 8, 5, 12, 0, 0, tzinfo=UTC))
    dispatcher = make_dispatcher(engine, now=clock.now, retention_days=30)
    dead_letter(engine, dispatcher)

    clock.advance(seconds=400 * 24 * 3600)
    assert dispatcher.compact_due_events() == 0
    with engine.connect() as connection:
        event = connection.execute(
            select(outbox_event_table.c.storage_state).where(
                outbox_event_table.c.event_id == "evt_1"
            )
        ).scalar_one()
        assert event == "full"


def test_replay_then_delivered_then_compaction_uses_new_retention_start() -> None:
    engine = build_engine()
    identity = build_identity_service(engine)
    alice = provision_user(identity, username="alice")
    publisher = make_publisher(engine, now=lambda: fixed_now())
    publish(engine, publisher, user_ids=(alice,))
    clock = _MutableClock(datetime(2026, 8, 5, 12, 0, 0, tzinfo=UTC))
    dispatcher = make_dispatcher(engine, now=clock.now, retention_days=30)
    dead_letter(engine, dispatcher)

    clock.advance(seconds=100000)
    dispatcher.replay(
        "evt_1",
        consumer_name="in_app_notification",
        expected_version=2,
        idempotency_key="replay-k",
        request_hash="replay-h",
    )
    claim = dispatcher.claim_one(owner="worker-1")
    assert claim is not None
    dispatcher.run_consumer_and_finalize(claim, owner="worker-1")

    # Not due yet: the 30-day clock starts from the replayed delivery.
    clock.advance(seconds=29 * 24 * 3600)
    assert dispatcher.compact_due_events() == 0
    clock.advance(seconds=2 * 24 * 3600)
    assert dispatcher.compact_due_events() == 1


def test_compact_due_events_is_bounded_per_round_and_converges() -> None:
    """#14：单轮 compact 至多领取固定批量（100），积压在多轮内收敛；
    单事务时长有界。"""
    engine = build_engine()
    clock = _MutableClock(fixed_now())
    publisher = make_publisher(engine, now=clock.now)
    adapter = SqlAlchemyPublicGraphSourceOutboxAdapter(publisher)
    for revision in range(1, 151):
        with engine.begin() as connection:
            adapter.publish_public_graph_source_change(
                source_revision=revision,
                source_manifest_id=f"manifest_{revision}",
                source_manifest_hash=f"hash_{revision}",
                document_id=f"doc_{revision}",
                change_type="publish",
                occurred_at=clock.now(),
                connection=connection,
            )
    dispatcher = make_dispatcher(engine, now=clock.now, retention_days=30)
    clock.advance(seconds=31 * 24 * 3600)

    assert dispatcher.compact_due_events() == 100
    assert dispatcher.compact_due_events() == 50
    assert dispatcher.compact_due_events() == 0

    with engine.connect() as connection:
        compacted = connection.execute(
            select(func.count())
            .select_from(outbox_event_table)
            .where(outbox_event_table.c.storage_state == "compacted")
        ).scalar_one()
    assert compacted == 150
