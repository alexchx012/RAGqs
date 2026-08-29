from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.pool import StaticPool

from app.documents import worker as worker_module
from app.documents.indexing import NoopIndexingHandoff
from app.documents.maintenance import run_documents_maintenance_once
from app.documents.schema import (
    documents_metadata,
    ingestion_attempts_table,
    ingestion_jobs_table,
    knowledge_submissions_table,
)
from app.documents.service import DocumentsService, DocumentUpload
from app.documents.worker import IngestionWorker, LeaseHeartbeat
from app.identity.service import AuthPrincipal
from app.indexing.service import IndexingService
from app.platform.config import load_platform_settings
from app.platform.database import core_metadata
from app.platform.errors import PlatformError
from app.platform.persistence import MemoryLeaseStore
from app.platform.runtime import PlatformRuntime
from app.platform.storage import MemoryObjectStore, StorageKeyError
from app.platform.worker import WorkerRuntime

NOW = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)


class _Identity:
    def authorize_space(self, *, principal, space_id: str, action: str) -> str:
        del principal, space_id, action
        return "manage"


def _settings():
    return load_platform_settings(
        {
            "RAG_PLATFORM_PROFILE": "development",
            "RAG_DATABASE_URL": "sqlite+pysqlite:///:memory:",
            "RAG_OBJECT_STORAGE_ENDPOINT": "http://localhost:9000",
            "RAG_OBJECT_STORAGE_BUCKET": "rag-dev",
            "RAG_PROVIDER_NAME": "fake",
            "RAG_BUSINESS_TIMEZONE": "UTC",
            "RAG_MAINTENANCE_KEY": "documents-maintenance-test-key",
        }
    )


def _runtime(*, now=lambda: NOW):
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    core_metadata.create_all(engine)
    documents_metadata.create_all(engine)
    store = MemoryObjectStore()
    docs = DocumentsService(
        engine,
        now=now,
        object_store=store,
        identity_access=_Identity(),
        indexing_handoff_port=NoopIndexingHandoff(),
    )
    indexing = IndexingService()
    docs._indexing_handoff_port = indexing
    settings = _settings()
    runtime = PlatformRuntime(
        settings,
        adapters={
            "database_engine": engine,
            "database_clock": type("Clock", (), {"now_utc": lambda self, connection=None: now()})(),
            "documents_service": docs,
            "indexing_service": indexing,
        },
    )
    leases = MemoryLeaseStore(now)
    worker_runtime = WorkerRuntime(runtime, leases, now, owns_runtime=False)
    return engine, docs, worker_runtime


def test_ingestion_worker_processes_and_publishes_text_upload() -> None:
    engine, docs, worker_runtime = _runtime()
    principal = AuthPrincipal("u1", "s1", "alice", "user", None)
    result = docs.create_initial_upload(
        principal=principal,
        space_id="space-1",
        files=[DocumentUpload("guide.txt", b"hello worker", "text/plain")],
        idempotency_key="upload-1",
    )

    stats = IngestionWorker(worker_runtime).run_once(owner="ingestion:test:1", limit=1)

    assert stats.claimed == 1
    assert stats.succeeded == 1
    assert stats.failed == 0
    with engine.connect() as connection:
        job = connection.execute(select(ingestion_jobs_table)).mappings().one()
        attempt = connection.execute(select(ingestion_attempts_table)).mappings().one()
    assert job["state"] == "succeeded"
    assert attempt["state"] == "succeeded"
    assert docs.list_documents(principal=principal, space_id="space-1")["total"] == 1
    assert result["items"][0]["job_id"] == job["id"]


def test_job_lease_renewal_requires_current_owner_and_fence() -> None:
    _engine, docs, _worker_runtime = _runtime()
    principal = AuthPrincipal("u1", "s1", "alice", "user", None)
    result = docs.create_initial_upload(
        principal=principal,
        space_id="space-1",
        files=[DocumentUpload("guide.txt", b"hello", "text/plain")],
        idempotency_key="upload-2",
    )
    lease = docs.claim_job(worker_id="ingestion:test:1", job_id=result["items"][0]["job_id"])
    renewed = docs.renew_job_lease(
        lease,
        worker_id="ingestion:test:1",
        lease_ttl=timedelta(minutes=5),
    )
    assert renewed is not None
    assert renewed.lease_expires_at >= lease.lease_expires_at
    assert docs.renew_job_lease(lease, worker_id="other", lease_ttl=timedelta(minutes=5)) is None


def test_heartbeat_renewal_prevents_reclaim_after_the_original_lease_expiry() -> None:
    clock = [NOW]
    _engine, docs, _worker_runtime = _runtime(now=lambda: clock[0])
    principal = AuthPrincipal("u1", "s1", "alice", "user", None)
    result = docs.create_initial_upload(
        principal=principal,
        space_id="space-1",
        files=[DocumentUpload("guide.txt", b"hello", "text/plain")],
        idempotency_key="upload-heartbeat",
    )
    lease = docs.claim_job(
        worker_id="ingestion:test:1",
        job_id=result["items"][0]["job_id"],
        lease_ttl=timedelta(seconds=5),
    )
    heartbeat = LeaseHeartbeat(
        docs,
        lease,
        "ingestion:test:1",
        lease_ttl=timedelta(seconds=5),
    )
    clock[0] += timedelta(seconds=4)
    renewed = heartbeat.beat()
    assert renewed is not None
    clock[0] += timedelta(seconds=2)

    with pytest.raises(PlatformError) as unavailable:
        docs.claim_job(
            worker_id="ingestion:test:2",
            job_id=result["items"][0]["job_id"],
            lease_ttl=timedelta(seconds=5),
        )

    assert unavailable.value.code == "job_unavailable"


def test_ingestion_default_owner_is_bounded_for_a_long_hostname(monkeypatch) -> None:
    monkeypatch.setattr(worker_module.socket, "gethostname", lambda: "h" * 500)
    monkeypatch.setattr(worker_module.os, "getpid", lambda: 123)

    owner = worker_module._default_owner()

    assert owner.startswith("ingestion:")
    assert owner.endswith(":123")
    assert len(owner) == 128


def test_documents_maintenance_cleans_withdrawn_submission_idempotently() -> None:
    engine, docs, worker_runtime = _runtime()
    principal = AuthPrincipal("u1", "s1", "alice", "user", None)
    submission = docs.create_submission(
        principal=principal,
        space_id="space-1",
        file=DocumentUpload("guide.txt", b"private submission", "text/plain"),
        idempotency_key="submission-1",
    )
    withdrawn = docs.withdraw_submission(
        principal=principal,
        submission_id=submission["submission_id"],
        expected_version=1,
        idempotency_key="withdraw-1",
    )
    assert withdrawn["status"] == "withdrawn"
    with engine.connect() as connection:
        private_key = connection.execute(
            select(knowledge_submissions_table.c.private_object_key).where(
                knowledge_submissions_table.c.id == submission["submission_id"]
            )
        ).scalar_one()

    first = run_documents_maintenance_once(
        worker_runtime.runtime.settings, runtime=worker_runtime.runtime
    )

    assert first.cleaned == 1
    with pytest.raises(StorageKeyError):
        docs._object_store.get(private_key)
    with engine.connect() as connection:
        cleaned_at = connection.execute(
            select(knowledge_submissions_table.c.private_object_cleaned_at_utc).where(
                knowledge_submissions_table.c.id == submission["submission_id"]
            )
        ).scalar_one()
    assert cleaned_at is not None
    assert (
        run_documents_maintenance_once(
            worker_runtime.runtime.settings, runtime=worker_runtime.runtime
        ).cleaned
        == 0
    )
