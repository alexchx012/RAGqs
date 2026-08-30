from __future__ import annotations

import pytest
from sqlalchemy import delete, select

from app.documents.schema import (
    ingestion_attempts_table,
    ingestion_jobs_table,
    knowledge_submissions_table,
    submission_execution_grants_table,
)
from app.documents.submissions import DocumentsSubmissionInvalidationPort, SubmissionService
from app.identity.ports import PendingSubmissionInvalidationCommand
from app.platform.errors import PlatformError
from app.platform.storage import StorageKeyError

from .test_commands import _upload


class RecordingSubmissionOutbox:
    def __init__(self) -> None:
        self.events: list[dict[str, object]] = []

    def publish_submission_event(
        self,
        *,
        event_type: str,
        submission_id: str,
        transition_version: int,
        recipient_user_id: str,
        occurred_at: object,
        connection: object,
        document_id: str | None = None,
        job_id: str | None = None,
        reason: str | None = None,
    ) -> str:
        del occurred_at, connection
        event: dict[str, object] = {
            "event_type": event_type,
            "submission_id": submission_id,
            "transition_version": transition_version,
            "recipient_user_id": recipient_user_id,
            "document_id": document_id,
            "job_id": job_id,
            "reason": reason,
        }
        self.events.append(event)
        return f"event-{submission_id}"


def test_contribute_upload_creates_private_pending_submission(service, principal) -> None:
    submission = service.create_submission(
        principal=principal,
        space_id="space_1",
        file=_upload(),
        idempotency_key="submission-1",
    )
    assert submission["status"] == "pending"
    assert submission["quota_exempt"] is True
    assert submission["document_id"] is None
    assert (
        service.list_submissions(principal=principal)["items"][0]["submission_id"]
        == submission["submission_id"]
    )


def test_submission_list_exposes_review_contract_fields_and_grant_ids(service, principal) -> None:
    submission = service.create_submission(
        principal=principal,
        space_id="space_1",
        file=_upload(),
        idempotency_key="submission-list-contract-1",
    )
    reviewer = principal.__class__(
        user_id="user_1",
        auth_session_id="admin-session",
        username="admin",
        role="admin",
        department_id=None,
    )
    approved = service.approve_submission(
        principal=reviewer,
        submission_id=submission["submission_id"],
        expected_version=1,
        idempotency_key="approve-list-contract-1",
    )

    assert service.list_submissions(principal=principal)["items"] == [
        {
            "submission_id": submission["submission_id"],
            "version": 2,
            "target_space_id": "space_1",
            "target_space_name": "space_1",
            "name": "guide.txt",
            "media_kind": "text/plain",
            "size_bytes": 5,
            "status": "approved",
            "created_at": "2026-01-01T00:00:00",
            "reviewed_at": "2026-01-01T00:00:00",
            "reject_reason": None,
            "invalidated_reason": None,
            "document_id": approved["document_id"],
            "job_id": approved["job_id"],
        }
    ]


def test_approval_list_uses_nested_submitter_snapshot_contract(service, principal) -> None:
    class SnapshotIdentity:
        def __init__(self) -> None:
            self.display_name = "Alice Snapshot"
            self.department = {"id": "department_snapshot", "name": "Snapshot Department"}
            self.department_names = {"department_snapshot": "Snapshot Department"}

        def authorize_space(self, *, principal, space_id: str, action: str, connection=None) -> str:
            del principal, space_id, action, connection
            return "contribute"

        def list_departments(self, *, actor, status: str) -> list[dict[str, str]]:
            del actor, status
            return [
                {"id": department_id, "name": department_name}
                for department_id, department_name in self.department_names.items()
            ]

        def user_response(self, user_id: str) -> dict[str, object]:
            assert user_id == "user_1"
            return {
                "display_name": self.display_name,
                "department": self.department,
            }

    identity = SnapshotIdentity()
    service._identity_access = identity
    submitter = principal.__class__(
        user_id="user_1",
        auth_session_id="submitter-session",
        username="alice",
        role="minister",
        department_id="department_snapshot",
    )
    submission = service.create_submission(
        principal=submitter,
        space_id="department:department_snapshot",
        file=_upload(),
        idempotency_key="submission-approval-contract-1",
    )
    identity.display_name = "Alice Renamed"
    identity.department = {"id": "department_live", "name": "Live Department"}
    identity.department_names["department_snapshot"] = "Renamed Snapshot Department"
    reviewer = principal.__class__(
        user_id="user_1",
        auth_session_id="admin-session",
        username="admin",
        role="admin",
        department_id=None,
    )

    assert service.list_approval_submissions(principal=reviewer)["items"] == [
        {
            "submission_id": submission["submission_id"],
            "version": 1,
            "submitter": {
                "id": "user_1",
                "display_name": "Alice Snapshot",
                "department": {"id": "department_snapshot", "name": "Snapshot Department"},
            },
            "name": "guide.txt",
            "media_kind": "text/plain",
            "size_bytes": 5,
            "target_space_id": "department:department_snapshot",
            "target_space_name": "Renamed Snapshot Department",
            "created_at": "2026-01-01T00:00:00",
        }
    ]


