from __future__ import annotations

import secrets
from typing import Any

from sqlalchemy import and_, delete, select, update

from app.identity.ports import PendingSubmissionInvalidationCommand
from app.platform.database import _insert_do_nothing
from app.platform.errors import PlatformError
from app.platform.storage import ObjectMetadata, StorageKeyError

from .domain import DocumentLifecycle, DocumentVersionState, IngestionJobState, PublicationState
from .schema import (
    document_versions_table,
    documents_table,
    ingestion_jobs_table,
    knowledge_submissions_table,
    publications_table,
    submission_execution_grants_table,
    upload_dedup_claims_table,
)
from .service import DocumentsService, DocumentUpload


def _new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_urlsafe(18)}"


class DocumentsSubmissionInvalidationPort:
    """Identity-facing command adapter over pending submissions."""

    def __init__(self, service: DocumentsService) -> None:
        self._service = service

    def invalidate_pending_submissions(
        self,
        command: PendingSubmissionInvalidationCommand,
        *,
        connection,
    ) -> int:
        return SubmissionService(self._service).invalidate_pending_for_identity(
            command,
            connection=connection,
        )


class SubmissionService:
    def __init__(self, service: DocumentsService) -> None:
        self._service = service

    def _publish_submission_event(
        self,
        connection,
        *,
        event_type: str,
        submission_id: str,
        transition_version: int,
        recipient_user_id: str,
        occurred_at,
    ) -> None:
        port = self._service._submission_notification_port
        if port is None:
            return
        port.publish_submission_event(
            event_type=event_type,
            submission_id=str(submission_id),
            transition_version=transition_version,
            recipient_user_id=str(recipient_user_id),
            occurred_at=occurred_at,
            connection=connection,
        )

    @staticmethod
    def _can_contribute(
        *,
        user_id: str,
        role: str,
        department_id: str | None,
        lifecycle_status: str,
        space_id: str,
    ) -> bool:
        if lifecycle_status != "active":
            return False
        if space_id == "public":
            return role in {"user", "minister", "ops", "admin"}
        if space_id == f"personal:{user_id}":
            return True
        if not space_id.startswith("department:"):
            return False
        if role in {"ops", "admin"}:
            return True
        return role in {"user", "minister"} and space_id == f"department:{department_id}"

    def _review_preconditions(
        self,
        connection,
        *,
        submission: Any,
        principal: Any,
    ) -> tuple[Any | None, str | None]:
        """Lock review facts and return either a duplicate claim or an invalidation reason.

        Content integrity is trusted from the stored hash/manifest state written at
        upload time; the object body is only touched when approve copies it.
        """
        normalized_name = " ".join(str(submission["file_name"]).strip().split()).casefold()
        existing_claim = (
            connection.execute(
                select(upload_dedup_claims_table)
                .where(
                    (upload_dedup_claims_table.c.space_id == submission["space_id"])
                    & (upload_dedup_claims_table.c.normalized_filename == normalized_name)
                    & (
                        upload_dedup_claims_table.c.content_hash_sha256
                        == submission["content_hash_sha256"]
                    )
                )
                .with_for_update()
            )
            .mappings()
            .one_or_none()
        )
        identity_access = self._service._identity_access
        if identity_access is not None and hasattr(identity_access, "user_response"):
            try:
                submitter = identity_access.user_response(str(submission["submitter_user_id"]))
            except PlatformError:
                submitter = {"lifecycle_status": "deleted"}
            if submitter.get("lifecycle_status") != "active":
                return existing_claim, "submitter_not_active"
            if not self._can_contribute(
                user_id=str(submission["submitter_user_id"]),
                role=str(submitter.get("role", "")),
                department_id=submitter.get("department_id"),
                lifecycle_status=str(submitter.get("lifecycle_status", "deleted")),
                space_id=str(submission["space_id"]),
            ):
                return existing_claim, "submitter_contribution_revoked"
        try:
            self._service._authorize(principal, str(submission["space_id"]), "manage")
        except PlatformError:
            return existing_claim, "space_not_writable"
        return existing_claim, None

    def invalidate_pending_for_identity(
        self,
        command: PendingSubmissionInvalidationCommand,
        *,
        connection,
    ) -> int:
        rows = (
            connection.execute(
                select(knowledge_submissions_table)
                .where(
                    and_(
                        knowledge_submissions_table.c.submitter_user_id == command.user_id,
                        knowledge_submissions_table.c.status == "pending",
                    )
                )
                .order_by(knowledge_submissions_table.c.id)
                .with_for_update()
            )
            .mappings()
            .all()
        )
        now = self._service._current_time()
        invalidated = 0
        for submission in rows:
            if self._can_contribute(
                user_id=command.user_id,
                role=command.role,
                department_id=command.department_id,
                lifecycle_status=command.lifecycle_status,
                space_id=str(submission["space_id"]),
            ):
                continue
            next_version = int(submission["version"]) + 1
            updated = connection.execute(
                update(knowledge_submissions_table)
                .where(
                    and_(
                        knowledge_submissions_table.c.id == submission["id"],
                        knowledge_submissions_table.c.status == "pending",
                        knowledge_submissions_table.c.version == submission["version"],
                    )
                )
                .values(
                    status="invalidated",
                    version=next_version,
                    reviewer_user_id=None,
                    reviewer_role_snapshot=None,
                    review_reason=command.reason,
                    private_object_cleanup_requested_at_utc=now,
                    reviewed_at_utc=now,
                    updated_at_utc=now,
                )
            ).rowcount
            if updated != 1:
                continue
            self._publish_submission_event(
                connection,
                event_type="submission_invalidated",
                submission_id=str(submission["id"]),
                transition_version=next_version,
                recipient_user_id=command.user_id,
                occurred_at=now,
            )
            invalidated += 1
        return invalidated

    def _invalidate(
        self,
        connection,
        *,
        submission: Any,
        reviewer: Any,
        reason: str,
        now,
        actor_id: str,
        endpoint: str,
        key: str,
        fingerprint: str,
    ) -> dict[str, Any]:
        next_version = int(submission["version"]) + 1
        connection.execute(
            update(knowledge_submissions_table)
            .where(knowledge_submissions_table.c.id == submission["id"])
            .values(
                status="invalidated",
                version=next_version,
                reviewer_user_id=actor_id,
                reviewer_role_snapshot=str(reviewer.role),
                review_reason=reason,
                private_object_cleanup_requested_at_utc=now,
                reviewed_at_utc=now,
                updated_at_utc=now,
            )
        )
        response = {
            "submission_id": submission["id"],
            "version": next_version,
            "status": "invalidated",
            "reason": reason,
        }
        self._service._complete_idempotency(
            connection,
            actor_id=actor_id,
            endpoint=endpoint,
            target_id=submission["id"],
            key=key,
            fingerprint=fingerprint,
            response=response,
        )
        recipient_user_id = str(submission["submitter_user_id"])
        identity_access = self._service._identity_access
        if identity_access is not None and hasattr(identity_access, "user_response"):
            try:
                recipient = identity_access.user_response(recipient_user_id)
            except PlatformError:
                recipient = {"lifecycle_status": "deleted"}
            if recipient.get("lifecycle_status") != "active":
                recipient_user_id = actor_id
        self._publish_submission_event(
            connection,
            event_type="submission_invalidated",
            submission_id=str(submission["id"]),
            transition_version=next_version,
            recipient_user_id=recipient_user_id,
            occurred_at=now,
        )
        return response

    def create(
        self,
        *,
        principal: Any,
        space_id: str,
        file: DocumentUpload,
        idempotency_key: str | None,
        idempotency_item_index: int | None = None,
    ) -> dict[str, Any]:
        key = self._service._required_key(idempotency_key)
        self._service._authorize(principal, space_id, "contribute")
        info = self._service._file_fingerprint(file)
        actor_id = str(principal.user_id)
        endpoint = "documents.submission_create"
        if idempotency_item_index is not None:
            endpoint = f"{endpoint}:{idempotency_item_index}"
        fingerprint = self._service._idempotency_fingerprint({"space_id": space_id, "file": info})
        with self._service._engine.begin() as connection:
            replay = self._service._idempotency_replay(
                connection,
                actor_id=actor_id,
                endpoint=endpoint,
                target_id=space_id,
                key=key,
                fingerprint=fingerprint,
            )
            if replay is not None:
                return replay
            now = self._service._current_time()
            submission_id = _new_id("submission")
            object_key = f"submissions/{submission_id}/original"
            self._service._object_store.put(
                object_key,
                file.content,
                ObjectMetadata(
                    content_type=file.media_kind,
                    size_bytes=len(file.content),
                    checksum_sha256=info["content_hash_sha256"],
                ),
            )
            connection.execute(
                knowledge_submissions_table.insert().values(
                    id=submission_id,
                    space_id=space_id,
                    submitter_user_id=actor_id,
                    version=1,
                    status="pending",
                    file_name=info["filename"],
                    media_kind=file.media_kind,
                    content_hash_sha256=info["content_hash_sha256"],
                    private_object_key=object_key,
                    object_manifest_json={
                        "object_key": object_key,
                        "size_bytes": len(file.content),
                    },
                    private_object_cleanup_requested_at_utc=None,
                    private_object_cleaned_at_utc=None,
                    reviewer_user_id=None,
                    reviewer_role_snapshot=None,
                    review_reason=None,
                    created_at_utc=now,
                    reviewed_at_utc=None,
                    updated_at_utc=now,
                )
            )
            response = {
                "submission_id": submission_id,
                "version": 1,
                "status": "pending",
                "space_id": space_id,
                "quota_exempt": True,
                "document_id": None,
                "document_version_id": None,
                "job_id": None,
            }
            self._service._complete_idempotency(
                connection,
                actor_id=actor_id,
                endpoint=endpoint,
                target_id=space_id,
                key=key,
                fingerprint=fingerprint,
                response=response,
            )
            return response

    def list(self, *, principal: Any, status: str | None = None) -> dict[str, Any]:
        with self._service._engine.connect() as connection:
            query = select(knowledge_submissions_table).where(
                knowledge_submissions_table.c.submitter_user_id == str(principal.user_id)
            )
            if status is not None:
                if status not in {"pending", "approved", "rejected", "withdrawn", "invalidated"}:
                    raise PlatformError("validation_error", "status is invalid", {}, 422)
                query = query.where(knowledge_submissions_table.c.status == status)
            rows = (
                connection.execute(
                    query.order_by(knowledge_submissions_table.c.created_at_utc.desc())
                )
                .mappings()
                .all()
            )
        return {"items": [self._public_row(row) for row in rows]}

    def list_approvals(self, *, principal: Any) -> dict[str, Any]:
        role = str(getattr(principal, "role", ""))
        with self._service._engine.connect() as connection:
            query = select(knowledge_submissions_table).where(
                knowledge_submissions_table.c.status == "pending"
            )
            if role == "admin":
                pass
            elif role == "ops":
                query = query.where(knowledge_submissions_table.c.space_id == "public")
            elif role == "minister":
                query = query.where(
                    knowledge_submissions_table.c.space_id
                    == f"department:{getattr(principal, 'department_id', None)}"
                )
            else:
                return {"items": []}
            rows = connection.execute(query).mappings().all()
        return {"items": [self._public_row(row) for row in rows]}

    def content(self, *, principal: Any, submission_id: str) -> tuple[bytes, ObjectMetadata, str]:
        with self._service._engine.connect() as connection:
            row = (
                connection.execute(
                    select(knowledge_submissions_table).where(
                        knowledge_submissions_table.c.id == submission_id
                    )
                )
                .mappings()
                .one_or_none()
            )
        if row is None:
            raise PlatformError("submission_not_found", "Submission was not found", {}, 404)
        is_submitter = str(row["submitter_user_id"]) == str(principal.user_id)
        owner_can_read = is_submitter and row["status"] in {
            "pending",
            "rejected",
            "withdrawn",
            "invalidated",
        }
        reviewer_can_read = row["status"] == "pending" and self._review_allowed(
            principal, str(row["space_id"])
        )
        if not owner_can_read and not reviewer_can_read:
            raise PlatformError(
                "submission_content_forbidden", "Submission content is not available", {}, 403
            )
        if row["private_object_cleaned_at_utc"] is not None:
            raise PlatformError(
                "submission_content_unavailable", "Submission content is unavailable", {}, 410
            )
        try:
            content, metadata = self._service._object_store.get(str(row["private_object_key"]))
        except (StorageKeyError, KeyError) as exc:
            raise PlatformError(
                "submission_content_unavailable", "Submission content is unavailable", {}, 410
            ) from exc
        return content, metadata, str(row["file_name"])

    def approve(
        self,
        *,
        principal: Any,
        submission_id: str,
        expected_version: int,
        idempotency_key: str | None,
    ) -> dict[str, Any]:
        return self._review(
            principal=principal,
            submission_id=submission_id,
            expected_version=expected_version,
            idempotency_key=idempotency_key,
            approve=True,
        )

    def reject(
        self,
        *,
        principal: Any,
        submission_id: str,
        expected_version: int,
        idempotency_key: str | None,
        reason: str | None = None,
    ) -> dict[str, Any]:
        if reason is not None and ("\n" in reason or "\r" in reason or len(reason) > 256):
            raise PlatformError("validation_error", "reason is invalid", {}, 422)
        return self._review(
            principal=principal,
            submission_id=submission_id,
            expected_version=expected_version,
            idempotency_key=idempotency_key,
            approve=False,
            reason=reason,
        )

    def _review(
        self,
        *,
        principal: Any,
        submission_id: str,
        expected_version: int,
        idempotency_key: str | None,
        approve: bool,
        reason: str | None = None,
    ) -> dict[str, Any]:
        key = self._service._required_key(idempotency_key)
        actor_id = str(principal.user_id)
        endpoint = "documents.submission_approve" if approve else "documents.submission_reject"
        fingerprint = self._service._idempotency_fingerprint(
            {"submission_id": submission_id, "expected_version": expected_version, "reason": reason}
        )
        with self._service._engine.begin() as connection:
            submission = (
                connection.execute(
                    select(knowledge_submissions_table)
                    .where(knowledge_submissions_table.c.id == submission_id)
                    .with_for_update()
                )
                .mappings()
                .one_or_none()
            )
            if submission is None:
                raise PlatformError("submission_not_found", "Submission was not found", {}, 404)
            if not self._review_allowed(principal, str(submission["space_id"])):
                raise PlatformError(
                    "submission_review_forbidden", "Submission review is not allowed", {}, 403
                )
            replay = self._service._idempotency_replay(
                connection,
                actor_id=actor_id,
                endpoint=endpoint,
                target_id=submission_id,
                key=key,
                fingerprint=fingerprint,
            )
            if replay is not None:
                return replay
            if int(submission["version"]) != expected_version:
                raise PlatformError(
                    "submission_version_conflict", "Submission version does not match", {}, 409
                )
            if submission["status"] != "pending":
                raise PlatformError("submission_not_pending", "Submission is not pending", {}, 409)
            now = self._service._current_time()
            existing_claim, invalid_reason = self._review_preconditions(
                connection,
                submission=submission,
                principal=principal,
            )
            if invalid_reason is not None:
                return self._invalidate(
                    connection,
                    submission=submission,
                    reviewer=principal,
                    reason=invalid_reason,
                    now=now,
                    actor_id=actor_id,
                    endpoint=endpoint,
                    key=key,
                    fingerprint=fingerprint,
                )
            if not approve:
                connection.execute(
                    update(knowledge_submissions_table)
                    .where(knowledge_submissions_table.c.id == submission_id)
                    .values(
                        status="rejected",
                        version=expected_version + 1,
                        reviewer_user_id=actor_id,
                        reviewer_role_snapshot=str(principal.role),
                        review_reason=reason,
                        reviewed_at_utc=now,
                        updated_at_utc=now,
                    )
                )
                response = {
                    "submission_id": submission_id,
                    "version": expected_version + 1,
                    "status": "rejected",
                }
                self._publish_submission_event(
                    connection,
                    event_type="submission_rejected",
                    submission_id=submission_id,
                    transition_version=expected_version + 1,
                    recipient_user_id=str(submission["submitter_user_id"]),
                    occurred_at=now,
                )
            else:
                if existing_claim is not None:
                    raise PlatformError(
                        "duplicate_document",
                        "A document with the same name and content already exists",
                        {"document_id": existing_claim["document_id"]},
                        409,
                    )
                normalized_name = " ".join(str(submission["file_name"]).strip().split()).casefold()
                document_id = _new_id("doc")
                version_id = _new_id("version")
                job_id = _new_id("job")
                publication_id = _new_id("publication")
                object_key = f"documents/{document_id}/{version_id}/original"
                manifest_size = int((submission["object_manifest_json"] or {}).get("size_bytes", 0))
                try:
                    self._service._object_store.copy(
                        str(submission["private_object_key"]), object_key
                    )
                except (StorageKeyError, KeyError):
                    return self._invalidate(
                        connection,
                        submission=submission,
                        reviewer=principal,
                        reason="private_object_unavailable",
                        now=now,
                        actor_id=actor_id,
                        endpoint=endpoint,
                        key=key,
                        fingerprint=fingerprint,
                    )
                connection.execute(
                    documents_table.insert().values(
                        id=document_id,
                        space_id=submission["space_id"],
                        lifecycle_status=DocumentLifecycle.ACTIVE.value,
                        active_version_id=None,
                        pending_version_id=version_id,
                        active_operation_job_id=job_id,
                        deletion_id=None,
                        version=1,
                        name=submission["file_name"],
                        normalized_name=str(submission["file_name"]).casefold(),
                        media_kind=submission["media_kind"],
                        uploaded_at_utc=now,
                        created_by_user_id=submission["submitter_user_id"],
                        created_at_utc=now,
                        updated_at_utc=now,
                    )
                )
                connection.execute(
                    document_versions_table.insert().values(
                        id=version_id,
                        document_id=document_id,
                        version_number=1,
                        status=DocumentVersionState.PENDING.value,
                        content_hash_sha256=submission["content_hash_sha256"],
                        object_manifest_json={
                            "object_key": object_key,
                            "size_bytes": manifest_size,
                        },
                        original_object_key=object_key,
                        file_name=submission["file_name"],
                        media_kind=submission["media_kind"],
                        size_bytes=manifest_size,
                        created_by_user_id=submission["submitter_user_id"],
                        activated_at_utc=None,
                        terminal_at_utc=None,
                        superseded_at_utc=None,
                        purge_after_at_utc=None,
                        purged_at_utc=None,
                        restored_from_version_id=None,
                        created_at_utc=now,
                        updated_at_utc=now,
                    )
                )
                connection.execute(
                    ingestion_jobs_table.insert().values(
                        id=job_id,
                        document_id=document_id,
                        document_version_id=version_id,
                        operation="initial",
                        state=IngestionJobState.PENDING.value,
                        stage="queued",
                        base_active_version_id=None,
                        upload_batch_id=None,
                        active_attempt_id=None,
                        active_publication_id=publication_id,
                        version=1,
                        replay_generation=0,
                        next_attempt_at_utc=None,
                        failure_reason=None,
                        degradations_json=[],
                        processing_summary_json={},
                        usage_json=None,
                        ocr_low_confidence=False,
                        notification_event_ids_json=[],
                        created_by_user_id=submission["submitter_user_id"],
                        quota_role_snapshot="user",
                        quota_department_id_snapshot=None,
                        quota_exempt_reason="shared_library_submission",
                        created_at_utc=now,
                        updated_at_utc=now,
                    )
                )
                connection.execute(
                    publications_table.insert().values(
                        id=publication_id,
                        document_id=document_id,
                        document_version_id=version_id,
                        job_id=job_id,
                        attempt_id="pending",
                        generation_id=self._service._current_index_generation(connection),
                        status=PublicationState.STAGED.value,
                        resource_manifest_json={},
                        created_at_utc=now,
                        activated_at_utc=None,
                        superseded_at_utc=None,
                        discarded_at_utc=None,
                    )
                )
                claimed = _insert_do_nothing(
                    connection,
                    upload_dedup_claims_table,
                    {
                        "space_id": submission["space_id"],
                        "normalized_filename": normalized_name,
                        "content_hash_sha256": submission["content_hash_sha256"],
                        "document_id": document_id,
                        "created_at_utc": now,
                    },
                    index_elements=["space_id", "normalized_filename", "content_hash_sha256"],
                )
                if not claimed:
                    self._service._object_store.delete(object_key)
                    winning_claim = (
                        connection.execute(
                            select(upload_dedup_claims_table.c.document_id).where(
                                and_(
                                    upload_dedup_claims_table.c.space_id == submission["space_id"],
                                    upload_dedup_claims_table.c.normalized_filename
                                    == normalized_name,
                                    upload_dedup_claims_table.c.content_hash_sha256
                                    == submission["content_hash_sha256"],
                                )
                            )
                        )
                        .mappings()
                        .one()
                    )
                    raise PlatformError(
                        "duplicate_document",
                        "A document with the same name and content already exists",
                        {"document_id": winning_claim["document_id"]},
                        409,
                    )
                grant_id = _new_id("grant")
                grant_fingerprint = self._service._idempotency_fingerprint(
                    {
                        "submission_id": submission_id,
                        "document_id": document_id,
                        "reviewer_user_id": actor_id,
                        "reviewer_role": str(principal.role),
                        "space_id": submission["space_id"],
                    }
                )
                connection.execute(
                    submission_execution_grants_table.insert().values(
                        id=grant_id,
                        submission_id=submission_id,
                        document_id=document_id,
                        document_version_id=version_id,
                        job_id=job_id,
                        submitter_user_id_snapshot=submission["submitter_user_id"],
                        reviewer_user_id_snapshot=actor_id,
                        reviewer_role_snapshot=str(principal.role),
                        department_id_snapshot=getattr(principal, "department_id", None),
                        space_id_snapshot=submission["space_id"],
                        policy_version="documents-v1",
                        capability="shared_submission_ingest",
                        fingerprint=grant_fingerprint,
                        created_at_utc=now,
                    )
                )
                connection.execute(
                    update(knowledge_submissions_table)
                    .where(knowledge_submissions_table.c.id == submission_id)
                    .values(
                        status="approved",
                        version=expected_version + 1,
                        reviewer_user_id=actor_id,
                        reviewer_role_snapshot=str(principal.role),
                        private_object_cleanup_requested_at_utc=now,
                        reviewed_at_utc=now,
                        updated_at_utc=now,
                    )
                )
                response = {
                    "submission_id": submission_id,
                    "version": expected_version + 1,
                    "status": "approved",
                    "document_id": document_id,
                    "document_version_id": version_id,
                    "job_id": job_id,
                    "grant_id": grant_id,
                }
                self._publish_submission_event(
                    connection,
                    event_type="submission_approved",
                    submission_id=submission_id,
                    transition_version=expected_version + 1,
                    recipient_user_id=str(submission["submitter_user_id"]),
                    occurred_at=now,
                )
            self._service._complete_idempotency(
                connection,
                actor_id=actor_id,
                endpoint=endpoint,
                target_id=submission_id,
                key=key,
                fingerprint=fingerprint,
                response=response,
            )
            return response

    def withdraw(
        self,
        *,
        principal: Any,
        submission_id: str,
        expected_version: int,
        idempotency_key: str | None,
    ) -> dict[str, Any]:
        return self._transition_owner(
            principal=principal,
            submission_id=submission_id,
            expected_version=expected_version,
            idempotency_key=idempotency_key,
            target_status="withdrawn",
        )

    def delete(
        self,
        *,
        principal: Any,
        submission_id: str,
        expected_version: int,
        idempotency_key: str | None,
    ) -> None:
        key = self._service._required_key(idempotency_key)
        actor_id = str(principal.user_id)
        endpoint = "documents.submission_delete"
        fingerprint = self._service._idempotency_fingerprint({"expected_version": expected_version})
        with self._service._engine.begin() as connection:
            replay = self._service._idempotency_replay(
                connection,
                actor_id=actor_id,
                endpoint=endpoint,
                target_id=submission_id,
                key=key,
                fingerprint=fingerprint,
            )
            if replay is not None:
                return None
            row = (
                connection.execute(
                    select(knowledge_submissions_table)
                    .where(knowledge_submissions_table.c.id == submission_id)
                    .with_for_update()
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                raise PlatformError("submission_not_found", "Submission was not found", {}, 404)
            if row["submitter_user_id"] != actor_id:
                raise PlatformError(
                    "submission_forbidden", "Submission ownership is required", {}, 403
                )
            if int(row["version"]) != expected_version:
                raise PlatformError(
                    "submission_version_conflict", "Submission version does not match", {}, 409
                )
            if row["status"] not in {"rejected", "withdrawn", "invalidated"}:
                raise PlatformError(
                    "submission_not_deletable", "Submission cannot be deleted", {}, 409
                )
            connection.execute(
                delete(knowledge_submissions_table).where(
                    knowledge_submissions_table.c.id == submission_id
                )
            )
            try:
                self._service._object_store.delete(str(row["private_object_key"]))
            except (StorageKeyError, KeyError):
                pass
            self._service._complete_idempotency(
                connection,
                actor_id=actor_id,
                endpoint=endpoint,
                target_id=submission_id,
                key=key,
                fingerprint=fingerprint,
                response={},
            )
        return None

    def _transition_owner(
        self,
        *,
        principal: Any,
        submission_id: str,
        expected_version: int,
        idempotency_key: str | None,
        target_status: str,
    ) -> dict[str, Any]:
        key = self._service._required_key(idempotency_key)
        actor_id = str(principal.user_id)
        endpoint = "documents.submission_withdraw"
        fingerprint = self._service._idempotency_fingerprint({"expected_version": expected_version})
        with self._service._engine.begin() as connection:
            row = (
                connection.execute(
                    select(knowledge_submissions_table)
                    .where(knowledge_submissions_table.c.id == submission_id)
                    .with_for_update()
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                raise PlatformError("submission_not_found", "Submission was not found", {}, 404)
            if row["submitter_user_id"] != actor_id:
                raise PlatformError(
                    "submission_forbidden", "Submission ownership is required", {}, 403
                )
            replay = self._service._idempotency_replay(
                connection,
                actor_id=actor_id,
                endpoint=endpoint,
                target_id=submission_id,
                key=key,
                fingerprint=fingerprint,
            )
            if replay is not None:
                return replay
            if int(row["version"]) != expected_version or row["status"] != "pending":
                raise PlatformError("submission_not_pending", "Submission is not pending", {}, 409)
            now = self._service._current_time()
            connection.execute(
                update(knowledge_submissions_table)
                .where(knowledge_submissions_table.c.id == submission_id)
                .values(
                    status=target_status,
                    version=expected_version + 1,
                    private_object_cleanup_requested_at_utc=now,
                    updated_at_utc=now,
                )
            )
            response = {
                "submission_id": submission_id,
                "version": expected_version + 1,
                "status": target_status,
            }
            self._service._complete_idempotency(
                connection,
                actor_id=actor_id,
                endpoint=endpoint,
                target_id=submission_id,
                key=key,
                fingerprint=fingerprint,
                response=response,
            )
            return response

    def cleanup_scheduled(self, *, limit: int = 100) -> list[str]:
        if limit < 1 or limit > 1000:
            raise PlatformError("validation_error", "cleanup limit is invalid", {}, 422)
        with self._service._engine.begin() as connection:
            rows = (
                connection.execute(
                    select(knowledge_submissions_table)
                    .where(
                        and_(
                            knowledge_submissions_table.c.status.in_(
                                ["approved", "withdrawn", "invalidated"]
                            ),
                            knowledge_submissions_table.c.private_object_cleanup_requested_at_utc.is_not(
                                None
                            ),
                            knowledge_submissions_table.c.private_object_cleaned_at_utc.is_(None),
                        )
                    )
                    .order_by(
                        knowledge_submissions_table.c.private_object_cleanup_requested_at_utc,
                        knowledge_submissions_table.c.id,
                    )
                    .limit(limit)
                    .with_for_update()
                )
                .mappings()
                .all()
            )
            now = self._service._current_time()
            cleaned: list[str] = []
            for row in rows:
                try:
                    self._service._object_store.delete(str(row["private_object_key"]))
                except (StorageKeyError, KeyError):
                    pass
                updated = connection.execute(
                    update(knowledge_submissions_table)
                    .where(
                        and_(
                            knowledge_submissions_table.c.id == row["id"],
                            knowledge_submissions_table.c.private_object_cleaned_at_utc.is_(None),
                        )
                    )
                    .values(private_object_cleaned_at_utc=now, updated_at_utc=now)
                ).rowcount
                if updated == 1:
                    cleaned.append(str(row["id"]))
            return cleaned

    @staticmethod
    def _review_allowed(principal: Any, space_id: str) -> bool:
        role = str(getattr(principal, "role", ""))
        if role == "admin":
            return True
        if space_id == "public":
            return role == "ops"
        return (
            space_id == f"department:{getattr(principal, 'department_id', None)}"
            and role == "minister"
        )

    @staticmethod
    def _public_row(row: Any) -> dict[str, Any]:
        return {
            "submission_id": row["id"],
            "space_id": row["space_id"],
            "version": row["version"],
            "status": row["status"],
            "file_name": row["file_name"],
            "media_kind": row["media_kind"],
            "created_at": row["created_at_utc"].isoformat(),
            "reviewed_at": row["reviewed_at_utc"].isoformat() if row["reviewed_at_utc"] else None,
        }
