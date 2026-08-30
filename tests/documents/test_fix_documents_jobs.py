from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, select, update

from app.documents.indexing import NoopIndexingHandoff
from app.documents.public_graph import PublicGraphSourceService
from app.documents.schema import (
    documents_metadata,
    ingestion_jobs_table,
    knowledge_submissions_table,
    publications_table,
)
from app.documents.service import DocumentsService
from app.identity.service import AuthPrincipal
from app.platform.database import core_metadata
from app.platform.errors import PlatformError
from app.platform.storage import MemoryObjectStore

from .test_commands import _accept, _upload
from .test_public_graph_source import _Lifecycle, _PublicIdentity, _RecordingSourceOutbox


class _FailingSubmissionNotifier:
    def publish_submission_event(self, *, connection, **kwargs) -> str:
        del connection
        raise RuntimeError("injected post-approval transaction failure")


class _GetCountingStore:
    def __init__(self, store) -> None:
        self._store = store
        self.get_keys: list[str] = []

    def __getattr__(self, name):
        return getattr(self._store, name)

    def get(self, key):
        self.get_keys.append(key)
        return self._store.get(key)


class _ConnectionRecordingCleanup(NoopIndexingHandoff):
    def __init__(self) -> None:
        self.cleanup_connections: list[object] = []

    def cleanup_resource(self, resource, *, connection) -> None:
        self.cleanup_connections.append(connection)


def _rejected_reason_row(service, submission_id):
    with service._engine.connect() as connection:
        return connection.execute(
            select(knowledge_submissions_table.c.status).where(
                knowledge_submissions_table.c.id == submission_id
            )
        ).scalar_one()


def test_approve_transaction_failure_keeps_submission_pending_and_object_readable(
    service, principal
) -> None:
    submission = service.create_submission(
        principal=principal,
        space_id="space_1",
        file=_upload(),
        idempotency_key="submission-approve-txn-failure-1",
    )
    reviewer = principal.__class__(
        user_id="user_1",
        auth_session_id="admin-session",
        username="admin",
        role="admin",
        department_id=None,
    )
    service._submission_notification_port = _FailingSubmissionNotifier()

    with pytest.raises(RuntimeError, match="injected post-approval"):
        service.approve_submission(
            principal=reviewer,
            submission_id=submission["submission_id"],
            expected_version=1,
            idempotency_key="approve-txn-failure-1",
        )

    assert _rejected_reason_row(service, submission["submission_id"]) == "pending"
    with service._engine.connect() as connection:
        private_key = connection.execute(
            select(knowledge_submissions_table.c.private_object_key).where(
                knowledge_submissions_table.c.id == submission["submission_id"]
            )
        ).scalar_one()
    content, _ = service._object_store.get(private_key)
    assert content == b"hello"

    service._submission_notification_port = None
    approved = service.approve_submission(
        principal=reviewer,
        submission_id=submission["submission_id"],
        expected_version=1,
        idempotency_key="approve-txn-failure-2",
    )
    assert approved["status"] == "approved"
    # Cleanup runs through the scheduled pass after the transaction committed.
    assert service.cleanup_scheduled_submissions() == [submission["submission_id"]]


def test_review_and_restore_paths_never_read_object_content(service, principal) -> None:
    counting = _GetCountingStore(service._object_store)
    service._object_store = counting

    # Reject path: pure state transition, no object body access.
    submission = service.create_submission(
        principal=principal,
        space_id="space_1",
        file=_upload(),
        idempotency_key="submission-reject-no-read-1",
    )
    reviewer = principal.__class__(
        user_id="user_1",
        auth_session_id="admin-session",
        username="admin",
        role="admin",
        department_id=None,
    )
    rejected = service.reject_submission(
        principal=reviewer,
        submission_id=submission["submission_id"],
        expected_version=1,
        idempotency_key="reject-no-read-1",
        reason="not needed",
    )
    assert rejected["status"] == "rejected"
    assert counting.get_keys == []

    # Restore path: copy + trusted DB hash, no object body read.
    original = service.create_initial_upload(
        principal=principal,
        space_id="space_1",
        files=[_upload()],
        idempotency_key="upload-restore-no-read-1",
    )["items"][0]
    _accept(service, principal, original)
    replacement = service.replace_version(
        principal=principal,
        document_id=original["document_id"],
        expected_version=1,
        file=_upload(content=b"replacement"),
        idempotency_key="replace-restore-no-read-1",
    )
    _accept(service, principal, replacement)
    counting.get_keys.clear()
    restored = service.restore_version(
        principal=principal,
        document_id=original["document_id"],
        document_version_id=original["document_version_id"],
        expected_version=2,
        idempotency_key="restore-no-read-1",
    )
    assert restored["status"] == "pending"
    assert counting.get_keys == []


