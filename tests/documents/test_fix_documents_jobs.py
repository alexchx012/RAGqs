from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, func, select, update
from sqlalchemy.pool import StaticPool

from app.documents.indexing import NoopIndexingHandoff
from app.documents.public_graph import PublicGraphSourceService
from app.documents.schema import (
    documents_metadata,
    ingestion_attempts_table,
    ingestion_jobs_table,
    knowledge_submissions_table,
    publications_table,
    submission_execution_grants_table,
)
from app.documents.service import DocumentsService
from app.documents.worker import IngestionWorker
from app.identity.service import AuthPrincipal
from app.indexing.service import IndexingService
from app.platform.config import load_platform_settings
from app.platform.database import core_metadata
from app.platform.errors import PlatformError
from app.platform.persistence import MemoryLeaseStore
from app.platform.runtime import PlatformRuntime
from app.platform.storage import MemoryObjectStore
from app.platform.worker import WorkerRuntime

from .test_commands import _accept, _upload
from .test_jobs_and_fences import (
    _accepted,
    _Quota,
    _receipt_contract_fields,
    _receipt_request_echoes,
)
from .test_public_graph_source import _Lifecycle, _PublicIdentity, _RecordingSourceOutbox
from .test_upload_http import _make_client, _seed_user


class _MutableGeneration:
    def __init__(self, active_generation_id: str = "gen-1") -> None:
        self.active_generation_id = active_generation_id


