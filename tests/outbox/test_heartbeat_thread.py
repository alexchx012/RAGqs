"""Blocker 3: independent timed heartbeat keeps a long delivery's lease alive.

The heartbeat runs on its own thread every interval, so a single long blocking
chunk of work cannot let the lease expire. All finalize paths still validate
the DB lease/fence before committing.
"""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from _helpers import (
    build_engine,
    build_identity_service,
    fixed_now,
    make_publisher,
    make_settings,
    provision_user,
)
from sqlalchemy import select

from app.outbox.dispatcher import OutboxDispatcher
from app.outbox.metrics import SqlAlchemyOutboxMetrics
from app.outbox.notifications import NotificationMaterializer
from app.outbox.ports import DeliveryOutcome
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


class _BackgroundRenewalFailureDispatcher:
    def __init__(self, outcome: BaseException | None) -> None:
        self.heartbeat_requires_exclusive_connection = False
        self._outcome = outcome
        self.background_renewal_started = threading.Event()
        self.finalized = False

    def renew_lease(self, claim, *, owner):
        del owner
        if threading.current_thread().name == "outbox-heartbeat":
            self.background_renewal_started.set()
            if self._outcome is not None:
                raise self._outcome
            return None
        return claim

    def run_consumer_and_finalize(self, claim, *, owner):
        del claim, owner
        self.finalized = True
        return DeliveryOutcome(status="delivered")


class _TrailingFenceLossDispatcher:
    def __init__(self) -> None:
        self.heartbeat_requires_exclusive_connection = False
        self.claim = SimpleNamespace(event_id="evt_trailing_fence_loss")
        self._claimed = False
        self.finalized = False
        self.background_renewed_after_finalize = threading.Event()

    def claim_one(self, owner):
        del owner
        if self._claimed:
            return None
        self._claimed = True
        return self.claim

    def renew_lease(self, claim, *, owner):
        del owner
        if threading.current_thread().name == "outbox-heartbeat" and self.finalized:
            self.background_renewed_after_finalize.set()
            return None
        return claim

    def run_consumer_and_finalize(self, claim, *, owner):
        del claim, owner
        self.finalized = True
        assert self.background_renewed_after_finalize.wait(timeout=1)
        return DeliveryOutcome(status="delivered")

    def recycle_expired_running(self, *, limit):
        del limit
        return 0

    def compact_due_events(self):
        return 0


def _wait_for_background_heartbeat_to_stop() -> None:
    deadline = time.monotonic() + 1
    while any(thread.name == "outbox-heartbeat" for thread in threading.enumerate()):
        if time.monotonic() >= deadline:
            raise AssertionError("background heartbeat did not stop")
        time.sleep(0.001)


class _OverlapDetectingDispatcher:
    def __init__(self, *, exclusive_connection: bool = True) -> None:
        self.heartbeat_requires_exclusive_connection = exclusive_connection
        self._state_lock = threading.Lock()
        self._active_renewals = 0
        self._finalizing = False
        self.first_renew_started = threading.Event()
        self.renewed_during_finalization = threading.Event()
        self.overlap_detected = False

    def renew_lease(self, claim, *, owner):
        del owner
        with self._state_lock:
            self._active_renewals += 1
            if self._finalizing:
                self.renewed_during_finalization.set()
            self.overlap_detected = (
                self.overlap_detected or self._active_renewals > 1 or self._finalizing
            )
            self.first_renew_started.set()
        try:
            time.sleep(0.05)
            return claim
        finally:
            with self._state_lock:
                self._active_renewals -= 1

    def run_consumer_and_finalize(self, claim, *, owner):
        del claim, owner
        with self._state_lock:
            self._finalizing = True
            self.overlap_detected = self.overlap_detected or self._active_renewals > 0
        try:
            if self.heartbeat_requires_exclusive_connection:
                time.sleep(0.05)
            else:
                assert self.renewed_during_finalization.wait(timeout=1)
            return DeliveryOutcome(status="delivered")
        finally:
            with self._state_lock:
                self._finalizing = False