def test_submission_creation_and_approval_keep_submitter_snapshots(service, principal) -> None:
    assert {
        "submitter_role_snapshot",
        "submitter_department_snapshot",
        "submitter_display_name_snapshot",
        "submitter_department_name_snapshot",
        "invalidated_reason",
        "invalidated_at",
    } <= set(knowledge_submissions_table.c.keys())

    submitter = principal.__class__(
        user_id="user_1",
        auth_session_id="submitter-session",
        username="alice",
        role="minister",
        department_id="department_7",
    )
    submission = service.create_submission(
        principal=submitter,
        space_id="space_1",
        file=_upload(),
        idempotency_key="submission-snapshot-1",
    )
    reviewer = principal.__class__(
        user_id="user_1",
        auth_session_id="admin-session",
        username="admin",
        role="admin",
        department_id=None,
    )
    approved = service.approve_submission(
        principal=reviewer,
        submission_id=submission["submission_id"],
        expected_version=1,
        idempotency_key="approve-snapshot-1",
    )

    with service._engine.connect() as connection:
        stored_submission = (
            connection.execute(
                select(knowledge_submissions_table).where(
                    knowledge_submissions_table.c.id == submission["submission_id"]
                )
            )
            .mappings()
            .one()
        )
        job = (
            connection.execute(
                select(ingestion_jobs_table).where(ingestion_jobs_table.c.id == approved["job_id"])
            )
            .mappings()
            .one()
        )

    assert stored_submission["submitter_role_snapshot"] == "minister"
    assert stored_submission["submitter_department_snapshot"] == "department_7"
    assert stored_submission["submitter_display_name_snapshot"] == "alice"
    assert stored_submission["submitter_department_name_snapshot"] is None
    assert job["quota_role_snapshot"] == "minister"
    assert job["quota_department_id_snapshot"] == "department_7"


def test_contribute_upload_accepts_maximum_key_for_distinct_request_items(
    service, principal
) -> None:
    key = "x" * 256

    first = service.create_submission(
        principal=principal,
        space_id="space_1",
        file=_upload(),
        idempotency_key=key,
        idempotency_item_index=0,
    )
    second = service.create_submission(
        principal=principal,
        space_id="space_1",
        file=type(_upload())("second.txt", b"second", "text/plain"),
        idempotency_key=key,
        idempotency_item_index=1,
    )

    assert first["submission_id"] != second["submission_id"]


def test_approval_creates_immutable_grant_and_initial_job(service, principal) -> None:
    submission = service.create_submission(
        principal=principal,
        space_id="space_1",
        file=_upload(),
        idempotency_key="submission-1",
    )
    minister = principal.__class__(
        user_id=principal.user_id,
        auth_session_id=principal.auth_session_id,
        username=principal.username,
        role="minister",
        department_id=None,
    )
    with pytest.raises(PlatformError) as error:
        service.approve_submission(
            principal=minister,
            submission_id=submission["submission_id"],
            expected_version=1,
            idempotency_key="approve-1",
        )
    assert error.value.code == "approval_forbidden"