class _GenerationalHandoff(NoopIndexingHandoff):
    def __init__(self) -> None:
        self.generation = _MutableGeneration()


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
        expected_version=2,
        file=_upload(content=b"replacement"),
        idempotency_key="replace-restore-no-read-1",
    )
    _accept(service, principal, replacement)
    counting.get_keys.clear()
    restored = service.restore_version(
        principal=principal,
        document_id=original["document_id"],
        document_version_id=original["document_version_id"],
        expected_version=4,
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
        expected_version=2,
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

    handoff = _GenerationalHandoff()
    handoff.generation.active_generation_id = "gen-0"
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
        expected_version=2,
        idempotency_key="delete-public-good-1",
    )
    with pytest.raises(PlatformError) as error:
        service.delete_document(
            principal=principal,
            document_id=bad["document_id"],
            expected_version=2,
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
    kind_items = service.list_approval_submissions(principal=admin, target_kind="department")[
        "items"
    ]
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


# ---------------------------------------------------------------------------
# 2026-09 审计修复（documents-jobs 批次）：
# A7 取消角色矩阵 / A8 usage.images / A50 个人库账号终态 / A53 重放配置快照 /
# A55 投稿响应占位键 / A56 target_kind 契约 / A57 审核幂等 / A65 重试窗口 /
# A72 processing_summary 暴露面收敛。
# ---------------------------------------------------------------------------


class _SpaceMatrixIdentity:
    """按 identity 服务的空间权限矩阵回答 authorize_space（A7 取消矩阵）。"""

    def authorize_space(self, *, principal, space_id: str, action: str, connection=None) -> str:
        del connection
        role = str(getattr(principal, "role", "user"))
        permission: str | None = None
        if space_id.startswith("personal:"):
            if space_id == f"personal:{principal.user_id}":
                permission = "manage"
            elif role in {"ops", "admin"}:
                permission = "read"
        elif space_id == "public":
            permission = "manage" if role in {"ops", "admin"} else "contribute"
        elif space_id.startswith("department:"):
            own = space_id == f"department:{getattr(principal, 'department_id', None)}"
            if role in {"ops", "admin"} or (own and role == "minister"):
                permission = "manage"
            elif own:
                permission = "contribute"
        if permission is None:
            raise PlatformError("space_not_found", "Space was not found", {}, 404)
        rank = {"read": 0, "contribute": 1, "manage": 2}
        if rank[permission] < rank[action]:
            raise PlatformError("space_action_forbidden", "Space action is not allowed", {}, 403)
        return permission


def _principal(role: str, *, user_id: str = "user_1", department_id: str | None = None):
    return AuthPrincipal(
        user_id=user_id,
        auth_session_id=f"session-{role}-{user_id}",
        username=f"{role}-{user_id}",
        role=role,
        department_id=department_id,
    )


def _direct_job(service, principal, *, space_id: str, name: str, key: str) -> dict:
    return service.create_initial_upload(
        principal=principal,
        space_id=space_id,
        files=[_upload(name=name)],
        idempotency_key=key,
    )["items"][0]


def test_cancel_acl_lets_creator_cancel_own_job_after_losing_manage(service, principal) -> None:
    service._identity_access = _SpaceMatrixIdentity()
    minister = _principal("minister", department_id="d1")
    item = _direct_job(
        service,
        minister,
        space_id="department:d1",
        name="creator-lose-manage.txt",
        key="cancel-creator-1",
    )

    # 角色变动后失去部门 manage：仍是原请求操作者，按矩阵可取消自己发起的 job。
    demoted = _principal("user", department_id="d1")
    listed = service.list_jobs(principal=demoted, space_id="department:d1")["items"][0]
    assert listed["allowed_actions"] == ["cancel"]
    cancelled = service.cancel_job(principal=demoted, job_id=item["job_id"])
    assert cancelled["state"] == "cancelled"


def test_cancel_acl_limits_admin_to_own_jobs_even_with_manage(service, principal) -> None:
    service._identity_access = _SpaceMatrixIdentity()
    minister = _principal("minister", department_id="d1")
    others = _direct_job(
        service, minister, space_id="department:d1", name="admin-scope.txt", key="cancel-admin-1"
    )
    admin = _principal("admin", user_id="admin_1")

    # admin 对部门空间持有 manage，也不能取消他人发起的非投稿 job。
    with pytest.raises(PlatformError) as error:
        service.cancel_job(principal=admin, job_id=others["job_id"])
    assert error.value.code == "space_action_forbidden"
    assert error.value.status_code == 403
    listed = service.list_jobs(principal=admin, space_id="department:d1")["items"][0]
    assert listed["allowed_actions"] == []

    # 自己发起的 job 仍可取消。
    own = _direct_job(
        service, admin, space_id="department:d1", name="admin-own.txt", key="cancel-admin-own-1"
    )
    cancelled = service.cancel_job(principal=admin, job_id=own["job_id"])
    assert cancelled["state"] == "cancelled"


def test_cancel_acl_keeps_space_manage_derivation_for_ops_and_minister(service, principal) -> None:
    service._identity_access = _SpaceMatrixIdentity()
    uploader = _principal("minister", department_id="d1")
    ops_job = _direct_job(
        service, uploader, space_id="department:d1", name="ops-cancel.txt", key="cancel-ops-1"
    )

    cancelled = service.cancel_job(
        principal=_principal("ops", user_id="ops_1"), job_id=ops_job["job_id"]
    )
    assert cancelled["state"] == "cancelled"

    minister_job = _direct_job(
        service,
        uploader,
        space_id="department:d1",
        name="minister-cancel.txt",
        key="cancel-minister-1",
    )
    cancelled = service.cancel_job(
        principal=_principal("minister", user_id="minister_d1", department_id="d1"),
        job_id=minister_job["job_id"],
    )
    assert cancelled["state"] == "cancelled"

    foreign_job = _direct_job(
        service,
        uploader,
        space_id="department:d1",
        name="foreign-minister.txt",
        key="cancel-foreign-1",
    )
    with pytest.raises(PlatformError) as error:
        service.cancel_job(
            principal=_principal("minister", user_id="minister_d2", department_id="d2"),
            job_id=foreign_job["job_id"],
        )
    assert error.value.code == "space_action_forbidden"


def test_cancel_acl_submission_jobs_require_current_manage(service, principal) -> None:
    service._identity_access = _SpaceMatrixIdentity()
    submitter = _principal("user", department_id="d1")
    submission = service.create_submission(
        principal=submitter,
        space_id="department:d1",
        file=_upload(name="submission-cancel.txt"),
        idempotency_key="cancel-submission-create-1",
    )
    reviewer = _principal("minister", user_id="minister_d1", department_id="d1")
    approved = service.approve_submission(
        principal=reviewer,
        submission_id=submission["submission_id"],
        expected_version=1,
        idempotency_key="cancel-submission-approve-1",
    )

    # 投稿 job 的 created_by 是投稿者，但仅当前 manage 角色可取消。
    with pytest.raises(PlatformError) as error:
        service.cancel_job(principal=submitter, job_id=approved["job_id"])
    assert error.value.code == "space_action_forbidden"
    assert error.value.status_code == 403

    cancelled = service.cancel_job(principal=reviewer, job_id=approved["job_id"])
    assert cancelled["state"] == "cancelled"


def test_succeeded_job_usage_projects_images(service, principal) -> None:
    service._quota_service = _Quota()
    created = service.create_initial_upload(
        principal=principal,
        space_id="space_1",
        files=[_upload()],
        idempotency_key="upload-usage-images-1",
    )
    item = created["items"][0]
    lease = service.claim_job(worker_id="worker-usage-images", job_id=item["job_id"])
    receipt = {
        "job_id": item["job_id"],
        "attempt_id": lease.attempt_id,
        "fencing_token": lease.fencing_token,
        "publication_id": lease.publication_id,
        "generation_id": lease.expected_generation_id,
        "document_id": item["document_id"],
        "document_version_id": item["document_version_id"],
        "input_content_hash": hashlib.sha256(b"hello").hexdigest(),
        "processing_config_version": "usage-images-v1",
        "authorization_fence": dict(lease.authorization_fence),
        **_receipt_request_echoes(service, lease.attempt_id),
        **_receipt_contract_fields(),
    }
    receipt["processing_summary"] = {
        **receipt["processing_summary"],
        "pages": 3,
        "images": 2,
        "page_count": 3,
        "image_count": 2,
        "processing_list": {
            "processing_list_id": f"processing_list:{lease.publication_id}:{lease.attempt_id}",
            "frozen": True,
            "items": [{"chunk_id": "chunk_1", "contextual_retrieval": False}],
        },
    }
    service.accept_processing_receipt(principal=principal, job_id=item["job_id"], receipt=receipt)

    usage = service.list_jobs(principal=principal, space_id="space_1")["items"][0]["usage"]
    # 与 §6.2 文档列表端点同形：pages 为折叠后的计量页数，images 为图片数。
    assert usage["pages"] == 5
    assert usage["images"] == 2


def test_list_jobs_collapses_processing_summary_to_summary_only(service, principal) -> None:
    item = _accepted(service, principal)
    service.create_initial_upload(
        principal=principal,
        space_id="space_1",
        files=[_upload(name="pending-summary.txt", content=b"pending")],
        idempotency_key="upload-summary-collapse-1",
    )["items"][0]

    items = service.list_jobs(principal=principal, space_id="space_1")["items"]
    by_state = {entry["state"]: entry for entry in items}
    summary = by_state["succeeded"]["processing_summary"]
    assert summary["chunk_count"] == 1
    assert summary["page_count"] == 1
    for internal_receipt_field in (
        "authorization_fence",
        "stage_resources",
        "stage_resource_ids",
        "model_version",
        "model_versions",
        "prompt_version",
        "prompt_versions",
        "locator_snippet_integrity",
        "index_component_results",
        "content_manifest_hash",
        "fencing_token",
    ):
        assert internal_receipt_field not in summary
    assert by_state["pending"]["processing_summary"] == {}

    # 写侧仍保留完整处理回执（读侧聚合由独立子任务负责）。
    with service._engine.connect() as connection:
        stored = connection.execute(
            select(ingestion_jobs_table.c.processing_summary_json).where(
                ingestion_jobs_table.c.id == item["job_id"]
            )
        ).scalar_one()
    assert "authorization_fence" in stored
    assert "stage_resources" in stored


def test_claim_clears_next_attempt_and_projection_keeps_retry_window_only(
    service, principal
) -> None:
    clock = {"now": datetime(2026, 1, 1, tzinfo=UTC)}
    service._now = lambda: clock["now"]
    item = _direct_job(
        service, principal, space_id="space_1", name="retry-window.txt", key="upload-retry-window-1"
    )
    lease = service.claim_job(worker_id="worker-retry-window", job_id=item["job_id"])
    failed = service.fail_job(
        job_id=item["job_id"],
        reason="transient failure",
        retryable=True,
        attempt_id=lease.attempt_id,
        fencing_token=lease.fencing_token,
    )
    assert failed["state"] == "retry_wait"

    retry_wait = service.list_jobs(principal=principal, space_id="space_1")["items"][0]
    assert retry_wait["next_attempt_at"] is not None

    clock["now"] += timedelta(minutes=45)
    service.claim_job(worker_id="worker-retry-window", job_id=item["job_id"])
    with service._engine.connect() as connection:
        job_state, next_attempt = connection.execute(
            select(ingestion_jobs_table.c.state, ingestion_jobs_table.c.next_attempt_at_utc).where(
                ingestion_jobs_table.c.id == item["job_id"]
            )
        ).one()
    assert job_state == "running"
    assert next_attempt is None

    running = service.list_jobs(principal=principal, space_id="space_1")["items"][0]
    assert running["state"] == "running"
    assert running["next_attempt_at"] is None


def test_replay_freezes_processing_config_snapshot_shared_by_generation(service, principal) -> None:
    service._identity_access = None
    clock = {"now": datetime(2026, 1, 1, tzinfo=UTC)}
    service._now = lambda: clock["now"]
    # 多次失败/重放会为同一版本产生多份 discarded publication；按既有测试
    # 约定用可变代际避免与 uq_publications_version_generation_status 冲突。
    handoff = _GenerationalHandoff()
    service._indexing_handoff_port = handoff
    generation = {"n": 0}

    def bump_generation() -> None:
        generation["n"] += 1
        handoff.generation.active_generation_id = f"gen-replay-config-{generation['n']}"

    item = _direct_job(
        service,
        principal,
        space_id="space_1",
        name="replay-config.txt",
        key="upload-replay-config-1",
    )

    # 初始序列：快照留空、profile 固定 default（处理端按媒体画像解析）。
    bump_generation()
    first_lease = service.claim_job(worker_id="worker-replay-config", job_id=item["job_id"])
    service.fail_job(
        job_id=item["job_id"],
        reason="deterministic failure",
        attempt_id=first_lease.attempt_id,
        fencing_token=first_lease.fencing_token,
    )

    bump_generation()
    replayed = service.replay_job(
        principal=_principal("ops", user_id="ops_1"),
        job_id=item["job_id"],
        idempotency_key="replay-config-1",
    )
    assert replayed["replay_generation"] == 1
    frozen = {
        "id": "text",
        "media_category": "text",
        "processing_route": "text-chunking",
        "config_version": "document-profile:text:v1",
    }
    with service._engine.connect() as connection:
        stored = connection.execute(
            select(ingestion_jobs_table.c.replay_config_snapshot_json).where(
                ingestion_jobs_table.c.id == item["job_id"]
            )
        ).scalar_one()
    assert stored == frozen

    bump_generation()
    second_lease = service.claim_job(worker_id="worker-replay-config", job_id=item["job_id"])
    service.fail_job(
        job_id=item["job_id"],
        reason="transient failure",
        retryable=True,
        attempt_id=second_lease.attempt_id,
        fencing_token=second_lease.fencing_token,
    )
    clock["now"] += timedelta(minutes=45)
    bump_generation()
    third_lease = service.claim_job(worker_id="worker-replay-config", job_id=item["job_id"])

    with service._engine.connect() as connection:
        rows = connection.execute(
            select(
                ingestion_attempts_table.c.id, ingestion_attempts_table.c.staging_request_json
            ).where(
                ingestion_attempts_table.c.id.in_(
                    [first_lease.attempt_id, second_lease.attempt_id, third_lease.attempt_id]
                )
            )
        ).all()
    by_attempt = {attempt_id: request for attempt_id, request in rows}
    initial_request = by_attempt[first_lease.attempt_id]
    assert initial_request["processing_config_snapshot"] == {}
    assert initial_request["processing_profile_version"] == "default"
    for replay_request in (
        by_attempt[second_lease.attempt_id],
        by_attempt[third_lease.attempt_id],
    ):
        # 同代 attempt 共用重放事务固化的快照。
        assert replay_request["processing_config_snapshot"] == frozen
        assert replay_request["processing_profile_version"] == "document-profile:text:v1"


class _IdentityHandoff(NoopIndexingHandoff):
    """Handoff exposing the indexing side's model/prompt identity (§2.3)."""

    def __init__(self) -> None:
        self.generation = _MutableGeneration()
        self.identity = {
            "model_version": "frozen-model-v2",
            "prompt_version": "frozen-prompt-v2",
            "cr_model": "ds-v4-flash",
        }

    def processing_identity(self) -> dict[str, str]:
        return dict(self.identity)


def test_replay_freezes_model_and_prompt_identity_across_generation_attempts(
    service, principal
) -> None:
    service._identity_access = None
    clock = {"now": datetime(2026, 1, 1, tzinfo=UTC)}
    service._now = lambda: clock["now"]
    handoff = _IdentityHandoff()
    service._indexing_handoff_port = handoff
    generation = {"n": 0}

    def bump_generation() -> None:
        generation["n"] += 1
        handoff.generation.active_generation_id = f"gen-replay-identity-{generation['n']}"

    item = _direct_job(
        service,
        principal,
        space_id="space_1",
        name="replay-identity.txt",
        key="upload-replay-identity-1",
    )

    # 初始序列：无重放快照，处理端按注入身份执行。
    bump_generation()
    first_lease = service.claim_job(worker_id="worker-replay-identity", job_id=item["job_id"])
    service.fail_job(
        job_id=item["job_id"],
        reason="deterministic failure",
        attempt_id=first_lease.attempt_id,
        fencing_token=first_lease.fencing_token,
    )

    bump_generation()
    replayed = service.replay_job(
        principal=_principal("ops", user_id="ops_1"),
        job_id=item["job_id"],
        idempotency_key="replay-identity-1",
    )
    assert replayed["replay_generation"] == 1
    with service._engine.connect() as connection:
        stored = connection.execute(
            select(ingestion_jobs_table.c.replay_config_snapshot_json).where(
                ingestion_jobs_table.c.id == item["job_id"]
            )
        ).scalar_one()
    # 重放事务把模型 ID/版本与 prompt 版本随处理画像一并冻结。
    assert stored["model_version"] == "frozen-model-v2"
    assert stored["prompt_version"] == "frozen-prompt-v2"
    assert stored["cr_model"] == "ds-v4-flash"
    assert stored["config_version"] == "document-profile:text:v1"

    # 同代两次 claim（重试路径）的 staging request 携带完全一致的冻结身份。
    bump_generation()
    second_lease = service.claim_job(worker_id="worker-replay-identity", job_id=item["job_id"])
    service.fail_job(
        job_id=item["job_id"],
        reason="transient failure",
        retryable=True,
        attempt_id=second_lease.attempt_id,
        fencing_token=second_lease.fencing_token,
    )
    clock["now"] += timedelta(minutes=45)
    bump_generation()
    third_lease = service.claim_job(worker_id="worker-replay-identity", job_id=item["job_id"])

    with service._engine.connect() as connection:
        rows = connection.execute(
            select(
                ingestion_attempts_table.c.id, ingestion_attempts_table.c.staging_request_json
            ).where(
                ingestion_attempts_table.c.id.in_([second_lease.attempt_id, third_lease.attempt_id])
            )
        ).all()
    snapshots = [request["processing_config_snapshot"] for _, request in rows]
    assert snapshots[0] == snapshots[1]
    for snapshot in snapshots:
        assert snapshot["model_version"] == "frozen-model-v2"
        assert snapshot["prompt_version"] == "frozen-prompt-v2"
        assert snapshot["cr_model"] == "ds-v4-flash"


class _LifecycleIdentity:
    def __init__(self, statuses: dict[str, str]) -> None:
        self._statuses = statuses

    def authorize_space(self, *, principal, space_id: str, action: str, connection=None) -> str:
        del principal, space_id, action, connection
        return "manage"

    def account_lifecycle_status(self, user_id: str) -> str:
        return self._statuses.get(str(user_id), "active")


class _SpyIndexingService:
    def __init__(self, inner) -> None:
        self._inner = inner
        self.processed_attempts: list[str] = []

    def process_and_stage(self, request, *args, **kwargs):
        self.processed_attempts.append(str(request.attempt_id))
        return self._inner.process_and_stage(request, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._inner, name)


def _worker_runtime(identity):
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    core_metadata.create_all(engine)
    documents_metadata.create_all(engine)
    indexing = IndexingService()
    docs = DocumentsService(
        engine,
        now=lambda: datetime(2026, 1, 1, tzinfo=UTC),
        object_store=MemoryObjectStore(),
        identity_access=identity,
        indexing_handoff_port=indexing,
    )
    spy = _SpyIndexingService(indexing)
    settings = load_platform_settings(
        {
            "RAG_PLATFORM_PROFILE": "development",
            "RAG_DATABASE_URL": "sqlite+pysqlite:///:memory:",
            "RAG_OBJECT_STORAGE_ENDPOINT": "http://localhost:9000",
            "RAG_OBJECT_STORAGE_BUCKET": "rag-dev",
            "RAG_PROVIDER_NAME": "fake",
            "RAG_BUSINESS_TIMEZONE": "UTC",
            "RAG_MAINTENANCE_KEY": "documents-worker-test-key",
        }
    )
    runtime = PlatformRuntime(
        settings,
        adapters={
            "database_engine": engine,
            "database_clock": type(
                "Clock",
                (),
                {"now_utc": lambda self, connection=None: datetime(2026, 1, 1, tzinfo=UTC)},
            )(),
            "documents_service": docs,
            "indexing_service": spy,
        },
    )
    now = lambda: datetime(2026, 1, 1, tzinfo=UTC)  # noqa: E731
    worker_runtime = WorkerRuntime(runtime, MemoryLeaseStore(now), now, owns_runtime=False)
    return engine, docs, spy, worker_runtime


@pytest.mark.parametrize("lifecycle_status", ["pending_delete", "deleted"])
def test_worker_terminates_personal_jobs_for_inactive_account_owner(lifecycle_status) -> None:
    engine, docs, spy, worker_runtime = _worker_runtime(
        _LifecycleIdentity({"u1": lifecycle_status})
    )
    owner = AuthPrincipal("u1", "s1", "alice", "user", None)
    personal = docs.create_initial_upload(
        principal=owner,
        space_id="personal:u1",
        files=[_upload(name="personal.txt", content=b"personal")],
        idempotency_key="upload-personal-terminate-1",
    )["items"][0]
    shared = docs.create_initial_upload(
        principal=owner,
        space_id="department:d1",
        files=[_upload(name="shared.txt", content=b"shared")],
        idempotency_key="upload-shared-terminate-1",
    )["items"][0]

    stats = IngestionWorker(worker_runtime).run_once(owner="ingestion:test:terminate", limit=2)

    assert stats.claimed == 2
    assert stats.failed == 1
    assert stats.succeeded == 1
    with engine.connect() as connection:
        rows = dict(
            connection.execute(
                select(
                    ingestion_jobs_table.c.document_id,
                    ingestion_jobs_table.c.state,
                )
            ).all()
        )
        reasons = dict(
            connection.execute(
                select(
                    ingestion_jobs_table.c.document_id,
                    ingestion_jobs_table.c.failure_reason,
                )
            ).all()
        )
    assert rows[personal["document_id"]] == "failed"
    assert reasons[personal["document_id"]] == f"account_{lifecycle_status}"
    # 共享库 job 不受个人账号状态影响，正常完成。
    assert rows[shared["document_id"]] == "succeeded"
    assert reasons[shared["document_id"]] is None
    # 个人库 job 未进入处理管道：只有共享库 attempt 产生处理副作用。
    assert len(spy.processed_attempts) == 1


def test_contribute_upload_response_includes_null_upload_batch_id(service, principal) -> None:
    class _ContributeIdentity:
        def authorize_space(self, *, principal, space_id: str, action: str, connection=None) -> str:
            del principal, space_id, action, connection
            return "contribute"

    service._identity_access = _ContributeIdentity()
    result = service.create_upload(
        principal=principal,
        space_id="space_1",
        files=[_upload(name="submission-batch-key.txt")],
        idempotency_key="upload-submission-batch-key-1",
    )
    assert result["upload_batch_id"] is None
    assert result["items"][0]["status"] == "pending"
    assert result["items"][0]["submission_id"]


def test_submission_only_upload_http_response_has_null_upload_batch_id() -> None:
    client, runtime, _store = _make_client()
    token, _space = _seed_user(runtime)
    response = client.post(
        "/v1/spaces/public/documents",
        files=[("files", ("shared-null-batch.txt", b"shared knowledge", "text/plain"))],
        headers={"Authorization": token, "Idempotency-Key": "submission-null-batch-1"},
    )
    assert response.status_code == 202
    body = response.json()
    assert body["upload_batch_id"] is None
    assert body["items"][0]["status"] == "pending"


def test_approval_list_rejects_unreachable_personal_target_kind() -> None:
    client, runtime, _store = _make_client()
    token, _space = _seed_user(runtime)
    rejected = client.get(
        "/v1/approvals/submissions",
        params={"target_kind": "personal"},
        headers={"Authorization": token},
    )
    assert rejected.status_code == 422
    # 支持的取值仍进入业务层（普通 user 无审核资格 → 403 approval_forbidden）。
    allowed_shape = client.get(
        "/v1/approvals/submissions",
        params={"target_kind": "department"},
        headers={"Authorization": token},
    )
    assert allowed_shape.status_code == 403
    assert allowed_shape.json()["error"]["code"] == "approval_forbidden"


def test_duplicate_review_and_idempotent_replay_create_no_second_grant_or_job(
    service, principal
) -> None:
    submission = service.create_submission(
        principal=principal,
        space_id="space_1",
        file=_upload(name="review-idempotent.txt"),
        idempotency_key="submission-review-idempotent-1",
    )
    reviewer = _principal("admin")
    approved = service.approve_submission(
        principal=reviewer,
        submission_id=submission["submission_id"],
        expected_version=1,
        idempotency_key="approve-idempotent-1",
    )
    assert approved["status"] == "approved"

    def counts() -> tuple[int, int]:
        with service._engine.connect() as connection:
            grants = int(
                connection.execute(
                    select(func.count()).select_from(submission_execution_grants_table)
                ).scalar_one()
            )
            jobs = int(
                connection.execute(
                    select(func.count()).select_from(ingestion_jobs_table)
                ).scalar_one()
            )
        return grants, jobs

    assert counts() == (1, 1)

    # 同 key 幂等重放：返回同一响应，不创建第二个 grant 或 job。
    replayed = service.approve_submission(
        principal=reviewer,
        submission_id=submission["submission_id"],
        expected_version=1,
        idempotency_key="approve-idempotent-1",
    )
    assert replayed == approved
    assert counts() == (1, 1)

    # 重复审核（不同 key）：submission_already_reviewed 直接断言，计数不变。
    with pytest.raises(PlatformError) as error:
        service.approve_submission(
            principal=reviewer,
            submission_id=submission["submission_id"],
            expected_version=2,
            idempotency_key="approve-idempotent-2",
        )
    assert error.value.code == "submission_already_reviewed"
    assert error.value.status_code == 409
    assert error.value.details == {"status": "approved"}
    assert counts() == (1, 1)