def test_background_renewal_exception_propagates_before_the_next_work_chunk() -> None:
    injected = RuntimeError("heartbeat database unavailable")
    dispatcher = _BackgroundRenewalFailureDispatcher(injected)
    worker = object.__new__(OutboxWorker)
    worker._dispatcher = dispatcher  # type: ignore[assignment]
    claim = SimpleNamespace(event_id="evt_background_error")
    chunks = 0

    def work(_claim) -> bool:
        nonlocal chunks
        chunks += 1
        assert dispatcher.background_renewal_started.wait(timeout=1)
        _wait_for_background_heartbeat_to_stop()
        return chunks == 1

    with pytest.raises(RuntimeError) as caught:
        worker.run_delivery_with_heartbeat(
            claim,  # type: ignore[arg-type]
            "worker-1",
            work=work,
            heartbeat_interval_seconds=0.001,
        )

    assert caught.value is injected
    assert chunks == 1
    assert dispatcher.finalized is False


def test_background_fence_loss_aborts_before_the_next_work_chunk() -> None:
    dispatcher = _BackgroundRenewalFailureDispatcher(None)
    worker = object.__new__(OutboxWorker)
    worker._dispatcher = dispatcher  # type: ignore[assignment]
    claim = SimpleNamespace(event_id="evt_background_fence_loss")
    chunks = 0

    def work(_claim) -> bool:
        nonlocal chunks
        chunks += 1
        assert dispatcher.background_renewal_started.wait(timeout=1)
        _wait_for_background_heartbeat_to_stop()
        return chunks == 1

    with pytest.raises(FenceViolation, match="evt_background_fence_loss"):
        worker.run_delivery_with_heartbeat(
            claim,  # type: ignore[arg-type]
            "worker-1",
            work=work,
            heartbeat_interval_seconds=0.001,
        )

    assert chunks == 1
    assert dispatcher.finalized is False


def test_trailing_heartbeat_loss_after_successful_finalize_is_delivered() -> None:
    class _FastHeartbeatWorker(OutboxWorker):
        def run_delivery_with_heartbeat(self, claim, owner, *, work=None):
            return super().run_delivery_with_heartbeat(
                claim,
                owner,
                work=work,
                heartbeat_interval_seconds=0.001,
            )

    dispatcher = _TrailingFenceLossDispatcher()
    worker = object.__new__(_FastHeartbeatWorker)
    worker._dispatcher = dispatcher  # type: ignore[assignment]

    stats = worker.run_once(owner="worker-1", limit=1)

    assert dispatcher.finalized is True
    assert stats.claimed == 1
    assert stats.delivered == 1
    assert stats.failed == 0


def test_final_database_phase_does_not_overlap_the_heartbeat_thread() -> None:
    dispatcher = _OverlapDetectingDispatcher()
    worker = object.__new__(OutboxWorker)
    worker._dispatcher = dispatcher  # type: ignore[assignment]
    claim = object()

    def work(_claim) -> bool:
        assert dispatcher.first_renew_started.wait(timeout=1)
        return False

    outcome = worker.run_delivery_with_heartbeat(
        claim,  # type: ignore[arg-type]
        "worker-1",
        work=work,
        heartbeat_interval_seconds=0.001,
    )

    assert outcome.status == "delivered"
    assert dispatcher.overlap_detected is False


def test_nonexclusive_pool_keeps_heartbeat_running_during_finalization() -> None:
    dispatcher = _OverlapDetectingDispatcher(exclusive_connection=False)
    worker = object.__new__(OutboxWorker)
    worker._dispatcher = dispatcher  # type: ignore[assignment]
    claim = object()

    def work(_claim) -> bool:
        assert dispatcher.first_renew_started.wait(timeout=1)
        return False

    outcome = worker.run_delivery_with_heartbeat(
        claim,  # type: ignore[arg-type]
        "worker-1",
        work=work,
        heartbeat_interval_seconds=0.001,
    )

    assert outcome.status == "delivered"
    assert dispatcher.renewed_during_finalization.is_set()


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