def test_cleanup_external_calls_run_without_database_transaction(service, principal) -> None:
    recording = _ConnectionRecordingCleanup()
    service._indexing_handoff_port = recording
    service._lifecycle_port = _Lifecycle()
    locked_connections: list[object] = []
    original_locked = DocumentsService._locked_document

    def recording_locked(connection, document_id):
        locked_connections.append(connection)
        return original_locked(connection, document_id)

    item = service.create_initial_upload(
        principal=principal,
        space_id="space_1",
        files=[_upload()],
        idempotency_key="upload-cleanup-txn-1",
    )["items"][0]
    _accept(
        service,
        principal,
        item,
        stage_resources=[{"backend_kind": "index", "resource_id": "index-cleanup-txn"}],
    )
    deletion = service.delete_document(
        principal=principal,
        document_id=item["document_id"],
        expected_version=1,
        idempotency_key="delete-cleanup-txn-1",
    )

    DocumentsService._locked_document = staticmethod(recording_locked)
    try:
        result = service.finalize_deletion(
            document_id=item["document_id"], deletion_id=deletion["deletion_id"]
        )
    finally:
        DocumentsService._locked_document = staticmethod(original_locked)

    assert result == {"document_id": item["document_id"], "state": "deleted"}
    assert recording.cleanup_connections, "derived cleanup targets must be executed"
    assert all(
        conn is None for conn in recording.cleanup_connections
    ), "external cleanup must run without any database transaction"


def test_retry_budget_resets_per_replay_cycle(service, principal) -> None:
    service._identity_access = None

    class _MutableGeneration:
        def __init__(self) -> None:
            self.active_generation_id = "gen-1"

    class _GenerationalHandoff(NoopIndexingHandoff):
        def __init__(self) -> None:
            self.generation = _MutableGeneration()

    handoff = _GenerationalHandoff()
    service._indexing_handoff_port = handoff
    cycle = {"n": 1}

    def bump_generation() -> None:
        cycle["n"] += 1
        handoff.generation.active_generation_id = f"gen-{cycle['n']}"

    clock = {"now": datetime(2026, 1, 1, tzinfo=UTC)}

    def advancing_now():
        clock["now"] += timedelta(minutes=45)
        return clock["now"]

    service._now = advancing_now
    ops = principal.__class__(
        user_id="ops_1",
        auth_session_id="ops-session",
        username="ops",
        role="ops",
        department_id=None,
    )
    created = service.create_initial_upload(
        principal=principal,
        space_id="space_1",
        files=[_upload()],
        idempotency_key="upload-retry-budget-1",
    )["items"][0]

    for expected_state in ("retry_wait", "retry_wait", "retry_wait", "dead_letter"):
        bump_generation()
        lease = service.claim_job(worker_id="worker-budget", job_id=created["job_id"])
        failed = service.fail_job(
            job_id=created["job_id"],
            reason="transient outage",
            retryable=True,
            attempt_id=lease.attempt_id,
            fencing_token=lease.fencing_token,
        )
        assert failed["state"] == expected_state

    bump_generation()
    replayed = service.replay_job(
        principal=ops,
        job_id=created["job_id"],
        idempotency_key="replay-retry-budget-1",
    )
    assert replayed["replay_generation"] == 1
    bump_generation()
    lease = service.claim_job(worker_id="worker-budget", job_id=created["job_id"])
    failed = service.fail_job(
        job_id=created["job_id"],
        reason="transient outage",
        retryable=True,
        attempt_id=lease.attempt_id,
        fencing_token=lease.fencing_token,
    )
    assert failed["state"] == "retry_wait"
    with service._engine.connect() as connection:
        next_attempt = connection.execute(
            select(ingestion_jobs_table.c.next_attempt_at_utc).where(
                ingestion_jobs_table.c.id == created["job_id"]
            )
        ).scalar_one()
    # First failure of the new replay cycle uses the shortest backoff (~1 minute),
    # proving the budget was reset instead of counting historical attempts.
    next_attempt = next_attempt.replace(tzinfo=UTC)
    assert clock["now"] < next_attempt <= clock["now"] + timedelta(minutes=2)


