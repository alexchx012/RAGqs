"""Retention maintenance worker tests."""

from __future__ import annotations

from retention_helpers import build_engine, fixed_now, make_settings

from app.chat.schema import chat_metadata
from app.documents.schema import documents_metadata
from app.identity.schema import identity_metadata
from app.indexing.schema import indexing_metadata
from app.platform.database import (
    SqlAlchemyDatabaseClock,
    SqlAlchemyLeaseStore,
    core_metadata,
    platform_lease_table,
)
from app.platform.errors import PlatformError
from app.platform.runtime import PlatformRuntime
from app.platform.worker import WorkerRuntime
from app.retention.repository import SqlAlchemyRetentionRepository
from app.retention.schema import retention_metadata
from app.retention.service import RetentionOpsService
from app.retention.worker import RetentionMaintenanceWorker
from app.usage.schema import usage_metadata


class FakeReader:
    def dashboard(self, *, role: str, window: str) -> dict:
        del role, window
        return {}

    def operations(self, *, window: str) -> dict:
        del window
        return {}


class FakeOpsJobs:
    def jobs(self, *, principal, view: str) -> dict:
        del principal, view
        return {"items": [], "stale_count": 0}


class FakeDocumentsPort:
    def __init__(self) -> None:
        self.purged: list[str] = []
        self.finalized: int = 0

    def purge_retained_versions(self, *, limit: int = 100) -> list[str]:
        del limit
        return self.purged

    def finalize_deletion(self, *, document_id: str, deletion_id: str) -> dict:
        del document_id, deletion_id
        self.finalized += 1
        return {"state": "deleted"}


class FakeReconciliation:
    def run(self, *, scope: str, limit: int = 100) -> dict:
        return {"run_id": f"run:{scope}", "counts": {"info": 0, "repairable": 0, "blocking": 0}}

    def repair_generation_gc(self, *, finding_id: str, candidate_generation_id: str) -> dict:
        return {"finding_id": finding_id, "state": "repaired"}


class FakeGc:
    def handoff(self, **kwargs) -> dict:
        del kwargs
        return {"state": "purged"}


class FakeCompaction:
    def request_once(self, **kwargs) -> dict:
        del kwargs
        return {"state": "completed"}


def _service_and_runtime(engine, service):
    runtime = PlatformRuntime(
        settings=make_settings(),
        adapters={
            "retention_ops": service,
            "database_engine": engine,
        },
    )
    clock = SqlAlchemyDatabaseClock(engine)
    leases = SqlAlchemyLeaseStore(engine, clock)
    worker_runtime = WorkerRuntime(
        runtime=runtime, leases=leases, now=fixed_now, owns_runtime=False
    )
    return runtime, worker_runtime


def _service(engine):
    repository = SqlAlchemyRetentionRepository(engine, now=lambda connection=None: fixed_now())
    return RetentionOpsService(
        repository=repository,
        dashboard=FakeReader(),
        ops_jobs=FakeOpsJobs(),
        reconciliation=FakeReconciliation(),
        gc_coordinator=FakeGc(),
        compaction=FakeCompaction(),
        engine=engine,
        documents_cleanup_port=FakeDocumentsPort(),
    )


def _create_tables(engine):
    for metadata in (
        core_metadata,
        identity_metadata,
        documents_metadata,
        usage_metadata,
        indexing_metadata,
        chat_metadata,
        retention_metadata,
    ):
        metadata.create_all(engine)


def test_worker_runs_all_six_steps_under_leases() -> None:
    engine = build_engine()
    _create_tables(engine)
    service = _service(engine)
    runtime, worker_runtime = _service_and_runtime(engine, service)
    worker = RetentionMaintenanceWorker(worker_runtime)
    stats = worker.run_once(owner="worker-1")
    assert stats.completed == 6
    assert stats.deferred == 0
    with engine.connect() as connection:
        rows = connection.execute(platform_lease_table.select()).mappings()
        leases = [dict(row) for row in rows]
    assert len(leases) == 6
    worker_runtime.close()
    runtime.close()


class _FailingService(RetentionOpsService):
    def purge_due_versions(self, *, limit: int = 100) -> list[str]:
        del limit
        raise PlatformError("deletion_cleanup_blocked", "blocked", {}, 409)


def _failing_service(engine):
    repository = SqlAlchemyRetentionRepository(engine, now=lambda connection=None: fixed_now())
    return _FailingService(
        repository=repository,
        dashboard=FakeReader(),
        ops_jobs=FakeOpsJobs(),
        reconciliation=FakeReconciliation(),
        gc_coordinator=FakeGc(),
        compaction=FakeCompaction(),
        engine=engine,
        documents_cleanup_port=FakeDocumentsPort(),
    )


def test_failing_step_defers_without_aborting_other_steps() -> None:
    engine = build_engine()
    _create_tables(engine)
    service = _failing_service(engine)
    runtime, worker_runtime = _service_and_runtime(engine, service)
    worker = RetentionMaintenanceWorker(worker_runtime)
    stats = worker.run_once(owner="worker-1")
    assert stats.completed == 5
    assert stats.deferred == 1
    worker_runtime.close()
    runtime.close()
