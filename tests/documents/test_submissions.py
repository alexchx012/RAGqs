from __future__ import annotations

import pytest
from sqlalchemy import delete, select

from app.documents.schema import ingestion_attempts_table, submission_execution_grants_table
from app.documents.submissions import DocumentsSubmissionInvalidationPort
from app.identity.ports import PendingSubmissionInvalidationCommand
from app.platform.errors import PlatformError

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
    ) -> str:
        del occurred_at, connection
        self.events.append(
            {
                "event_type": event_type,
                "submission_id": submission_id,
                "transition_version": transition_version,
                "recipient_user_id": recipient_user_id,
            }
        )
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
    assert error.value.code == "submission_review_forbidden"


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
        def authorize_space(self, *, principal, space_id, action):
            del principal, space_id
            if action == "manage":
                raise PlatformError("space_action_forbidden", "Space is no longer writable", {}, 403)
            return "contribute"

    service._identity_access = _ReadOnlyIdentity()
    reviewer = principal.__class__(
        user_id="admin_1",
        auth_session_id="admin-session",
        username="admin",
        role="admin",
        department_id=None,
    )

    result = service.reject_submission(
        principal=reviewer,
        submission_id=submission["submission_id"],
        expected_version=1,
        idempotency_key="reject-revalidate-1",
    )
    assert result["status"] == "invalidated"
    assert result["reason"] == "space_not_writable"


def test_terminal_submission_delete_replays_after_the_submission_row_is_removed(service, principal) -> None:
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

    assert service.delete_submission(
        principal=principal,
        submission_id=submission["submission_id"],
        expected_version=withdrawn["version"],
        idempotency_key="delete-terminal-replay-1",
    ) is None
    assert service.delete_submission(
        principal=principal,
        submission_id=submission["submission_id"],
        expected_version=withdrawn["version"],
        idempotency_key="delete-terminal-replay-1",
    ) is None


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
    assert outbox.events == [
        {
            "event_type": "submission_invalidated",
            "submission_id": invalidated["submission_id"],
            "transition_version": 2,
            "recipient_user_id": principal.user_id,
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