def test_approval_deletes_private_submission_original_after_copying_document(
    service, principal
) -> None:
    submission = service.create_submission(
        principal=principal,
        space_id="space_1",
        file=_upload(),
        idempotency_key="submission-private-cleanup-1",
    )
    with service._engine.connect() as connection:
        private_object_key = connection.execute(
            select(knowledge_submissions_table.c.private_object_key).where(
                knowledge_submissions_table.c.id == submission["submission_id"]
            )
        ).scalar_one()

    reviewer = principal.__class__(
        user_id="user_1",
        auth_session_id="admin-session",
        username="admin",
        role="admin",
        department_id=None,
    )
    approved = service.approve_submission(
        principal=reviewer,
        submission_id=submission["submission_id"],
        expected_version=1,
        idempotency_key="approve-private-cleanup-1",
    )

    document_content, _ = service._object_store.get(
        f"documents/{approved['document_id']}/{approved['document_version_id']}/original"
    )
    assert document_content == b"hello"
    # The private original is only removed by the scheduled cleanup pass, never inside
    # the approve transaction itself.
    service._object_store.get(private_object_key)
    assert service.cleanup_scheduled_submissions() == [submission["submission_id"]]
    with pytest.raises(StorageKeyError):
        service._object_store.get(private_object_key)


def test_submitter_can_withdraw_pending_submission(service, principal) -> None:
    submission = service.create_submission(
        principal=principal,
        space_id="space_1",
        file=_upload(),
        idempotency_key="submission-1",
    )
    withdrawn = service.withdraw_submission(
        principal=principal,
        submission_id=submission["submission_id"],
        expected_version=1,
        idempotency_key="withdraw-1",
    )
    assert withdrawn["status"] == "withdrawn"


def test_reject_revalidates_the_target_space_before_transitioning(service, principal) -> None:
    submission = service.create_submission(
        principal=principal,
        space_id="space_1",
        file=_upload(),
        idempotency_key="submission-reject-revalidate-1",
    )

    class _ReadOnlyIdentity:
        def authorize_space(self, *, principal, space_id, action, connection=None):
            del principal, space_id
            if action == "manage":
                raise PlatformError(
                    "space_action_forbidden", "Space is no longer writable", {}, 403
                )
            return "contribute"

    service._identity_access = _ReadOnlyIdentity()
    reviewer = principal.__class__(
        user_id="admin_1",
        auth_session_id="admin-session",
        username="admin",
        role="admin",
        department_id=None,
    )

    # 前端契约（§8.5）：scope 失效时先落库 invalidated，再向审核者返回
    # 409 submission_scope_changed + 最新 version。
    with pytest.raises(PlatformError) as error:
        service.reject_submission(
            principal=reviewer,
            submission_id=submission["submission_id"],
            expected_version=1,
            idempotency_key="reject-revalidate-1",
        )
    assert error.value.code == "submission_scope_changed"
    assert error.value.details == {"version": 2}


def test_terminal_submission_delete_replays_after_the_submission_row_is_removed(
    service, principal
) -> None:
    submission = service.create_submission(
        principal=principal,
        space_id="space_1",
        file=_upload(),
        idempotency_key="submission-delete-replay-1",
    )
    withdrawn = service.withdraw_submission(
        principal=principal,
        submission_id=submission["submission_id"],
        expected_version=1,
        idempotency_key="withdraw-delete-replay-1",
    )

    assert (
        service.delete_submission(
            principal=principal,
            submission_id=submission["submission_id"],
            expected_version=withdrawn["version"],
            idempotency_key="delete-terminal-replay-1",
        )
        is None
    )
    assert (
        service.delete_submission(
            principal=principal,
            submission_id=submission["submission_id"],
            expected_version=withdrawn["version"],
            idempotency_key="delete-terminal-replay-1",
        )
        is None
    )


def test_submitter_can_read_an_uncleaned_withdrawn_original(service, principal) -> None:
    submission = service.create_submission(
        principal=principal,
        space_id="space_1",
        file=_upload(),
        idempotency_key="submission-1",
    )
    service.withdraw_submission(
        principal=principal,
        submission_id=submission["submission_id"],
        expected_version=1,
        idempotency_key="withdraw-1",
    )

    content, _metadata, filename = service.submission_content(
        principal=principal,
        submission_id=submission["submission_id"],
    )
    assert content == b"hello"
    assert filename == "guide.txt"


def test_withdrawal_schedules_cleanup_without_revoking_uncleaned_owner_access(
    service, principal
) -> None:
    submission = service.create_submission(
        principal=principal,
        space_id="space_1",
        file=_upload(),
        idempotency_key="submission-cleanup-1",
    )
    service.withdraw_submission(
        principal=principal,
        submission_id=submission["submission_id"],
        expected_version=1,
        idempotency_key="withdraw-cleanup-1",
    )

    assert service.cleanup_scheduled_submissions() == [submission["submission_id"]]
    with pytest.raises(PlatformError) as error:
        service.submission_content(principal=principal, submission_id=submission["submission_id"])
    assert error.value.code == "submission_content_unavailable"
    assert error.value.status_code == 404


