from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, TypeVar

from sqlalchemy import (
    JSON,
    BigInteger,
    Column,
    DateTime,
    Float,
    Integer,
    MetaData,
    String,
    Table,
    and_,
    create_engine,
    func,
    select,
    update,
)
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.pool import StaticPool

from .config import PlatformSettings
from .persistence import (
    AuditRecord,
    FenceViolation,
    IdempotencyConflict,
    IdempotencyReservation,
    Lease,
    LeaseUnavailable,
)

_Result = TypeVar("_Result")

core_metadata = MetaData()

platform_audit_table = Table(
    "platform_audit",
    core_metadata,
    # The migration owns only shared audit primitives, not domain entities.
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("actor_id", String(128), nullable=False),
    Column("resource_type", String(128), nullable=False),
    Column("resource_id", String(256), nullable=False),
    Column("request_id", String(128), nullable=False),
    Column("occurred_at_utc", DateTime(timezone=True), nullable=False),
    Column("result", String(64), nullable=False),
    Column("details_json", JSON, nullable=False),
)

platform_idempotency_table = Table(
    "platform_idempotency",
    core_metadata,
    Column("scope", String(128), primary_key=True),
    Column("idempotency_key", String(256), primary_key=True),
    Column("request_hash", String(128), nullable=False),
    Column("status", String(32), nullable=False),
    Column("response_json", JSON, nullable=True),
    Column("created_at_utc", DateTime(timezone=True), nullable=False),
    Column("completed_at_utc", DateTime(timezone=True), nullable=True),
)

platform_lease_table = Table(
    "platform_lease",
    core_metadata,
    Column("resource", String(256), primary_key=True),
    Column("owner", String(128), nullable=False),
    Column("fence_token", BigInteger, nullable=False),
    Column("expires_at_utc", DateTime(timezone=True), nullable=False),
    Column("updated_at_utc", DateTime(timezone=True), nullable=False),
)

platform_observability_sample_table = Table(
    "platform_observability_sample",
    core_metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("observed_at_utc", DateTime(timezone=True), nullable=False),
    Column("route_template", String(256), nullable=False),
    Column("method", String(16), nullable=False),
    Column("outcome_class", String(64), nullable=False),
    Column("status_family", String(16), nullable=False),
    Column("latency_ms", Integer, nullable=False),
    Column("sample_weight", Float, nullable=False),
    Column("retention_days", Integer, nullable=False),
    Column("expires_at_utc", DateTime(timezone=True), nullable=False),
)

platform_observability_aggregate_table = Table(
    "platform_observability_aggregate",
    core_metadata,
    Column("bucket_start_utc", DateTime(timezone=True), primary_key=True),
    Column("route_template", String(256), primary_key=True),
    Column("method", String(16), primary_key=True),
    Column("outcome_class", String(64), primary_key=True),
    Column("status_family", String(16), primary_key=True),
    Column("latency_bucket_ms", Integer, primary_key=True),
    Column("retention_days", Integer, primary_key=True),
    Column("expires_at_utc", DateTime(timezone=True), nullable=False),
    Column("sample_weight", Float, nullable=False),
    Column("sample_count", BigInteger, nullable=False),
)

CORE_TABLE_NAMES = frozenset(core_metadata.tables)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _current_timestamp(connection: Connection) -> datetime:
    database_time = (
        func.clock_timestamp(type_=DateTime(timezone=True))
        if connection.dialect.name == "postgresql"
        else func.current_timestamp()
    )
    value = connection.execute(select(database_time)).scalar_one()
    if not isinstance(value, datetime):
        raise RuntimeError("database did not return a timestamp")
    return _as_utc(value)


def _insert_do_nothing(
    connection: Connection,
    table: Table,
    values: dict[str, Any],
    index_elements: list[str],
):
    if connection.dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert as postgresql_insert

        return (
            postgresql_insert(table)
            .values(**values)
            .on_conflict_do_nothing(index_elements=index_elements)
        )
    if connection.dialect.name == "sqlite":
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert

        return (
            sqlite_insert(table)
            .values(**values)
            .on_conflict_do_nothing(index_elements=index_elements)
        )
    return table.insert().values(**values)


def create_engine_for_settings(settings: PlatformSettings) -> Engine:
    options: dict[str, Any] = {"pool_pre_ping": True}
    if settings.database.url.startswith("postgresql"):
        options["pool_size"] = settings.database.pool_size
        options["connect_args"] = {
            "connect_timeout": settings.database.connect_timeout_seconds,
        }
    elif settings.database.url in {"sqlite://", "sqlite+pysqlite:///:memory:"}:
        options["connect_args"] = {"check_same_thread": False}
        options["poolclass"] = StaticPool
    return create_engine(settings.database.url, **options)


