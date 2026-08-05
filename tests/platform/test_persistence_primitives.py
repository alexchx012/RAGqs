from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.platform.persistence import (
    AuditRecord,
    FenceViolation,
    IdempotencyConflict,
    LeaseUnavailable,
    MemoryDatabaseClock,
    MemoryIdempotencyStore,
    MemoryLeaseStore,
    MemoryTransactionManager,
)


def test_transaction_rolls_back_state_and_audit_together() -> None:
    manager = MemoryTransactionManager()
    record = AuditRecord(
        actor_id="user-1",
        resource_type="document",
        resource_id="doc-1",
        request_id="req_123",
        result="created",
    )

    with pytest.raises(RuntimeError, match="abort"):
        with manager.begin() as transaction:
            transaction.set("document:doc-1", {"status": "created"})
            transaction.audit(record)
            raise RuntimeError("abort")

    assert manager.state == {}
    assert manager.audit_records == []


def test_transaction_commits_state_and_audit_as_one_unit() -> None:
    manager = MemoryTransactionManager()
    record = AuditRecord(
        actor_id="user-1",
        resource_type="document",
        resource_id="doc-1",
        request_id="req_123",
        result="created",
    )

    with manager.begin() as transaction:
        transaction.set("document:doc-1", {"status": "created"})
        transaction.audit(record)

    assert manager.state["document:doc-1"] == {"status": "created"}
    assert manager.audit_records == [record]


def test_idempotency_replays_same_hash_and_rejects_different_hash() -> None:
    store = MemoryIdempotencyStore()

    reservation = store.get_or_reserve("req-key", "hash-a")
    assert reservation.replayed is False
    store.commit_result("req-key", "hash-a", {"id": "result-1"})

    replay = store.get_or_reserve("req-key", "hash-a")
    assert replay.replayed is True
    assert replay.result == {"id": "result-1"}

    with pytest.raises(IdempotencyConflict):
        store.get_or_reserve("req-key", "hash-b")


def test_database_clock_returns_utc_now() -> None:
    now = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)
    clock = MemoryDatabaseClock(now)

    assert clock.now_utc() == now
    assert clock.now_utc().tzinfo == UTC


def test_lease_uses_monotonic_fence_and_rejects_stale_owner() -> None:
    now = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)
    current = [now]
    leases = MemoryLeaseStore(lambda: current[0])

    first = leases.acquire("job-1", "worker-a", timedelta(seconds=30))
    assert first.fence_token == 1
    with pytest.raises(LeaseUnavailable):
        leases.acquire("job-1", "worker-b", timedelta(seconds=30))

    current[0] = now + timedelta(seconds=31)
    second = leases.acquire("job-1", "worker-b", timedelta(seconds=30))
    assert second.fence_token == 2

    with pytest.raises(FenceViolation):
        leases.assert_fence("job-1", "worker-a", first.fence_token)
    leases.assert_fence("job-1", "worker-b", second.fence_token)