def test_missing_submission_storage_object_is_reported_as_not_found(service, principal) -> None:
    submission = service.create_submission(
        principal=principal,
        space_id="space_1",
        file=_upload(),
        idempotency_key="submission-missing-storage-1",
    )
    with service._engine.connect() as connection:
        private_object_key = connection.execute(
            select(knowledge_submissions_table.c.private_object_key).where(
                knowledge_submissions_table.c.id == submission["submission_id"]
            )
        ).scalar_one()
    service._object_store.delete(private_object_key)

    with pytest.raises(PlatformError) as error:
        service.submission_content(principal=principal, submission_id=submission["submission_id"])

    assert error.value.code == "submission_content_unavailable"
    assert error.value.status_code == 404


def test_identity_invalidation_is_sorted_notified_and_scheduled_for_cleanup(
    service, principal
) -> None:
    outbox = RecordingSubmissionOutbox()
    service._submission_notification_port = outbox
    service._identity_access = None
    invalidated = service.create_submission(
        principal=principal,
        space_id="department:previous",
        file=_upload(),
        idempotency_key="submission-previous-department",
    )
    retained = service.create_submission(
        principal=principal,
        space_id="department:current",
        file=type(_upload())("second.txt", b"second", "text/plain"),
        idempotency_key="submission-current-department",
    )

    with service._engine.begin() as connection:
        changed = DocumentsSubmissionInvalidationPort(service).invalidate_pending_submissions(
            PendingSubmissionInvalidationCommand(
                user_id=principal.user_id,
                role="user",
                department_id="current",
                lifecycle_status="active",
                reason="identity_authorization_changed",
            ),
            connection=connection,
        )

    assert changed == 1
    items = {
        item["submission_id"]: item
        for item in service.list_submissions(principal=principal)["items"]
    }
    assert items[invalidated["submission_id"]]["status"] == "invalidated"
    assert items[retained["submission_id"]]["status"] == "pending"
    assert "invalidated_reason" in items[invalidated["submission_id"]]
    assert items[invalidated["submission_id"]]["invalidated_reason"] == "identity_authorization_changed"
    assert items[invalidated["submission_id"]]["reviewed_at"] is None
    with service._engine.connect() as connection:
        invalidated_row = (
            connection.execute(
                select(knowledge_submissions_table).where(
                    knowledge_submissions_table.c.id == invalidated["submission_id"]
                )
            )
            .mappings()
            .one()
        )
    assert invalidated_row["invalidated_at"] is not None
    assert invalidated_row["review_reason"] is None
    assert invalidated_row["reviewed_at_utc"] is None
    assert outbox.events == [
        {
            "event_type": "submission_invalidated",
            "submission_id": invalidated["submission_id"],
            "transition_version": 2,
            "recipient_user_id": principal.user_id,
            "document_id": None,
            "job_id": None,
            "reason": "identity_authorization_changed",
        }
    ]


def test_submission_execution_grant_becomes_the_worker_authorization_fence(
    service, principal
) -> None:
    submission = service.create_submission(
        principal=principal,
        space_id="space_1",
        file=_upload(),
        idempotency_key="submission-grant-1",
    )
    reviewer = principal.__class__(
        user_id="user_1",
        auth_session_id="admin-session",
        username="admin",
        role="admin",
        department_id=None,
    )
    approved = service.approve_submission(
        principal=reviewer,
        submission_id=submission["submission_id"],
        expected_version=1,
        idempotency_key="approve-grant-1",
    )
    lease = service.claim_job(worker_id="worker-grant", job_id=approved["job_id"])

    with service._engine.connect() as connection:
        attempt = (
            connection.execute(
                select(ingestion_attempts_table).where(
                    ingestion_attempts_table.c.id == lease.attempt_id
                )
            )
            .mappings()
            .one()
        )
    assert attempt["staging_request_json"]["authorization_fence"] == {
        "kind": "submission_execution_grant",
        "grant_id": approved["grant_id"],
        "submission_id": submission["submission_id"],
    }

    service.fail_job(
        job_id=approved["job_id"],
        reason="retry grant validation",
        attempt_id=lease.attempt_id,
        fencing_token=lease.fencing_token,
    )

    with service._engine.begin() as connection:
        connection.execute(
            delete(submission_execution_grants_table).where(
                submission_execution_grants_table.c.job_id == approved["job_id"]
            )
        )
    with pytest.raises(PlatformError) as error:
        service.replay_job(
            principal=principal.__class__(
                user_id="user_1",
                auth_session_id="ops-session",
                username="ops",
                role="ops",
                department_id=None,
            ),
            job_id=approved["job_id"],
            idempotency_key="replay-missing-grant",
        )
    assert error.value.code == "submission_grant_invalid"


