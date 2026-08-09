"""Worker heartbeat renewal: 20s database-time renewals and fence-loss abort."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from _helpers import (
    build_engine,
    build_identity_service,
    cap,
    fixed_now,
    make_publisher,
    make_settings,
    provision_user,
)
from sqlalchemy import select, update

from app.outbox.dispatcher import OutboxDispatcher
from app.outbox.metrics import SqlAlchemyOutboxMetrics
from app.outbox.notifications import NotificationMaterializer
from app.outbox.schema import notification_table, outbox_delivery_table
from app.outbox.worker import LeaseHeartbeat, OutboxWorker
from app.platform.persistence import FenceViolation
from app.platform.runtime import build_runtime
from app.platform.worker import create_worker_runtime


class _MutableClock:
    def __init__(self, start: datetime) -> None:
        self._current = start

    def now(self) -> datetime:
        return self._current

    def advance(self, seconds: int) -> None:
        self._current = self._current + timedelta(seconds=seconds)


def make_dispatcher(engine, *, now):
    materializer = NotificationMaterializer(engine, notification_retention_days=90)
    return OutboxDispatcher(
        engine,
        consumers={"in_app_notification": materializer},
        now=now,
        retention_days=30,
        notification_retention_days=90,
        metrics=SqlAlchemyOutboxMetrics(),
    )


def publish(engine, *, user_ids, event_id="evt_1"):
    from app.outbox.ports import OutboxPublishCommand, RecipientSelection

    publisher = make_publisher(engine, now=lambda: fixed_now())
    command = OutboxPublishCommand(
        capability=cap("ingestion"),
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


def steal_lease(engine, event_id: str, *, owner: str, fence_token: int) -> None:
    with engine.begin() as connection:
        connection.execute(
            update(outbox_delivery_table)
            .where(outbox_delivery_table.c.event_id == event_id)
            .values(
                lease_owner=owner,
                fence_token=fence_token,
                lease_expires_at_utc=datetime(2099, 1, 1, tzinfo=UTC),
            )
        )


def delivery_state(engine, event_id: str) -> dict:
    with engine.connect() as connection:
        row = (
            connection.execute(
                select(
                    outbox_delivery_table.c.status,
                    outbox_delivery_table.c.lease_owner,
                    outbox_delivery_table.c.fence_token,
                ).where(outbox_delivery_table.c.event_id == event_id)
            )
            .mappings()
            .one()
        )
        return dict(row)


def test_heartbeat_renews_the_lease_every_interval_using_database_time() -> None:
    engine = build_engine()
    identity = build_identity_service(engine)
    alice = provision_user(identity, username="alice")
    publish(engine, user_ids=(alice,))
    clock = _MutableClock(datetime(2026, 8, 5, 12, 0, 0, tzinfo=UTC))
    dispatcher = make_dispatcher(engine, now=clock.now)

    claim = dispatcher.claim_one(owner="worker-1")
    assert claim is not None
    heartbeat = LeaseHeartbeat(dispatcher, claim, "worker-1", interval_seconds=20)

    clock.advance(seconds=20)
    renewed = heartbeat.beat()

    assert renewed is not None
    assert renewed.fence_token == claim.fence_token
    assert renewed.lease_expires_at == datetime(2026, 8, 5, 12, 1, 20, tzinfo=UTC)
    assert heartbeat.claim is renewed


def test_heartbeat_returns_none_and_stops_when_the_fence_is_lost() -> None:
    engine = build_engine()
    identity = build_identity_service(engine)
    alice = provision_user(identity, username="alice")
    publish(engine, user_ids=(alice,))
    clock = _MutableClock(datetime(2026, 8, 5, 12, 0, 0, tzinfo=UTC))
    dispatcher = make_dispatcher(engine, now=clock.now)

    claim = dispatcher.claim_one(owner="worker-1")
    assert claim is not None
    heartbeat = LeaseHeartbeat(dispatcher, claim, "worker-1", interval_seconds=20)
    steal_lease(engine, "evt_1", owner="worker-2", fence_token=99)

    assert heartbeat.beat() is None
    assert heartbeat.run_until(should_stop=lambda: False) is False


def test_worker_heartbeat_aborts_before_finalize_when_fence_is_lost() -> None:
    engine = build_engine()
    identity = build_identity_service(engine)
    alice = provision_user(identity, username="alice")
    publish(engine, user_ids=(alice,))
    configured = make_settings()
    runtime = build_runtime(
        configured,
        adapters={
            "database_engine": engine,
            "identity_access": identity,
            "outbox_dispatcher": make_dispatcher(
                engine, now=lambda: datetime(2026, 8, 5, 12, 0, 0, tzinfo=UTC)
            ),
        },
    )
    worker_runtime = create_worker_runtime(configured, runtime=runtime)
    worker = OutboxWorker(worker_runtime)

    claim = worker._dispatcher.claim_one(owner="worker-1")
    assert claim is not None
    steal_lease(engine, "evt_1", owner="worker-2", fence_token=99)

    with pytest.raises(FenceViolation):
        worker.run_delivery_with_heartbeat(
            claim,
            "worker-1",
            work=lambda _claim: False,
        )

    with engine.connect() as connection:
        # No notification was committed and the delivery was not finalized.
        assert connection.execute(select(notification_table)).all() == []
        state = delivery_state(engine, "evt_1")
        assert state["status"] == "running"
        assert state["lease_owner"] == "worker-2"
    runtime.close()


def test_worker_heartbeat_aborts_between_chunks_without_committing() -> None:
    engine = build_engine()
    identity = build_identity_service(engine)
    alice = provision_user(identity, username="alice")
    publish(engine, user_ids=(alice,))
    configured = make_settings()
    runtime = build_runtime(
        configured,
        adapters={
            "database_engine": engine,
            "identity_access": identity,
            "outbox_dispatcher": make_dispatcher(
                engine, now=lambda: datetime(2026, 8, 5, 12, 0, 0, tzinfo=UTC)
            ),
        },
    )
    worker_runtime = create_worker_runtime(configured, runtime=runtime)
    worker = OutboxWorker(worker_runtime)

    claim = worker._dispatcher.claim_one(owner="worker-1")
    assert claim is not None
    chunks = iter([True])  # one chunk of work remains, then the fence is lost

    def work(_claim) -> bool:
        steal_lease(engine, "evt_1", owner="worker-2", fence_token=99)
        return next(chunks, False)

    with pytest.raises(FenceViolation):
        worker.run_delivery_with_heartbeat(claim, "worker-1", work=work)

    with engine.connect() as connection:
        assert connection.execute(select(notification_table)).all() == []
        state = delivery_state(engine, "evt_1")
        assert state["status"] == "running"
    runtime.close()


def test_worker_heartbeat_delivers_normally_when_the_lease_is_kept() -> None:
    engine = build_engine()
    identity = build_identity_service(engine)
    alice = provision_user(identity, username="alice")
    publish(engine, user_ids=(alice,))
    configured = make_settings()
    runtime = build_runtime(
        configured,
        adapters={
            "database_engine": engine,
            "identity_access": identity,
            "outbox_dispatcher": make_dispatcher(
                engine, now=lambda: datetime(2026, 8, 5, 12, 0, 0, tzinfo=UTC)
            ),
        },
    )
    worker_runtime = create_worker_runtime(configured, runtime=runtime)
    worker = OutboxWorker(worker_runtime)

    claim = worker._dispatcher.claim_one(owner="worker-1")
    assert claim is not None
    outcome = worker.run_delivery_with_heartbeat(claim, "worker-1", work=lambda _claim: False)

    assert outcome.status == "delivered"
    with engine.connect() as connection:
        assert len(connection.execute(select(notification_table)).all()) == 1
        assert delivery_state(engine, "evt_1")["status"] == "delivered"
    runtime.close()
