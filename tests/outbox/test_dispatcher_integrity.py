"""Dispatcher integrity: seq uniqueness, fenced finalize rowcount, lease
expiry enforcement, replay cycle attempts, and deployable worker entrypoints."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, text, update

from app.outbox.dispatcher import OutboxDispatcher
from app.outbox.metrics import SqlAlchemyOutboxMetrics
from app.outbox.notifications import NotificationMaterializer
from app.outbox.schema import (
    notification_inbox_table,
    notification_table,
    outbox_delivery_attempt_table,
    outbox_delivery_table,
)
from app.platform.persistence import FenceViolation
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


def make_dispatcher(engine, *, now=None):
    return OutboxDispatcher(
        engine,
        consumers={"in_app_notification": NotificationMaterializer(engine)},
        now=now or (lambda: fixed_now()),
        retention_days=30,
        notification_retention_days=90,
        metrics=SqlAlchemyOutboxMetrics(),
    )


def publish(engine, *, user_ids, event_id, aggregate_id=None, clock=None):
    from app.outbox.ports import OutboxPublishCommand, RecipientSelection

    publisher = make_publisher(engine, now=clock or (lambda: fixed_now()))
    command = OutboxPublishCommand(
        event_id=event_id,
        caller_principal="ingestion",
        event_type="ingestion_completed",
        schema_version=1,
        aggregate_type="ingestion_job",
        aggregate_id=aggregate_id or f"job_{event_id}",
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


def steal_lease(engine, event_id: str) -> None:
    with engine.begin() as connection:
        connection.execute(
            update(outbox_delivery_table)
            .where(outbox_delivery_table.c.event_id == event_id)
            .values(
                lease_owner="worker-2",
                fence_token=999,
                lease_expires_at_utc=datetime(2099, 1, 1, tzinfo=UTC),
            )
        )


def test_notification_seq_is_unique_per_recipient() -> None:
    engine = build_engine()
    # The schema must enforce (recipient_user_id, notification_seq) uniqueness.
    with engine.connect() as connection:
        table_sql = connection.execute(
            text("SELECT sql FROM sqlite_master WHERE type='table' AND name='notification'")
        ).scalar_one()
    assert "UNIQUE" in table_sql
    assert "notification_seq" in table_sql


def test_materialization_seq_allocation_is_monotonic_under_repeat_delivery() -> None:
    engine = build_engine()
    identity = build_identity_service(engine)
    alice = provision_user(identity, username="alice")
    clock = _MutableClock(datetime(2026, 8, 5, 12, 0, 0, tzinfo=UTC))
    dispatcher = make_dispatcher(engine, now=clock.now)
    publish(engine, user_ids=(alice,), event_id="evt_1")
    publish(engine, user_ids=(alice,), event_id="evt_2")

    claim = dispatcher.claim_one(owner="worker-1")
    assert claim is not None
    dispatcher.run_consumer_and_finalize(claim, owner="worker-1")
    claim2 = dispatcher.claim_one(owner="worker-1")
    assert claim2 is not None
    dispatcher.run_consumer_and_finalize(claim2, owner="worker-1")

    with engine.connect() as connection:
        seqs = (
            connection.execute(
                select(notification_table.c.notification_seq).where(
                    notification_table.c.recipient_user_id == alice
                )
            )
            .scalars()
            .all()
        )
        assert sorted(seqs) == [1, 2]
        inbox = connection.execute(
            select(notification_inbox_table.c.next_notification_seq).where(
                notification_inbox_table.c.recipient_user_id == alice
            )
        ).scalar_one()
        assert inbox == 3


def test_finalize_fenced_delivery_update_must_affect_exactly_one_row() -> None:
    engine = build_engine()
    identity = build_identity_service(engine)
    alice = provision_user(identity, username="alice")
    dispatcher = make_dispatcher(engine)
    publish(engine, user_ids=(alice,), event_id="evt_1")

    claim = dispatcher.claim_one(owner="worker-1")
    assert claim is not None
    steal_lease(engine, "evt_1")

    # The fence is gone: finalize must raise and commit nothing.
    with pytest.raises(FenceViolation):
        dispatcher.run_consumer_and_finalize(claim, owner="worker-1")
    with engine.connect() as connection:
        assert connection.execute(select(notification_table)).all() == []
        delivery = connection.execute(
            select(outbox_delivery_table.c.status).where(
                outbox_delivery_table.c.event_id == "evt_1"
            )
        ).scalar_one()
        assert delivery == "running"
        assert connection.execute(select(outbox_delivery_attempt_table)).all() == [
            connection.execute(select(outbox_delivery_attempt_table)).all()[0]
        ]


def test_expired_lease_cannot_renew_or_finalize() -> None:
    engine = build_engine()
    identity = build_identity_service(engine)
    alice = provision_user(identity, username="alice")
    clock = _MutableClock(datetime(2026, 8, 5, 12, 0, 0, tzinfo=UTC))
    dispatcher = make_dispatcher(engine, now=clock.now)
    publish(engine, user_ids=(alice,), event_id="evt_1", clock=clock.now)

    claim = dispatcher.claim_one(owner="worker-1")
    assert claim is not None
    clock.advance(seconds=120)  # lease (60s) expired

    assert dispatcher.renew_lease(claim, owner="worker-1") is None
    with pytest.raises(FenceViolation):
        dispatcher.run_consumer_and_finalize(claim, owner="worker-1")
    with engine.connect() as connection:
        assert connection.execute(select(notification_table)).all() == []


def test_replay_cycle_attempt_starts_fresh_and_retries_use_cycle_delays() -> None:
    engine = build_engine()
    identity = build_identity_service(engine)
    alice = provision_user(identity, username="alice")
    clock = _MutableClock(datetime(2026, 8, 5, 12, 0, 0, tzinfo=UTC))
    dispatcher = make_dispatcher(engine, now=clock.now)
    publish(engine, user_ids=(alice,), event_id="evt_1", clock=clock.now)

    # Dead-letter through 8 global attempts.
    claim = dispatcher.claim_one(owner="worker-1")
    assert claim is not None
    for _ in range(7):
        dispatcher.fail_and_schedule(
            claim, owner="worker-1", error_category="retryable", error_code="boom"
        )
        clock.advance(seconds=100000)
        claim = dispatcher.claim_one(owner="worker-1")
        assert claim is not None
    dispatcher.fail_and_schedule(
        claim, owner="worker-1", error_category="retryable", error_code="boom"
    )

    dispatcher.replay(
        "evt_1",
        consumer_name="in_app_notification",
        expected_version=9,
        idempotency_key="k1",
        request_hash="h1",
    )
    clock.advance(seconds=1)
    replay_claim = dispatcher.claim_one(owner="worker-2")
    assert replay_claim is not None
    # A fresh replay cycle starts at cycle attempt 1, then uses cycle delays.
    assert replay_claim.cycle_attempt_number == 1
    assert replay_claim.attempt_number == 9
    outcome = dispatcher.fail_and_schedule(
        replay_claim, owner="worker-2", error_category="retryable", error_code="boom"
    )
    assert outcome.status == "failed"
    clock.advance(seconds=100000)
    next_claim = dispatcher.claim_one(owner="worker-2")
    assert next_claim is not None
    assert next_claim.cycle_attempt_number == 2


def test_cycle_attempt_tracking_survives_lease_expiry_with_correct_delay() -> None:
    engine = build_engine()
    identity = build_identity_service(engine)
    alice = provision_user(identity, username="alice")
    clock = _MutableClock(datetime(2026, 8, 5, 12, 0, 0, tzinfo=UTC))
    dispatcher = make_dispatcher(engine, now=clock.now)
    publish(engine, user_ids=(alice,), event_id="evt_1", clock=clock.now)

    claim = dispatcher.claim_one(owner="worker-1")
    assert claim is not None
    clock.advance(seconds=120)
    dispatcher.recycle_expired_running()
    clock.advance(seconds=100000)
    recycled_claim = dispatcher.claim_one(owner="worker-2")
    assert recycled_claim is not None
    # The expired attempt consumed cycle attempt 1; the retry is cycle attempt 2.
    assert recycled_claim.cycle_attempt_number == 2


def test_worker_run_once_delivers_and_tracks_dead_lettered() -> None:
    engine = build_engine()
    identity = build_identity_service(engine)
    alice = provision_user(identity, username="alice")
    publish(engine, user_ids=(alice,), event_id="evt_1")
    publish(engine, user_ids=(alice,), event_id="evt_2")
    from app.outbox.worker import OutboxWorker
    from app.platform.runtime import build_runtime
    from app.platform.worker import create_worker_runtime
    from tests._support import make_settings

    configured = make_settings()
    runtime = build_runtime(
        configured,
        adapters={
            "database_engine": engine,
            "identity_access": identity,
            "outbox_dispatcher": make_dispatcher(engine),
        },
    )
    worker_runtime = create_worker_runtime(configured, runtime=runtime)
    worker = OutboxWorker(worker_runtime)

    stats = worker.run_once(owner="worker-1")

    assert stats.delivered == 2
    assert stats.failed == 0
    assert stats.dead_lettered == 0
    runtime.close()


def test_worker_run_once_dead_letters_an_unsupported_consumer() -> None:
    engine = build_engine()
    identity = build_identity_service(engine)
    alice = provision_user(identity, username="alice")
    publish(engine, user_ids=(alice,), event_id="evt_1")
    # A consumer-less dispatcher makes the single delivery permanent-fail.
    from app.outbox.worker import OutboxWorker
    from app.platform.runtime import build_runtime
    from app.platform.worker import create_worker_runtime
    from tests._support import make_settings

    bare = OutboxDispatcher(
        engine,
        consumers={},
        now=lambda: fixed_now(),
        retention_days=30,
        notification_retention_days=90,
        metrics=SqlAlchemyOutboxMetrics(),
    )
    configured = make_settings()
    runtime = build_runtime(
        configured,
        adapters={
            "database_engine": engine,
            "identity_access": identity,
            "outbox_dispatcher": bare,
        },
    )
    worker_runtime = create_worker_runtime(configured, runtime=runtime)
    worker = OutboxWorker(worker_runtime)

    stats = worker.run_once(owner="worker-1")

    assert stats.failed == 0
    assert stats.dead_lettered == 1
    with engine.connect() as connection:
        status = connection.execute(
            select(outbox_delivery_table.c.status).where(
                outbox_delivery_table.c.event_id == "evt_1"
            )
        ).scalar_one()
        assert status == "dead_letter"
    runtime.close()


def test_runtime_hides_raw_publisher_behind_scoped_adapters() -> None:
    from app.platform.runtime import build_runtime
    from tests._support import make_settings

    engine = build_engine()
    runtime = build_runtime(make_settings(), adapters={"database_engine": engine})

    assert runtime.resolve("outbox_publisher", None) is None
    assert runtime.resolve("ingestion_outbox_port") is not None
    runtime.close()


def test_outbox_worker_main_entrypoint_is_callable() -> None:
    import importlib

    module = importlib.import_module("app.outbox.worker")
    assert callable(module.main)
    assert callable(module.run_outbox_worker_once)
