from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import Column, Integer, MetaData, String, Table, create_engine, select, update
from sqlalchemy.dialects.postgresql import dialect as postgresql_dialect

from app.platform.database import (
    SqlAlchemyDatabaseClock,
    SqlAlchemyLeaseStore,
    SqlAlchemyTransactionManager,
    core_metadata,
    platform_audit_table,
    platform_idempotency_table,
    platform_lease_table,
)
from app.platform.persistence import AuditRecord, FenceViolation, LeaseUnavailable


@pytest.fixture
def engine():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    core_metadata.create_all(engine)
    yield engine
    engine.dispose()


def test_sql_transaction_rolls_back_domain_state_audit_and_idempotency_together(engine) -> None:
    domain_metadata = MetaData()
    documents = Table(
        "test_documents",
        domain_metadata,
        Column("id", Integer, primary_key=True),
        Column("status", String(32), nullable=False),
    )
    domain_metadata.create_all(engine)
    manager = SqlAlchemyTransactionManager(engine)

    with pytest.raises(RuntimeError, match="abort"):
        with manager.begin() as transaction:
            transaction.connection.execute(documents.insert().values(id=1, status="created"))
            transaction.audit(
                AuditRecord(
                    actor_id="user-1",
                    resource_type="document",
                    resource_id="doc-1",
                    request_id="req_123",
                    result="created",
                )
            )
            reservation = transaction.reserve_idempotency("document:create", "key-1", "hash-1")
            assert reservation.replayed is False
            transaction.commit_idempotency("document:create", "key-1", "hash-1", {"id": "doc-1"})
            raise RuntimeError("abort")

    with engine.connect() as connection:
        assert connection.execute(select(documents)).all() == []
        assert connection.execute(select(platform_audit_table)).all() == []
        assert connection.execute(select(platform_idempotency_table)).all() == []


def test_sql_transaction_persists_idempotency_result_in_request_scope(engine) -> None:
    manager = SqlAlchemyTransactionManager(engine)

    with manager.begin() as transaction:
        reservation = transaction.reserve_idempotency("document:create", "key-1", "hash-1")
        assert reservation.replayed is False
        transaction.commit_idempotency("document:create", "key-1", "hash-1", {"id": "doc-1"})

    with manager.begin() as transaction:
        replay = transaction.reserve_idempotency("document:create", "key-1", "hash-1")
        assert replay.replayed is True
        assert replay.result == {"id": "doc-1"}

    with manager.begin() as transaction:
        with pytest.raises(RuntimeError, match="idempotency key conflict"):
            transaction.reserve_idempotency("document:create", "key-1", "other-hash")


def test_sql_lease_uses_database_clock_and_rejects_a_stale_fence(engine) -> None:
    clock = SqlAlchemyDatabaseClock(engine)
    leases = SqlAlchemyLeaseStore(engine, clock=clock)

    first = leases.acquire("ingest:doc-1", "worker-a", timedelta(seconds=30))
    assert first.fence_token == 1

    with pytest.raises(LeaseUnavailable):
        leases.acquire("ingest:doc-1", "worker-b", timedelta(seconds=30))

    with engine.begin() as connection:
        connection.execute(
            update(platform_lease_table)
            .where(platform_lease_table.c.resource == "ingest:doc-1")
            .values(expires_at_utc=datetime(2000, 1, 1, tzinfo=UTC))
        )

    second = leases.acquire("ingest:doc-1", "worker-b", timedelta(seconds=30))
    assert second.fence_token == 2
    with pytest.raises(FenceViolation):
        leases.assert_fence("ingest:doc-1", "worker-a", first.fence_token)
    leases.assert_fence("ingest:doc-1", "worker-b", second.fence_token)


def test_database_clock_uses_postgresql_wall_clock_timestamp() -> None:
    observed: list[object] = []

    class PostgresConnection:
        dialect = postgresql_dialect()

        def execute(self, statement):
            observed.append(statement)
            return self

        def scalar_one(self):
            return datetime(2026, 8, 5, 12, 0, tzinfo=UTC)

    clock = SqlAlchemyDatabaseClock(create_engine("sqlite+pysqlite:///:memory:"))
    clock.now_utc(PostgresConnection())

    compiled = str(observed[0].compile(dialect=postgresql_dialect()))
    assert "clock_timestamp()" in compiled


def test_fenced_transaction_rolls_back_domain_write_when_lease_becomes_stale(engine) -> None:
    domain_metadata = MetaData()
    documents = Table(
        "fenced_documents",
        domain_metadata,
        Column("id", Integer, primary_key=True),
        Column("status", String(32), nullable=False),
    )
    domain_metadata.create_all(engine)
    leases = SqlAlchemyLeaseStore(engine)
    lease = leases.acquire("ingest:doc-1", "worker-a", timedelta(seconds=30))

    def write_then_expire(connection) -> None:
        connection.execute(documents.insert().values(id=1, status="indexed"))
        connection.execute(
            update(platform_lease_table)
            .where(platform_lease_table.c.resource == lease.resource)
            .values(expires_at_utc=datetime(2000, 1, 1, tzinfo=UTC))
        )

    with pytest.raises(FenceViolation):
        leases.write_with_fence(lease, write_then_expire)

    with engine.connect() as connection:
        assert connection.execute(select(documents)).all() == []