@contextmanager
def transaction(engine: Engine) -> Iterator[Connection]:
    with engine.begin() as connection:
        yield connection


class SqlAlchemyDatabaseClock:
    """Reads time from the configured database rather than the process clock."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def now_utc(self, connection: Connection | None = None) -> datetime:
        if connection is not None:
            return _current_timestamp(connection)
        with self._engine.connect() as active_connection:
            return _current_timestamp(active_connection)


class SqlAlchemyTransaction:
    """One explicit SQL transaction shared by domain writes and core primitives."""

    def __init__(self, engine: Engine, clock: SqlAlchemyDatabaseClock) -> None:
        self._engine = engine
        self._clock = clock
        self.connection: Connection | None = None
        self._transaction: Any = None
        self._state: dict[str, Any] = {}

    def __enter__(self) -> SqlAlchemyTransaction:
        self.connection = self._engine.connect()
        self._transaction = self.connection.begin()
        return self

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> Literal[False]:
        if self._transaction is None or self.connection is None:
            return False
        try:
            if exc_type is None:
                self._transaction.commit()
            else:
                self._transaction.rollback()
        finally:
            self.connection.close()
            self.connection = None
            self._transaction = None
        return False

    def _connection(self) -> Connection:
        if self.connection is None:
            raise RuntimeError("transaction has not started")
        return self.connection

    def get(self, key: str, default: Any = None) -> Any:
        return self._state.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self._state[key] = value

    def delete(self, key: str) -> None:
        self._state.pop(key, None)

    def audit(self, record: AuditRecord) -> None:
        connection = self._connection()
        occurred_at = record.occurred_at_utc or self._clock.now_utc(connection)
        connection.execute(
            platform_audit_table.insert().values(
                actor_id=record.actor_id,
                resource_type=record.resource_type,
                resource_id=record.resource_id,
                request_id=record.request_id,
                occurred_at_utc=_as_utc(occurred_at),
                result=record.result,
                details_json={},
            )
        )

    def reserve_idempotency(
        self,
        scope: str,
        key: str,
        request_hash: str,
    ) -> IdempotencyReservation:
        connection = self._connection()
        now = self._clock.now_utc(connection)
        statement = _insert_do_nothing(
            connection,
            platform_idempotency_table,
            {
                "scope": scope,
                "idempotency_key": key,
                "request_hash": request_hash,
                "status": "reserved",
                "response_json": None,
                "created_at_utc": now,
                "completed_at_utc": None,
            },
            ["scope", "idempotency_key"],
        )
        inserted = connection.execute(statement).rowcount
        if inserted == 1:
            return IdempotencyReservation(replayed=False)

        record = (
            connection.execute(
                select(
                    platform_idempotency_table.c.request_hash,
                    platform_idempotency_table.c.status,
                    platform_idempotency_table.c.response_json,
                ).where(
                    and_(
                        platform_idempotency_table.c.scope == scope,
                        platform_idempotency_table.c.idempotency_key == key,
                    )
                )
            )
            .mappings()
            .one()
        )
        if record["request_hash"] != request_hash:
            raise IdempotencyConflict(f"idempotency key conflict: {key}")
        if record["status"] == "completed":
            return IdempotencyReservation(replayed=True, result=record["response_json"])
        return IdempotencyReservation(replayed=False, in_progress=True)

    def commit_idempotency(
        self,
        scope: str,
        key: str,
        request_hash: str,
        result: Any,
    ) -> None:
        connection = self._connection()
        updated = connection.execute(
            update(platform_idempotency_table)
            .where(
                and_(
                    platform_idempotency_table.c.scope == scope,
                    platform_idempotency_table.c.idempotency_key == key,
                    platform_idempotency_table.c.request_hash == request_hash,
                    platform_idempotency_table.c.status == "reserved",
                )
            )
            .values(
                status="completed",
                response_json=result,
                completed_at_utc=self._clock.now_utc(connection),
            )
        ).rowcount
        if updated != 1:
            raise IdempotencyConflict(f"idempotency key conflict: {key}")


class SqlAlchemyTransactionManager:
    def __init__(self, engine: Engine, clock: SqlAlchemyDatabaseClock | None = None) -> None:
        self._engine = engine
        self._clock = clock or SqlAlchemyDatabaseClock(engine)

    def begin(self) -> SqlAlchemyTransaction:
        return SqlAlchemyTransaction(self._engine, self._clock)


class SqlAlchemyLeaseStore:
    """Lease operations use database time and conditional writes for fencing."""

    def __init__(self, engine: Engine, clock: SqlAlchemyDatabaseClock | None = None) -> None:
        self._engine = engine
        self._clock = clock or SqlAlchemyDatabaseClock(engine)

    @staticmethod
    def _validate_ttl(ttl: timedelta) -> None:
        if ttl <= timedelta(0):
            raise ValueError("lease ttl must be positive")

    def acquire(self, resource: str, owner: str, ttl: timedelta) -> Lease:
        self._validate_ttl(ttl)
        with self._engine.begin() as connection:
            now = self._clock.now_utc(connection)
            expires_at = now + ttl
            inserted_token = connection.execute(
                _insert_do_nothing(
                    connection,
                    platform_lease_table,
                    {
                        "resource": resource,
                        "owner": owner,
                        "fence_token": 1,
                        "expires_at_utc": expires_at,
                        "updated_at_utc": now,
                    },
                    ["resource"],
                ).returning(platform_lease_table.c.fence_token)
            ).scalar_one_or_none()
            if inserted_token is not None:
                return Lease(resource, owner, int(inserted_token), expires_at)

            updated = connection.execute(
                update(platform_lease_table)
                .where(
                    and_(
                        platform_lease_table.c.resource == resource,
                        platform_lease_table.c.expires_at_utc <= now,
                    )
                )
                .values(
                    owner=owner,
                    fence_token=platform_lease_table.c.fence_token + 1,
                    expires_at_utc=expires_at,
                    updated_at_utc=now,
                )
            ).rowcount
            if updated != 1:
                raise LeaseUnavailable(f"lease is held for {resource}")
            token = connection.execute(
                select(platform_lease_table.c.fence_token).where(
                    platform_lease_table.c.resource == resource
                )
            ).scalar_one()
            return Lease(resource, owner, int(token), expires_at)

    def renew(self, lease: Lease, ttl: timedelta) -> Lease:
        self._validate_ttl(ttl)
        with self._engine.begin() as connection:
            now = self._clock.now_utc(connection)
            expires_at = now + ttl
            renewed = connection.execute(
                update(platform_lease_table)
                .where(
                    and_(
                        platform_lease_table.c.resource == lease.resource,
                        platform_lease_table.c.owner == lease.owner,
                        platform_lease_table.c.fence_token == lease.fence_token,
                        platform_lease_table.c.expires_at_utc > now,
                    )
                )
                .values(expires_at_utc=expires_at, updated_at_utc=now)
            ).rowcount
            if renewed != 1:
                raise FenceViolation(f"stale fence for {lease.resource}")
            return Lease(lease.resource, lease.owner, lease.fence_token, expires_at)

    def assert_fence(self, resource: str, owner: str, fence_token: int) -> None:
        with self._engine.connect() as connection:
            now = self._clock.now_utc(connection)
            record = (
                connection.execute(
                    select(
                        platform_lease_table.c.owner,
                        platform_lease_table.c.fence_token,
                        platform_lease_table.c.expires_at_utc,
                    ).where(platform_lease_table.c.resource == resource)
                )
                .mappings()
                .one_or_none()
            )
        if (
            record is None
            or record["owner"] != owner
            or int(record["fence_token"]) != fence_token
            or _as_utc(record["expires_at_utc"]) <= now
        ):
            raise FenceViolation(f"stale fence for {resource}")

    @contextmanager
    def fenced_transaction(self, lease: Lease) -> Iterator[Connection]:
        """Commit a callback's writes only while its lease token remains current."""

        with self._engine.begin() as connection:
            now = self._clock.now_utc(connection)
            active = connection.execute(
                select(platform_lease_table.c.resource).where(
                    and_(
                        platform_lease_table.c.resource == lease.resource,
                        platform_lease_table.c.owner == lease.owner,
                        platform_lease_table.c.fence_token == lease.fence_token,
                        platform_lease_table.c.expires_at_utc > now,
                    )
                )
            ).scalar_one_or_none()
            if active is None:
                raise FenceViolation(f"stale fence for {lease.resource}")
            yield connection
            current = self._clock.now_utc(connection)
            kept = connection.execute(
                update(platform_lease_table)
                .where(
                    and_(
                        platform_lease_table.c.resource == lease.resource,
                        platform_lease_table.c.owner == lease.owner,
                        platform_lease_table.c.fence_token == lease.fence_token,
                        platform_lease_table.c.expires_at_utc > current,
                    )
                )
                .values(updated_at_utc=current)
            ).rowcount
            if kept != 1:
                raise FenceViolation(f"stale fence for {lease.resource}")

    def write_with_fence(self, lease: Lease, operation: Callable[[Connection], _Result]) -> _Result:
        with self.fenced_transaction(lease) as connection:
            return operation(connection)