def test_lease_expiry_resets_retry_budget_per_replay_cycle(service, principal) -> None:
    service._identity_access = None

    class _MutableGeneration:
        def __init__(self) -> None:
            self.active_generation_id = "gen-0"

    class _GenerationalHandoff(NoopIndexingHandoff):
        def __init__(self) -> None:
            self.generation = _MutableGeneration()

    handoff = _GenerationalHandoff()
    service._indexing_handoff_port = handoff
    clock = {"now": datetime(2026, 1, 1, tzinfo=UTC)}
    service._now = lambda: clock["now"]
    ops = principal.__class__(
        user_id="ops_1",
        auth_session_id="ops-session",
        username="ops",
        role="ops",
        department_id=None,
    )
    created = service.create_initial_upload(
        principal=principal,
        space_id="space_1",
        files=[_upload()],
        idempotency_key="upload-lease-retry-budget-1",
    )["items"][0]

    for attempt_number, expected_state in enumerate(
        ("retry_wait", "retry_wait", "retry_wait", "dead_letter"), start=1
    ):
        handoff.generation.active_generation_id = f"old-{attempt_number}"
        lease = service.claim_job(worker_id="worker-expired", job_id=created["job_id"])
        failed = service.fail_job(
            job_id=created["job_id"],
            reason="transient outage",
            retryable=True,
            attempt_id=lease.attempt_id,
            fencing_token=lease.fencing_token,
        )
        assert failed["state"] == expected_state
        clock["now"] += timedelta(hours=1)

    replayed = service.replay_job(
        principal=ops,
        job_id=created["job_id"],
        idempotency_key="replay-lease-retry-budget-1",
    )
    assert replayed["replay_generation"] == 1
    handoff.generation.active_generation_id = "new-cycle"
    service.claim_job(
        worker_id="worker-expired",
        job_id=created["job_id"],
        lease_ttl=timedelta(seconds=1),
    )
    clock["now"] += timedelta(minutes=2)

    with pytest.raises(PlatformError) as error:
        service.claim_job(worker_id="worker-expired", job_id=created["job_id"])
    assert error.value.code == "job_unavailable"
    with service._engine.connect() as connection:
        state = connection.execute(
            select(ingestion_jobs_table.c.state).where(
                ingestion_jobs_table.c.id == created["job_id"]
            )
        ).scalar_one()
    assert state == "retry_wait"


