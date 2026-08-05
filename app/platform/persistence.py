from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, Protocol, Self, TypeVar

_Result = TypeVar("_Result")


class IdempotencyConflict(RuntimeError):
    """The same key was used for a different request payload."""


class LeaseUnavailable(RuntimeError):
    """A live lease is held by another owner."""


class FenceViolation(RuntimeError):
    """An executor attempted a write with an obsolete fence token."""


@dataclass(frozen=True, slots=True)
class AuditRecord:
    actor_id: str
    resource_type: str
    resource_id: str
    request_id: str
    result: str
    occurred_at_utc: datetime | None = None


class TransactionScope(Protocol):
    def get(self, key: str, default: Any = None) -> Any: ...

    def set(self, key: str, value: Any) -> None: ...

    def delete(self, key: str) -> None: ...

    def audit(self, record: AuditRecord) -> None: ...

    def __enter__(self) -> Self: ...

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> bool | None: ...


class TransactionManager(Protocol):
    def begin(self) -> TransactionScope: ...


class AuditWriter(Protocol):
    def record(self, record: AuditRecord) -> None: ...


class DatabaseClock(Protocol):
    def now_utc(self) -> datetime: ...


@dataclass(frozen=True, slots=True)
class IdempotencyReservation:
    replayed: bool
    result: Any = None
    in_progress: bool = False


class IdempotencyStore(Protocol):
    def get_or_reserve(self, key: str, request_hash: str) -> IdempotencyReservation: ...

    def commit_result(self, key: str, request_hash: str, result: Any) -> None: ...


@dataclass(frozen=True, slots=True)
class Lease:
    resource: str
    owner: str
    fence_token: int
    expires_at_utc: datetime


class LeaseStore(Protocol):
    def acquire(self, resource: str, owner: str, ttl: timedelta) -> Lease: ...

    def renew(self, lease: Lease, ttl: timedelta) -> Lease: ...

    def assert_fence(self, resource: str, owner: str, fence_token: int) -> None: ...

    def write_with_fence(self, lease: Lease, operation: Callable[[Any], _Result]) -> _Result: ...


class _MemoryTransaction:
    def __init__(self, manager: MemoryTransactionManager) -> None:
        self._manager = manager
        self._state = deepcopy(manager.state)
        self._audit_records = list(manager.audit_records)

    def __enter__(self) -> _MemoryTransaction:
        return self

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> Literal[False]:
        if exc_type is None:
            self._manager.state = self._state
            self._manager.audit_records = self._audit_records
        return False

    def get(self, key: str, default: Any = None) -> Any:
        return self._state.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self._state[key] = deepcopy(value)

    def delete(self, key: str) -> None:
        self._state.pop(key, None)

    def audit(self, record: AuditRecord) -> None:
        self._audit_records.append(record)


@dataclass
class MemoryTransactionManager:
    state: dict[str, Any] = field(default_factory=dict)
    audit_records: list[AuditRecord] = field(default_factory=list)

    def begin(self) -> _MemoryTransaction:
        return _MemoryTransaction(self)


@dataclass
class MemoryIdempotencyStore:
    _records: dict[str, tuple[str, bool, Any]] = field(default_factory=dict)

    def get_or_reserve(self, key: str, request_hash: str) -> IdempotencyReservation:
        existing = self._records.get(key)
        if existing is None:
            self._records[key] = (request_hash, False, None)
            return IdempotencyReservation(replayed=False)
        stored_hash, committed, result = existing
        if stored_hash != request_hash:
            raise IdempotencyConflict(f"idempotency key conflict: {key}")
        if committed:
            return IdempotencyReservation(replayed=True, result=deepcopy(result))
        return IdempotencyReservation(replayed=False, in_progress=True)

    def commit_result(self, key: str, request_hash: str, result: Any) -> None:
        existing = self._records.get(key)
        if existing is None or existing[0] != request_hash:
            raise IdempotencyConflict(f"idempotency key conflict: {key}")
        self._records[key] = (request_hash, True, deepcopy(result))


class MemoryDatabaseClock:
    def __init__(self, now: datetime | None = None) -> None:
        self._now = now or datetime.now(UTC)

    def now_utc(self) -> datetime:
        return self._now.astimezone(UTC)


@dataclass
class MemoryLeaseStore:
    now: Callable[[], datetime]
    _leases: dict[str, Lease] = field(default_factory=dict)
    _next_fence: int = 0

    def _current_time(self) -> datetime:
        return self.now().astimezone(UTC)

    def acquire(self, resource: str, owner: str, ttl: timedelta) -> Lease:
        if ttl <= timedelta(0):
            raise ValueError("lease ttl must be positive")
        current = self._leases.get(resource)
        now = self._current_time()
        if current is not None and current.expires_at_utc > now:
            raise LeaseUnavailable(f"lease is held for {resource}")
        self._next_fence += 1
        lease = Lease(resource, owner, self._next_fence, now + ttl)
        self._leases[resource] = lease
        return lease

    def renew(self, lease: Lease, ttl: timedelta) -> Lease:
        self.assert_fence(lease.resource, lease.owner, lease.fence_token)
        renewed = Lease(
            lease.resource,
            lease.owner,
            lease.fence_token,
            self._current_time() + ttl,
        )
        self._leases[lease.resource] = renewed
        return renewed

    def assert_fence(self, resource: str, owner: str, fence_token: int) -> None:
        current = self._leases.get(resource)
        if (
            current is None
            or current.owner != owner
            or current.fence_token != fence_token
            or current.expires_at_utc <= self._current_time()
        ):
            raise FenceViolation(f"stale fence for {resource}")

    def write_with_fence(self, lease: Lease, operation: Callable[[Any], _Result]) -> _Result:
        self.assert_fence(lease.resource, lease.owner, lease.fence_token)
        result = operation(None)
        self.assert_fence(lease.resource, lease.owner, lease.fence_token)
        return result


class MemoryAuditWriter:
    def __init__(self) -> None:
        self.records: list[AuditRecord] = []

    def record(self, record: AuditRecord) -> None:
        self.records.append(record)
