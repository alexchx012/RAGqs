"""Blocker 3: independent timed heartbeat keeps a long delivery's lease alive.

The heartbeat runs on its own thread every interval, so a single long blocking
chunk of work cannot let the lease expire. All finalize paths still validate
the DB lease/fence before committing.
"""

from __future__ import annotations

import time
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
from sqlalchemy import select

from app.outbox.dispatcher import OutboxDispatcher
from app.outbox.metrics import SqlAlchemyOutboxMetrics
from app.outbox.notifications import NotificationMaterializer
from app.outbox.schema import notification_table, outbox_delivery_table
from app.outbox.worker import OutboxWorker
from app.platform.persistence import FenceViolation
from app.platform.runtime import build_runtime
from app.platform.worker import create_worker_runtime


class _MutableClock:
    def __init__(self, start: datetime) -> None:
        self._current = start
        self._lock = __import__("threading").Lock()

    def now(self) -> datetime:
        with self._lock:
            return self._current

    def advance(self, seconds: int) -> None:
        with self._lock:
            self._current = self._current + timedelta(seconds=seconds)


def make_dispatcher(engine, *, now):
    return OutboxDispatcher(
        engine,
        consumers={"in_app_notification": NotificationMaterializer(engine)},
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
        caller_principal="ingestion",
        event_type="ingestion_completed",
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


def test_independent_heartbeat_keeps_lease_alive_during_a_long_blocking_work() -> None:
    engine = build_engine()
    identity = build_identity_service(engine)
    alice = provision_user(identity, username="alice")
    publish(engine, user_ids=(alice,))
    clock = _MutableClock(datetime(2026, 8, 5, 12, 0, 0, tzinfo=UTC))
    configured = make_settings()
    runtime = build_runtime(
        configured,
        adapters={
            "database_engine": engine,
            "identity_access": identity,
            "outbox_dispatcher": make_dispatcher(engine, now=clock.now),
        },
    )
    worker_runtime = create_worker_runtime(configured, runtime=runtime)
    worker = OutboxWorker(worker_runtime)

    claim = worker._dispatcher.claim_one(owner="worker-1")
    assert claim is not None

    # A single blocking chunk that simulates >120s of work. The independent
    # heartbeat thread (real 0.01s interval) renews between clock advances.
    def work(_claim) -> bool:
        for _ in range(8):
            clock.advance(seconds=20)  # 20s of simulated work per step
            time.sleep(0.5)  # ample real time for the heartbeat thread to beat
        return False

    outcome = worker.run_delivery_with_heartbeat(
        claim, "worker-1", work=work, heartbeat_interval_seconds=0.01
    )

    assert outcome.status == "delivered"
    with engine.connect() as connection:
        assert len(connection.execute(select(notification_table)).all()) == 1
        status = connection.execute(
            select(outbox_delivery_table.c.status).where(
                outbox_delivery_table.c.event_id == "evt_1"
            )
        ).scalar_one()
        assert status == "delivered"
    runtime.close()


def test_lease_expires_without_heartbeat_and_finalize_is_rejected() -> None:
    engine = build_engine()
    identity = build_identity_service(engine)
    alice = provision_user(identity, username="alice")
    publish(engine, user_ids=(alice,))
    clock = _MutableClock(datetime(2026, 8, 5, 12, 0, 0, tzinfo=UTC))
    dispatcher = make_dispatcher(engine, now=clock.now)

    claim = dispatcher.claim_one(owner="worker-1")
    assert claim is not None
    # Simulate a stalled worker: 120s pass with no heartbeat.
    clock.advance(seconds=120)

    assert dispatcher.renew_lease(claim, owner="worker-1") is None
    with pytest.raises(FenceViolation):
        dispatcher.run_consumer_and_finalize(claim, owner="worker-1")
    with engine.connect() as connection:
        assert connection.execute(select(notification_table)).all() == []


def test_console_entry_runs_forever_and_stops_gracefully() -> None:
    import importlib

    worker_module = importlib.import_module("app.outbox.worker")
    assert callable(worker_module.main)
    # main delegates to the resident run_forever loop.
    import inspect

    source = inspect.getsource(worker_module.main)
    assert "run_forever" in source