def test_public_space_with_corrupt_manifest_row_still_publishes_and_deletes() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    core_metadata.create_all(engine)

    documents_metadata.create_all(engine)
    source_outbox = _RecordingSourceOutbox()
    source = PublicGraphSourceService(engine, outbox_port=source_outbox)
    service = DocumentsService(
        engine,
        now=lambda: datetime(2026, 1, 1, tzinfo=UTC),
        object_store=MemoryObjectStore(),
        identity_access=_PublicIdentity(),
        indexing_handoff_port=NoopIndexingHandoff(),
        public_graph_source_service=source,
    )
    principal = AuthPrincipal(
        user_id="user_1",
        auth_session_id="session_1",
        username="alice",
        role="user",
        department_id=None,
    )
    good = service.create_initial_upload(
        principal=principal,
        space_id="public",
        files=[_upload(name="good.txt")],
        idempotency_key="upload-public-good-1",
    )["items"][0]
    _accept(service, principal, good)
    bad = service.create_initial_upload(
        principal=principal,
        space_id="public",
        files=[_upload(name="bad.txt", content=b"bad")],
        idempotency_key="upload-public-bad-1",
    )["items"][0]
    _accept(service, principal, bad)

    # Corrupt one historical row: strip its immutable content manifest identity.
    with engine.begin() as connection:
        connection.execute(
            update(publications_table)
            .where(publications_table.c.id == bad["publication_id"])
            .values(resource_manifest_json={"content_manifest_id": "manifest-bad"})
        )

    # Publishing and deleting other public documents must no longer 409 globally.
    third = service.create_initial_upload(
        principal=principal,
        space_id="public",
        files=[_upload(name="third.txt", content=b"third")],
        idempotency_key="upload-public-third-1",
    )["items"][0]
    _accept(service, principal, third)
    service._lifecycle_port = _Lifecycle()
    service.delete_document(
        principal=principal,
        document_id=good["document_id"],
        expected_version=1,
        idempotency_key="delete-public-good-1",
    )
    with pytest.raises(PlatformError) as error:
        service.delete_document(
            principal=principal,
            document_id=bad["document_id"],
            expected_version=1,
            idempotency_key="delete-public-bad-1",
        )
    assert error.value.code == "public_source_manifest_invalid"
    assert error.value.status_code == 409
    assert error.value.details == {"document_id": bad["document_id"]}
    snapshot = source.get_snapshot(source_revision=source.get_current_head().source_revision)
    assert bad["document_id"] not in {item["document_id"] for item in snapshot.publications}


def test_list_approvals_filters_spaces_in_sql() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    core_metadata.create_all(engine)

    documents_metadata.create_all(engine)

    class _PermissiveIdentity:
        def authorize_space(self, *, principal, space_id: str, action: str, connection=None) -> str:
            del principal, action
            return "manage"

    service = DocumentsService(
        engine,
        now=lambda: datetime(2026, 1, 1, tzinfo=UTC),
        object_store=MemoryObjectStore(),
        identity_access=_PermissiveIdentity(),
    )
    submitter = AuthPrincipal(
        user_id="user_1",
        auth_session_id="session_1",
        username="alice",
        role="user",
        department_id=None,
    )
    for space in ("public", "department:d1", "space_1"):
        service.create_submission(
            principal=submitter,
            space_id=space,
            file=_upload(name=f"{space}.txt"),
            idempotency_key=f"submission-approvals-{space}",
        )

    admin = AuthPrincipal(
        user_id="admin_1",
        auth_session_id="admin-session",
        username="admin",
        role="admin",
        department_id=None,
    )
    admin_items = service.list_approval_submissions(principal=admin)["items"]
    assert {item["target_space_id"] for item in admin_items} == {"public", "department:d1"}
    # admin 不可见范围外的空间（如 personal/space_1）不会出现在审核列表。
    assert all(
        item["target_space_id"] == "public" or item["target_space_id"].startswith("department:")
        for item in admin_items
    )
    kind_items = service.list_approval_submissions(
        principal=admin, target_kind="department"
    )["items"]
    assert [item["target_space_id"] for item in kind_items] == ["department:d1"]

    ops = AuthPrincipal(
        user_id="ops_1",
        auth_session_id="ops-session",
        username="ops",
        role="ops",
        department_id=None,
    )
    ops_items = service.list_approval_submissions(principal=ops)["items"]
    assert [item["target_space_id"] for item in ops_items] == ["public"]

    minister = AuthPrincipal(
        user_id="minister_1",
        auth_session_id="minister-session",
        username="minister",
        role="minister",
        department_id="d1",
    )
    minister_items = service.list_approval_submissions(principal=minister)["items"]
    assert [item["target_space_id"] for item in minister_items] == ["department:d1"]

    with pytest.raises(PlatformError) as error:
        service.list_approval_submissions(principal=submitter)
    assert error.value.code == "approval_forbidden"