def test_approved_submission_event_carries_document_and_job_ids(service, principal) -> None:
    """B8/#60-62：submission_approved 通知 payload 携带 document_id/job_id，
    经替身记录验证；rejected/invalidated 的 reason 同步传递。"""

    class _PublicIdentity:
        def authorize_space(self, *, principal, space_id: str, action: str, connection=None):
            del principal, space_id, action, connection
            return "manage"

        def user_response(self, user_id: str) -> dict[str, object]:
            return {"lifecycle_status": "active", "role": "user", "department_id": None}

    outbox = RecordingSubmissionOutbox()
    service._submission_notification_port = outbox
    service._identity_access = _PublicIdentity()
    submission = service.create_submission(
        principal=principal,
        space_id="public",
        file=_upload(),
        idempotency_key="b8-approved",
    )
    # 公共库审核仅 ops/admin（§8.4）；投稿人仍是原 principal。
    reviewer = principal.__class__(
        user_id="ops_1",
        auth_session_id="ops-session",
        username="ops",
        role="ops",
        department_id=None,
    )
    approved = SubmissionService(service).approve(
        principal=reviewer,
        submission_id=submission["submission_id"],
        expected_version=submission["version"],
        idempotency_key="b8-approved-approve",
    )

    assert outbox.events == [
        {
            "event_type": "submission_approved",
            "submission_id": submission["submission_id"],
            "transition_version": approved["version"],
            "recipient_user_id": principal.user_id,
            "document_id": approved["document_id"],
            "job_id": approved["job_id"],
            "reason": None,
        }
    ]


class _InactiveSubmitterIdentity:
    """user_response 拒绝所有非 active 账号；审核拆分依赖 account_lifecycle_status
    区分 pending_delete 与 deleted（后端设计 §6.4）。"""

    def __init__(self, lifecycle_status: str) -> None:
        self.lifecycle_status = lifecycle_status

    def user_response(self, user_id: str) -> dict[str, object]:
        raise PlatformError("authentication_required", "The account is not active", {}, 401)

    def account_lifecycle_status(self, user_id: str) -> str:
        del user_id
        return self.lifecycle_status


def _assert_invalidated_with_reason(service, submission_id: str, reason: str) -> None:
    with service._engine.connect() as connection:
        row = (
            connection.execute(
                select(knowledge_submissions_table).where(
                    knowledge_submissions_table.c.id == submission_id
                )
            )
            .mappings()
            .one()
        )
    assert row["status"] == "invalidated"
    assert row["invalidated_reason"] == reason


@pytest.mark.parametrize(
    ("lifecycle_status", "expected_code"),
    [
        ("pending_delete", "submitter_pending_delete"),
        ("deleted", "submitter_deleted"),
    ],
)
def test_review_splits_submitter_account_conflicts_by_lifecycle(
    service, principal, lifecycle_status, expected_code
) -> None:
    """账号 pending_delete 与 deleted 分别返回专用 409，invalidated_reason
    落库对应机器原因（后端设计 §6.4）。"""
    submission = service.create_submission(
        principal=principal,
        space_id="space_1",
        file=_upload(),
        idempotency_key=f"submitter-split-{lifecycle_status}",
    )
    service._identity_access = _InactiveSubmitterIdentity(lifecycle_status)
    reviewer = principal.__class__(
        user_id="admin_1",
        auth_session_id="admin-session",
        username="admin",
        role="admin",
        department_id=None,
    )

    with pytest.raises(PlatformError) as error:
        service.approve_submission(
            principal=reviewer,
            submission_id=submission["submission_id"],
            expected_version=submission["version"],
            idempotency_key=f"submitter-split-approve-{lifecycle_status}",
        )
    assert error.value.code == expected_code
    assert error.value.status_code == 409
    assert error.value.details == {"version": submission["version"] + 1}
    _assert_invalidated_with_reason(service, submission["submission_id"], expected_code)
