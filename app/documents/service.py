from __future__ import annotations

import hashlib
import json
import logging
import secrets
from collections.abc import Callable, Mapping, Sequence
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, NoReturn

from sqlalchemy import and_, delete, func, insert, select, text, update
from sqlalchemy.engine import Connection, Engine

from app.documents.upload_security import (
    INJECTION_RISK_KIND,
    scan_prompt_injection_risk,
    validate_upload_security,
)
from app.identity.ports import DepartmentWorkState
from app.outbox.ports import DocumentNotificationRedactionCommand
from app.platform.context import current_context
from app.platform.database import _insert_do_nothing, platform_audit_table
from app.platform.errors import PlatformError
from app.platform.http_contract import IDEMPOTENCY_KEY_MAX_LENGTH
from app.platform.storage import MemoryObjectStore, ObjectMetadata, ObjectStorePort, StorageKeyError

from .domain import (
    DocumentLifecycle,
    DocumentVersionState,
    IngestionJobState,
    PublicationState,
    canonical_request_fingerprint,
)
from .indexing import IndexProcessingReceipt
from .preview import PreviewContent
from .schema import (
    document_deletion_cleanup_targets_table,
    document_deletions_table,
    document_read_leases_table,
    document_version_cleanup_targets_table,
    document_version_restore_holds_table,
    document_versions_table,
    documents_idempotency_table,
    documents_instance_counters_table,
    documents_table,
    index_changes_table,
    index_revisions_table,
    ingestion_attempts_table,
    ingestion_jobs_table,
    knowledge_submissions_table,
    publications_table,
    submission_execution_grants_table,
    upload_batch_items_table,
    upload_batches_table,
    upload_dedup_claims_table,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class DocumentUpload:
    filename: str
    content: bytes
    media_kind: str = "application/octet-stream"

    def __post_init__(self) -> None:
        if not isinstance(self.content, bytes):
            raise TypeError("upload content must be bytes")
        if not self.filename or not self.filename.strip():
            raise PlatformError("validation_error", "filename is required", {}, 422)
        if len(self.filename.strip()) > 512:
            raise PlatformError("validation_error", "filename is too long", {}, 422)
        if not self.media_kind or len(self.media_kind) > 128:
            raise PlatformError("validation_error", "media_kind is invalid", {}, 422)


@dataclass(frozen=True, slots=True)
class DocumentVersionReference:
    """A renewable read lease on one document version.

    Readers hold the `(reference_id, owner_id, lease_token)` triple and renew
    with it before `expires_at`; the token stays stable across renewals and is
    never reused across acquisitions.
    """

    reference_id: str
    owner_id: str
    lease_token: str
    document_id: str
    document_version_id: str
    expires_at: datetime


# Supported upload media kinds, mirroring the frontend upload contract
# (frontend/src/mocks/knowledge-contract.ts MEDIA_KIND_BY_TYPE + extensions).
_UPLOAD_MEDIA_KINDS_BY_TYPE: dict[str, str] = {
    "application/pdf": "pdf",
    "application/msword": "word",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "word",
    "text/markdown": "md",
    "text/plain": "txt",
    "application/vnd.ms-excel": "excel",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "excel",
}
_UPLOAD_MEDIA_KINDS_BY_EXTENSION: dict[str, str] = {
    "pdf": "pdf",
    "doc": "word",
    "docx": "word",
    "md": "md",
    "txt": "txt",
    "xls": "excel",
    "xlsx": "excel",
}


CODE_TOKENS_PER_PAGE = 500


def _metered_pages(
    *, page_count: Any, image_count: Any, summary: Mapping[str, Any] | None = None
) -> int:
    """Metering converts the processing summary into billable pages.

    Document carriers keep the existing page_count + image_count fold (1 image
    = 1 page). Routed carriers follow the quota conversion rules: code files
    meter at ceil(tokens/500) with a 1-page minimum, config files meter only
    above 500 tokens, structured-table routes are free, and internvl images
    record usage without debiting pages.
    """

    pages = (
        page_count
        if isinstance(page_count, int) and not isinstance(page_count, bool) and page_count > 0
        else 0
    )
    images = (
        image_count
        if isinstance(image_count, int) and not isinstance(image_count, bool) and image_count > 0
        else 0
    )
    metering = summary.get("metering") if isinstance(summary, Mapping) else None
    metering_class = str(metering.get("class", "")) if isinstance(metering, Mapping) else ""
    if metering_class == "table":
        return 0
    if metering_class in {"code", "config"}:
        tokens = metering.get("token_count") if isinstance(metering, Mapping) else None
        token_count = (
            tokens if isinstance(tokens, int) and not isinstance(tokens, bool) and tokens > 0 else 0
        )
        metered = -(-token_count // CODE_TOKENS_PER_PAGE)
        if metering_class == "code":
            return max(metered, 1)
        return 0 if token_count <= CODE_TOKENS_PER_PAGE else metered
    if metering_class == "image":
        provider = metering.get("image_provider") if isinstance(metering, Mapping) else None
        return 0 if provider == "internvl" else images
    # B4 内嵌图片（PDF figure 块）：internvl 只记用量不扣页；其余 provider 按
    # 既有文档载体 pages+images 折叠（1 图=1 页）。
    embedded_provider = (
        metering.get("embedded_image_provider") if isinstance(metering, Mapping) else None
    )
    if embedded_provider == "internvl":
        return pages
    return pages + images


def _new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_urlsafe(18)}"


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _timestamp(value: datetime) -> str:
    return _utc(value).isoformat()


def _json(value: Any) -> Any:
    if isinstance(value, datetime):
        return _timestamp(value)
    if isinstance(value, Mapping):
        return {str(key): _json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json(item) for item in value]
    return value


class DocumentsDepartmentWorkCheckPort:
    """Identity-facing read adapter for active document work."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def inspect(self, department_id: str, *, connection: Connection) -> DepartmentWorkState:
        department_spaces = select(documents_table.c.id).where(
            documents_table.c.space_id == f"department:{department_id}"
        )
        document_ids = [row[0] for row in connection.execute(department_spaces).all()]
        if not document_ids:
            pending_jobs = 0
        else:
            pending_jobs = int(
                connection.execute(
                    select(func.count())
                    .select_from(ingestion_jobs_table)
                    .where(
                        and_(
                            ingestion_jobs_table.c.document_id.in_(document_ids),
                            ingestion_jobs_table.c.state.in_(
                                [
                                    IngestionJobState.PENDING.value,
                                    IngestionJobState.RUNNING.value,
                                    IngestionJobState.RETRY_WAIT.value,
                                ]
                            ),
                        )
                    )
                ).scalar_one()
            )
        submissions = int(
            connection.execute(
                select(func.count())
                .select_from(knowledge_submissions_table)
                .where(
                    and_(
                        knowledge_submissions_table.c.space_id == f"department:{department_id}",
                        knowledge_submissions_table.c.status == "pending",
                    )
                )
            ).scalar_one()
        )
        return DepartmentWorkState(
            nonterminal_job_count=pending_jobs,
            pending_submission_count=submissions,
        )

    def directory_counts(
        self, department_id: str, *, connection: Connection
    ) -> DepartmentWorkState:
        work = self.inspect(department_id, connection=connection)
        documents = int(
            connection.execute(
                select(func.count())
                .select_from(documents_table)
                .where(
                    and_(
                        documents_table.c.space_id == f"department:{department_id}",
                        documents_table.c.lifecycle_status != "deleted",
                    )
                )
            ).scalar_one()
        )
        return DepartmentWorkState(
            document_count=documents,
            nonterminal_job_count=work.nonterminal_job_count,
            pending_submission_count=work.pending_submission_count,
        )

    def user_document_counts(
        self, user_ids: Sequence[str], *, connection: Connection
    ) -> dict[str, int]:
        if not user_ids:
            return {}
        rows = connection.execute(
            select(documents_table.c.created_by_user_id, func.count())
            .where(
                and_(
                    documents_table.c.created_by_user_id.in_(list(user_ids)),
                    documents_table.c.lifecycle_status != "deleted",
                )
            )
            .group_by(documents_table.c.created_by_user_id)
        ).all()
        return {str(user_id): int(count) for user_id, count in rows}


class DocumentsService:
    """Transactional command/read service for the greenfield documents domain.

    The service owns durable document facts. Processing providers interact through
    ``accept_processing_receipt``; no parser, OCR, embedding, or retrieval code is
    included here.
    """

    def __init__(
        self,
        engine: Engine,
        *,
        now: Callable[[], datetime] | None = None,
        object_store: ObjectStorePort | None = None,
        identity_access: Any | None = None,
        lifecycle_port: Any | None = None,
        indexing_handoff_port: Any | None = None,
        quota_service: Any | None = None,
        submission_notification_port: Any | None = None,
        ingestion_notification_port: Any | None = None,
        public_graph_source_service: Any | None = None,
        preview_renderer: Any | None = None,
        message_citation_preview_port: Any | None = None,
        version_retention_days: int = 30,
        read_lease_ttl: timedelta = timedelta(minutes=5),
        max_upload_bytes: int = 25 * 1024 * 1024,
        cleanup_max_attempts: int = 3,
        malware_scanner: Any | None = None,
    ) -> None:
        if max_upload_bytes < 1:
            raise ValueError("max_upload_bytes must be positive")
        if cleanup_max_attempts < 1:
            raise ValueError("cleanup_max_attempts must be positive")
        self._engine = engine
        self._now = now or (lambda: datetime.now(UTC))
        self._object_store = object_store or MemoryObjectStore()
        self._identity_access = identity_access
        self._lifecycle_port = lifecycle_port
        self._indexing_handoff_port = indexing_handoff_port
        self._quota_service = quota_service
        self._submission_notification_port = submission_notification_port
        self._ingestion_notification_port = ingestion_notification_port
        self._public_graph_source_service = public_graph_source_service
        self._preview_renderer = preview_renderer
        self._message_citation_preview_port = message_citation_preview_port
        self._version_retention_days = version_retention_days
        self._read_lease_ttl = read_lease_ttl
        self._max_upload_bytes = max_upload_bytes
        self._cleanup_max_attempts = cleanup_max_attempts
        self._malware_scanner = malware_scanner

    def _current_time(self) -> datetime:
        return _utc(self._now())

    def _current_index_generation(self, connection: Connection) -> str:
        handoff = self._indexing_handoff_port
        generation = getattr(handoff, "generation", None)
        repository = getattr(generation, "_repository", None)
        if repository is not None:
            return str(repository.active_generation_id(connection=connection))
        if generation is not None:
            return str(generation.active_generation_id)
        return "generation_initial"

    def _check_quota(self, connection: Connection, principal: Any, *, pages: int = 1) -> None:
        if self._quota_service is None:
            return
        checker = getattr(self._quota_service, "check", None) or getattr(
            self._quota_service, "check_direct_ingest_balance", None
        )
        if callable(checker):
            checker(
                connection,
                quota_subject_user_id=str(principal.user_id),
                pages=pages,
                role=str(getattr(principal, "role", "user")),
            )

    @staticmethod
    def _publication_space_ownership(
        *, space_id: object, subject_user_id: str
    ) -> tuple[str, str | None, str | None]:
        """Derive publication cost ownership from the trusted document space id.

        ``public`` and ``department:<id>`` are the durable space identities used by
        the identity service. Personal/legacy test spaces remain user-owned without
        being misclassified as public.
        """
        trusted_space_id = str(space_id)
        if trusted_space_id == "public":
            return "public", "public", None
        if trusted_space_id.startswith("department:"):
            return trusted_space_id, "department", None
        if trusted_space_id.startswith("personal:"):
            return f"user:{subject_user_id}", "personal", subject_user_id
        return f"user:{subject_user_id}", None, None

    def _record_publication_quota(
        self,
        connection: Connection,
        *,
        job: Mapping[Any, Any],
        publication: Mapping[Any, Any],
        document: Mapping[Any, Any],
        receipt: Mapping[Any, Any],
        published_at: datetime,
    ) -> dict[str, Any] | None:
        if self._quota_service is None:
            return None
        recorder = getattr(self._quota_service, "record", None) or getattr(
            self._quota_service, "record_publication_debit", None
        )
        calendar = getattr(self._quota_service, "calendar", None)
        if not callable(recorder) or calendar is None:
            return None
        summary = receipt.get("processing_summary") or {}
        processing_list = summary.get("processing_list")
        processing_list_id = (
            str(processing_list.get("processing_list_id"))
            if isinstance(processing_list, Mapping) and processing_list.get("frozen") is True
            else None
        )
        if not processing_list_id:
            raise PlatformError(
                "validation_error", "Processing receipt has no frozen processing list", {}, 422
            )
        pages = _metered_pages(
            page_count=receipt.get(
                "page_count", summary.get("page_count", summary.get("pages", 1))
            ),
            image_count=summary.get("image_count", summary.get("images", 0)),
            summary=summary,
        )
        if pages < 1 and not isinstance(summary.get("metering"), Mapping):
            raise PlatformError(
                "validation_error", "Processing receipt page count is invalid", {}, 422
            )
        from app.usage.ledger import OwnershipSnapshot

        subject = str(job["created_by_user_id"])
        role = str(job["quota_role_snapshot"])
        cost_center_key, space_kind, space_owner_user_id = self._publication_space_ownership(
            space_id=document["space_id"], subject_user_id=subject
        )
        ownership = OwnershipSnapshot(
            actor_user_id=subject,
            actor_role_snapshot=role,
            actor_department_id_snapshot=job["quota_department_id_snapshot"],
            quota_subject_user_id=subject,
            cost_center_key=cost_center_key,
            space_id=document["space_id"],
            space_kind=space_kind,
            space_owner_user_id=space_owner_user_id,
            source_space_ids=(str(document["space_id"]),),
        )
        debit_id = recorder(
            connection,
            publication_status="succeeded",
            quota_operation_id=processing_list_id,
            publication_id=str(publication["id"]),
            quota_subject_user_id=subject,
            pages=pages,
            ownership=ownership,
            calendar_lock=calendar.lock_or_verify(connection),
            role=role,
            quota_exempt_reason=job["quota_exempt_reason"],
            replay_generation=int(job["replay_generation"]),
            published_at=published_at,
        )
        if debit_id is not None:
            charge_status = "charged"
            charge_reason = None
        elif job["quota_exempt_reason"] is not None:
            charge_status = "exempt"
            charge_reason = str(job["quota_exempt_reason"])
        elif str(role) in {"ops", "admin"}:
            charge_status = "exempt"
            charge_reason = "unlimited_role"
        elif int(job["replay_generation"]) > 0:
            charge_status = "exempt"
            charge_reason = "replay_generation"
        else:
            charge_status = "not_charged"
            charge_reason = "quota_debit_not_recorded"
        return {
            "pages": pages,
            "processing_list_id": processing_list_id,
            "quota_debit_id": debit_id,
            "quota_charge_status": charge_status,
            "quota_charge_reason": charge_reason,
        }

    def _publish_ingestion_notifications(
        self,
        connection: Connection,
        *,
        job: Mapping[Any, Any],
        publication: Mapping[Any, Any],
        receipt: Mapping[Any, Any],
        occurred_at: datetime,
    ) -> list[str]:
        port = self._ingestion_notification_port
        if port is None:
            return []
        event_ids = port.publish_ingestion_events(
            job_id=str(job["id"]),
            document_id=str(job["document_id"]),
            document_version_id=str(job["document_version_id"]),
            publication_id=str(publication["id"]),
            transition_version=int(job["replay_generation"]) + 1,
            recipient_user_id=str(job["created_by_user_id"]),
            occurred_at=occurred_at,
            ocr_low_confidence=bool(receipt.get("ocr_low_confidence", False)),
            ocr_low_confidence_fact=receipt.get("ocr_low_confidence_fact"),
            connection=connection,
        )
        return [str(event_id) for event_id in event_ids]

    @staticmethod
    def _index_request_from_attempt(attempt: Mapping[Any, Any]):
        from .indexing import IndexStagingRequest

        value = dict(attempt["staging_request_json"] or {})
        if not value:
            return None
        try:
            return IndexStagingRequest.from_mapping(value)
        except PlatformError as exc:
            raise PlatformError(
                "processing_receipt_conflict",
                "The stored staging request is invalid",
                {},
                409,
            ) from exc

    def _discard_indexing_attempt(self, connection: Connection, attempt_id: str | None) -> None:
        if self._indexing_handoff_port is None or not attempt_id:
            return
        attempt = (
            connection.execute(
                select(ingestion_attempts_table).where(ingestion_attempts_table.c.id == attempt_id)
            )
            .mappings()
            .one_or_none()
        )
        if attempt is None:
            return
        request = self._index_request_from_attempt(attempt)
        if request is not None:
            self._indexing_handoff_port.discard(request, connection=connection)

    @staticmethod
    def _worker_authorization_fence(
        connection: Connection, *, job: Mapping[Any, Any], document: Mapping[Any, Any]
    ) -> dict[str, str]:
        if job["quota_exempt_reason"] != "shared_library_submission":
            return {"kind": "direct_ingest", "actor_id": str(job["created_by_user_id"])}
        grant = (
            connection.execute(
                select(submission_execution_grants_table)
                .where(submission_execution_grants_table.c.job_id == job["id"])
                .with_for_update()
            )
            .mappings()
            .one_or_none()
        )
        if (
            grant is None
            or str(grant["document_id"]) != str(job["document_id"])
            or str(grant["document_version_id"]) != str(job["document_version_id"])
            or str(grant["space_id_snapshot"]) != str(document["space_id"])
        ):
            raise PlatformError(
                "submission_grant_invalid",
                "The submission execution grant is no longer valid",
                {},
                409,
            )
        return {
            "kind": "submission_execution_grant",
            "grant_id": str(grant["id"]),
            "submission_id": str(grant["submission_id"]),
        }

    def _assert_replay_eligible(
        self,
        connection: Connection,
        *,
        job: Mapping[Any, Any],
        document: Mapping[Any, Any],
        principal: Any,
    ) -> None:
        self._authorize(principal, str(document["space_id"]), "manage")
        if document["lifecycle_status"] == DocumentLifecycle.PENDING_DELETE.value:
            raise PlatformError("document_pending_delete", "Document is pending deletion", {}, 409)
        if document["lifecycle_status"] == DocumentLifecycle.DELETED.value:
            raise PlatformError("document_deleted", "Document has been deleted", {}, 410)
        if document["active_operation_job_id"] not in {None, job["id"]}:
            raise PlatformError(
                "document_operation_in_progress", "A document operation is active", {}, 409
            )
        if document["active_version_id"] != job["base_active_version_id"]:
            raise PlatformError(
                "document_version_changed",
                "The document active version has changed since this job was created",
                {},
                409,
            )
        self._worker_authorization_fence(connection, job=job, document=document)
        version_id = job["document_version_id"]
        if not version_id:
            raise PlatformError(
                "document_version_purged", "Document version content was purged", {}, 410
            )
        version = (
            connection.execute(
                select(document_versions_table).where(document_versions_table.c.id == version_id)
            )
            .mappings()
            .one_or_none()
        )
        if version is None or version["status"] == DocumentVersionState.PURGED.value:
            raise PlatformError(
                "document_version_purged", "Document version content was purged", {}, 410
            )
        purge_after = version["purge_after_at_utc"]
        if purge_after is not None and _utc(purge_after) <= self._current_time():
            raise PlatformError(
                "document_version_purged", "Document version retention has expired", {}, 410
            )
        object_key = str(version["original_object_key"] or "").strip()
        expected_hash = str(version["content_hash_sha256"] or "").strip()
        if not object_key or not expected_hash:
            raise PlatformError(
                "document_version_purged", "Document version content was purged", {}, 410
            )
        # Content integrity is trusted from the hash stored at upload time; the object
        # body is re-read only when replay actually stages a new processing attempt.

    def _can_replay_job(
        self, connection: Connection, *, job: Mapping[Any, Any], principal: Any
    ) -> bool:
        document = (
            connection.execute(
                select(documents_table).where(documents_table.c.id == job["document_id"])
            )
            .mappings()
            .one_or_none()
        )
        if document is None:
            return False
        try:
            self._assert_replay_eligible(
                connection,
                job=job,
                document=document,
                principal=principal,
            )
        except PlatformError:
            return False
        return True

    def _validate_direct_receipt_authorization(
        self, *, principal: Any | None, job: Mapping[Any, Any], document: Mapping[Any, Any]
    ) -> None:
        if job["quota_exempt_reason"] == "shared_library_submission":
            return
        if principal is None or str(getattr(principal, "user_id", "")) != str(
            job["created_by_user_id"]
        ):
            raise PlatformError(
                "authorization_changed",
                "The direct-ingest authorization is no longer current",
                {},
                409,
            )
        try:
            self._authorize(principal, str(document["space_id"]), "manage")
        except PlatformError as exc:
            raise PlatformError(
                "authorization_changed",
                "The direct-ingest authorization is no longer current",
                {},
                409,
            ) from exc

    def _acquire_read_lease(
        self,
        connection: Connection,
        *,
        document_id: str,
        document_version_id: str,
        principal_id: str,
    ) -> DocumentVersionReference:
        now = self._current_time()
        expires = now + self._read_lease_ttl
        reference_id = _new_id("read_lease")
        lease_token = secrets.token_hex(16)
        # A re-acquisition supersedes the caller's previous reference so
        # references and tokens are never reused across requests.
        connection.execute(
            delete(document_read_leases_table).where(
                and_(
                    document_read_leases_table.c.document_version_id == document_version_id,
                    document_read_leases_table.c.principal_id == principal_id,
                )
            )
        )
        connection.execute(
            document_read_leases_table.insert().values(
                id=reference_id,
                document_id=document_id,
                document_version_id=document_version_id,
                principal_id=principal_id,
                lease_token=lease_token,
                expires_at_utc=expires,
                created_at_utc=now,
                updated_at_utc=now,
            )
        )
        return DocumentVersionReference(
            reference_id=reference_id,
            owner_id=principal_id,
            lease_token=lease_token,
            document_id=document_id,
            document_version_id=document_version_id,
            expires_at=expires,
        )

    def renew_read_lease(
        self,
        *,
        reference_id: str,
        owner_id: str,
        lease_token: str,
    ) -> DocumentVersionReference:
        """Conditionally renew a read lease before it expires.

        Mirrors the acquisition lock order (logical document, then document
        version) so renewal stays mutually exclusive with entering ``purging``
        and deletion acceptance.  Every failure means the reader must stop
        reading immediately and must not return unfinished content.
        """
        with self._engine.begin() as connection:
            lease = (
                connection.execute(
                    select(document_read_leases_table).where(
                        and_(
                            document_read_leases_table.c.id == reference_id,
                            document_read_leases_table.c.principal_id == owner_id,
                            document_read_leases_table.c.lease_token == lease_token,
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
            if lease is None:
                raise PlatformError(
                    "read_lease_unavailable", "The read lease is no longer renewable", {}, 409
                )
            document = self._locked_document(connection, str(lease["document_id"]))
            if document is None or document["lifecycle_status"] != DocumentLifecycle.ACTIVE.value:
                raise PlatformError(
                    "read_lease_unavailable", "The read lease is no longer renewable", {}, 409
                )
            version = (
                connection.execute(
                    select(document_versions_table)
                    .where(document_versions_table.c.id == str(lease["document_version_id"]))
                    .with_for_update()
                )
                .mappings()
                .one_or_none()
            )
            if version is None or version["status"] in {
                DocumentVersionState.PURGING.value,
                DocumentVersionState.PURGED.value,
            }:
                raise PlatformError(
                    "read_lease_unavailable", "The read lease is no longer renewable", {}, 409
                )
            now = self._current_time()
            expires = now + self._read_lease_ttl
            renewed = connection.execute(
                update(document_read_leases_table)
                .where(
                    and_(
                        document_read_leases_table.c.id == reference_id,
                        document_read_leases_table.c.principal_id == owner_id,
                        document_read_leases_table.c.lease_token == lease_token,
                        document_read_leases_table.c.expires_at_utc > now,
                    )
                )
                .values(expires_at_utc=expires, updated_at_utc=now)
            )
            if renewed.rowcount != 1:
                raise PlatformError(
                    "read_lease_unavailable", "The read lease is no longer renewable", {}, 409
                )
            return DocumentVersionReference(
                reference_id=reference_id,
                owner_id=owner_id,
                lease_token=lease_token,
                document_id=str(lease["document_id"]),
                document_version_id=str(lease["document_version_id"]),
                expires_at=expires,
            )

    @staticmethod
    def _tombstone_version(connection: Connection, version_id: str, now: datetime) -> None:
        connection.execute(
            update(document_versions_table)
            .where(document_versions_table.c.id == version_id)
            .values(
                status=DocumentVersionState.PURGED.value,
                content_hash_sha256=None,
                object_manifest_json={},
                original_object_key=None,
                file_name=None,
                media_kind=None,
                size_bytes=0,
                created_by_user_id=None,
                terminal_at_utc=now,
                purge_after_at_utc=now,
                purged_at_utc=now,
                updated_at_utc=now,
            )
        )
        context = current_context()
        connection.execute(
            platform_audit_table.insert().values(
                actor_id="system_purge_worker",
                resource_type="documents.version_purged",
                resource_id=version_id,
                request_id=context.request_id if context is not None else "req_documents",
                occurred_at_utc=now,
                result="succeeded",
                details_json={},
            )
        )

    @staticmethod
    def _has_active_read_lease(
        connection: Connection,
        *,
        now: datetime,
        document_id: str | None = None,
        document_version_id: str | None = None,
    ) -> bool:
        conditions = [document_read_leases_table.c.expires_at_utc > now]
        if document_id is not None:
            conditions.append(document_read_leases_table.c.document_id == document_id)
        if document_version_id is not None:
            conditions.append(
                document_read_leases_table.c.document_version_id == document_version_id
            )
        return bool(
            connection.execute(
                select(func.count())
                .select_from(document_read_leases_table)
                .where(and_(*conditions))
            ).scalar_one()
        )

    @staticmethod
    def _has_active_restore_hold(connection: Connection, *, document_version_id: str) -> bool:
        return bool(
            connection.execute(
                select(func.count())
                .select_from(
                    document_version_restore_holds_table.join(
                        ingestion_jobs_table,
                        ingestion_jobs_table.c.id == document_version_restore_holds_table.c.job_id,
                    )
                )
                .where(
                    and_(
                        document_version_restore_holds_table.c.document_version_id
                        == document_version_id,
                        ingestion_jobs_table.c.state.in_(
                            [
                                IngestionJobState.PENDING.value,
                                IngestionJobState.RUNNING.value,
                                IngestionJobState.RETRY_WAIT.value,
                            ]
                        ),
                    )
                )
            ).scalar_one()
        )

    @staticmethod
    def _cleanup_phase(backend_kind: str) -> int:
        if backend_kind in {"cache", "index"}:
            return 0
        if backend_kind in {"parsing", "chunk"}:
            return 1
        if backend_kind in {"publication", "staging"}:
            return 2
        return 3

    def _cleanup_resources_for_version(
        self, connection: Connection, *, version: Mapping[str, Any]
    ) -> list[dict[str, Any]]:
        resources: dict[tuple[str, str], dict[str, Any]] = {}

        def add(backend_kind: object, resource_id: object, **extra: Any) -> None:
            if not isinstance(backend_kind, str) or not backend_kind.strip():
                return
            if not isinstance(resource_id, str) or not resource_id.strip():
                return
            resources.setdefault(
                (backend_kind, resource_id),
                {
                    "backend_kind": backend_kind,
                    "resource_id": resource_id,
                    **extra,
                    "document_id": version["document_id"],
                    "document_version_id": version["id"],
                },
            )

        publications = (
            connection.execute(
                select(publications_table).where(
                    publications_table.c.document_version_id == version["id"]
                )
            )
            .mappings()
            .all()
        )
        for publication in publications:
            manifest = dict(publication["resource_manifest_json"] or {})
            add(
                "publication",
                str(publication["id"]),
                publication_id=str(publication["id"]),
            )
            add("content_manifest", manifest.get("content_manifest_id"))
            for resource in manifest.get("stage_resources", []):
                if not isinstance(resource, Mapping):
                    continue
                add(
                    resource.get("backend_kind"),
                    resource.get("resource_id"),
                    **{
                        key: value
                        for key, value in resource.items()
                        if key not in {"backend_kind", "resource_id"}
                    },
                )
        add("object_store", version["original_object_key"])
        return sorted(
            resources.values(),
            key=lambda resource: (
                self._cleanup_phase(str(resource["backend_kind"])),
                str(resource["backend_kind"]),
                str(resource["resource_id"]),
            ),
        )

    def _stage_cleanup_targets(
        self,
        connection: Connection,
        *,
        target_table: Any,
        owner_field: str,
        owner_id: str,
        version: Mapping[str, Any],
        now: datetime,
    ) -> bool:
        """Durably record cleanup targets; return True once every target is completed.

        Performs database writes only: the actual external deletions run in
        ``_execute_cleanup_targets`` after the caller's transaction commits.
        """
        resources = self._cleanup_resources_for_version(connection, version=version)
        for resource in resources:
            _insert_do_nothing(
                connection,
                target_table,
                {
                    owner_field: owner_id,
                    "backend_kind": str(resource["backend_kind"]),
                    "resource_id": str(resource["resource_id"]),
                    "state": "pending",
                    "attempt_count": 0,
                    "last_error": None,
                    "created_at_utc": now,
                    "updated_at_utc": now,
                },
                index_elements=[owner_field, "backend_kind", "resource_id"],
            )
        owner_column = target_table.c[owner_field]
        for resource in resources:
            target = (
                connection.execute(
                    select(target_table)
                    .where(
                        and_(
                            owner_column == owner_id,
                            target_table.c.backend_kind == str(resource["backend_kind"]),
                            target_table.c.resource_id == str(resource["resource_id"]),
                        )
                    )
                    .with_for_update()
                )
                .mappings()
                .one()
            )
            if target["state"] == "completed":
                continue
            return False
        return True

    def _execute_cleanup_targets(
        self,
        target_table: Any,
        owner_field: str,
        owner_id: str,
        *,
        resource_context: Mapping[str, Any],
        retry_audit_resource_type: str | None = None,
    ) -> tuple[int, bool]:
        """Run one pass of staged cleanup deletions outside any caller transaction.

        Targets are claimed and marked in short transactions of their own, while external
        I/O receives no database connection. One pass per invocation and the pass stops at
        the first failure, mirroring the historical retry cadence:
        failed targets are retried on the next pass until the attempt budget is used.
        Returns how many targets were newly completed and whether any target failed.
        """
        owner_column = target_table.c[owner_field]
        now = self._current_time()
        with self._engine.connect() as connection:
            targets = connection.execute(
                select(target_table.c.backend_kind, target_table.c.resource_id).where(
                    and_(
                        owner_column == owner_id,
                        target_table.c.state != "completed",
                    )
                )
            ).all()
        completed = 0
        for backend_kind, resource_id in sorted(
            targets,
            key=lambda item: (
                self._cleanup_phase(str(item[0])),
                str(item[0]),
                str(item[1]),
            ),
        ):
            with self._engine.begin() as connection:
                target = (
                    connection.execute(
                        select(target_table)
                        .where(
                            and_(
                                owner_column == owner_id,
                                target_table.c.backend_kind == backend_kind,
                                target_table.c.resource_id == resource_id,
                            )
                        )
                        .with_for_update()
                    )
                    .mappings()
                    .one_or_none()
                )
                if target is None or target["state"] == "completed":
                    continue
                if (
                    target["state"] == "failed"
                    and int(target["attempt_count"]) >= self._cleanup_max_attempts
                ):
                    return completed, False
                attempts = int(target["attempt_count"]) + 1
                connection.execute(
                    update(target_table)
                    .where(
                        and_(
                            owner_column == owner_id,
                            target_table.c.backend_kind == backend_kind,
                            target_table.c.resource_id == resource_id,
                        )
                    )
                    .values(
                        state="pending",
                        attempt_count=attempts,
                        last_error=None,
                        updated_at_utc=now,
                    )
                )
            resource = {
                **resource_context,
                "backend_kind": str(backend_kind),
                "resource_id": str(resource_id),
            }
            error: str | None = None
            try:
                if resource["backend_kind"] == "object_store":
                    self._object_store.delete(str(resource["resource_id"]))
                else:
                    cleanup = getattr(self._indexing_handoff_port, "cleanup_resource", None)
                    if not callable(cleanup):
                        raise RuntimeError("indexing cleanup handoff is not configured")
                    cleanup(resource, connection=None)
            except (StorageKeyError, KeyError):
                error = None
            except Exception as exc:
                error = type(exc).__name__
            with self._engine.begin() as connection:
                if error is None:
                    connection.execute(
                        update(target_table)
                        .where(
                            and_(
                                owner_column == owner_id,
                                target_table.c.backend_kind == backend_kind,
                                target_table.c.resource_id == resource_id,
                            )
                        )
                        .values(state="completed", last_error=None, updated_at_utc=now)
                    )
                    completed += 1
                else:
                    connection.execute(
                        update(target_table)
                        .where(
                            and_(
                                owner_column == owner_id,
                                target_table.c.backend_kind == backend_kind,
                                target_table.c.resource_id == resource_id,
                            )
                        )
                        .values(state="failed", last_error=error, updated_at_utc=now)
                    )
                    if retry_audit_resource_type is not None:
                        # §9.3 审计事实：清理重试（与失败标记同一事务边界）。
                        self._audit(
                            connection,
                            actor_id="system_purge_worker",
                            resource_type=retry_audit_resource_type,
                            resource_id=str(owner_id),
                            result="retried",
                            occurred_at=now,
                        )
                    return completed, True
        return completed, False

    @staticmethod
    def _normalize_filename(filename: str) -> tuple[str, str]:
        normalized = " ".join(filename.strip().split())
        if not normalized or "/" in normalized or "\\" in normalized or "\x00" in normalized:
            raise PlatformError("validation_error", "filename is invalid", {}, 422)
        return normalized, normalized.casefold()

    @staticmethod
    def _locked_document(connection: Connection, document_id: str) -> Mapping[Any, Any] | None:
        row = (
            connection.execute(
                select(documents_table).where(documents_table.c.id == document_id).with_for_update()
            )
            .mappings()
            .one_or_none()
        )
        return dict(row) if row is not None else None

    @staticmethod
    def _hash(content: bytes) -> str:
        return hashlib.sha256(content).hexdigest()

    @staticmethod
    def _idempotency_fingerprint(value: Any) -> str:
        return canonical_request_fingerprint(value)

    @staticmethod
    def _outbox_redaction_fingerprint(command: DocumentNotificationRedactionCommand) -> str:
        encoded = json.dumps(
            {
                "kind": "redact",
                "deletion_id": command.deletion_id,
                "document_id": command.document_id,
                "document_version_ids": list(command.document_version_ids),
                "reason": command.reason,
                "transaction_id": command.transaction_id,
                "mode": command.mode,
            },
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        return hashlib.sha256(b"outbox-lifecycle-v1\0" + encoded.encode("utf-8")).hexdigest()

    @staticmethod
    def _required_key(value: str | None) -> str:
        if not value or not value.strip():
            raise PlatformError("validation_error", "Idempotency-Key is required", {}, 422)
        key = value.strip()
        if len(key) > 256:
            raise PlatformError(
                "validation_error", "Idempotency-Key is too long", {"max_length": 256}, 422
            )
        return key

    def _authorize(
        self,
        principal: Any,
        space_id: str,
        action: str,
        connection: Connection | None = None,
    ) -> str:
        if self._identity_access is None:
            return "manage"
        return str(
            self._identity_access.authorize_space(
                principal=principal,
                space_id=space_id,
                action=action,
                connection=connection,
            )
        )

    @property
    def max_upload_bytes(self) -> int:
        return self._max_upload_bytes

    def _authorize_upload(self, principal: Any, space_id: str) -> str:
        try:
            return self._authorize(principal, space_id, "manage")
        except PlatformError as error:
            if error.code != "space_action_forbidden":
                raise
            return self._authorize(principal, space_id, "contribute")

    def create_upload(
        self,
        *,
        principal: Any,
        space_id: str,
        files: Sequence[DocumentUpload],
        idempotency_key: str | None,
    ) -> dict[str, Any]:
        key = self._required_key(idempotency_key)
        if not files:
            raise PlatformError("validation_error", "At least one file is required", {}, 422)
        if self._authorize_upload(principal, space_id) == "contribute":
            items: list[dict[str, Any]] = []
            for index, file in enumerate(files):
                submission_key = f"{key}:{index}"
                item_index: int | None = None
                if len(submission_key) > IDEMPOTENCY_KEY_MAX_LENGTH:
                    submission_key = key
                    item_index = index
                items.append(
                    self.create_submission(
                        principal=principal,
                        space_id=space_id,
                        file=file,
                        idempotency_key=submission_key,
                        idempotency_item_index=item_index,
                    )
                )
            return {"items": items}
        return self.create_initial_upload(
            principal=principal, space_id=space_id, files=files, idempotency_key=key
        )

    def _idempotency_replay(
        self,
        connection: Connection,
        *,
        actor_id: str,
        endpoint: str,
        target_id: str,
        key: str,
        fingerprint: str,
    ) -> dict[str, Any] | None:
        key_hash = self._hash(key.encode("utf-8"))
        predicate = and_(
            documents_idempotency_table.c.actor_id == actor_id,
            documents_idempotency_table.c.endpoint == endpoint,
            documents_idempotency_table.c.target_id == target_id,
            documents_idempotency_table.c.idempotency_key_hash == key_hash,
        )

        def resolve(row: Mapping[str, Any]) -> dict[str, Any] | None:
            if row["request_fingerprint"] != fingerprint:
                raise PlatformError(
                    "idempotency_key_conflict",
                    "The idempotency key conflicts with a previous request",
                    {},
                    409,
                )
            if row["status"] != "completed":
                raise PlatformError(
                    "idempotency_in_progress",
                    "The request is still in progress",
                    {},
                    409,
                )
            return dict(row["response_json"] or {})

        row = (
            connection.execute(select(documents_idempotency_table).where(predicate))
            .mappings()
            .one_or_none()
        )
        if row is not None:
            return resolve(dict(row))
        inserted = _insert_do_nothing(
            connection,
            documents_idempotency_table,
            {
                "actor_id": actor_id,
                "endpoint": endpoint,
                "target_id": target_id,
                "idempotency_key_hash": key_hash,
                "request_fingerprint": fingerprint,
                "status": "reserved",
                "response_json": None,
                "created_at_utc": self._current_time(),
                "completed_at_utc": None,
            },
            ["actor_id", "endpoint", "target_id", "idempotency_key_hash"],
        )
        if inserted:
            return None
        row = (
            connection.execute(select(documents_idempotency_table).where(predicate))
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise PlatformError(
                "idempotency_in_progress",
                "The request is still in progress",
                {},
                409,
            )
        return resolve(dict(row))

    def _complete_idempotency(
        self,
        connection: Connection,
        *,
        actor_id: str,
        endpoint: str,
        target_id: str,
        key: str,
        fingerprint: str,
        response: dict[str, Any],
    ) -> None:
        key_hash = self._hash(key.encode("utf-8"))
        updated = connection.execute(
            update(documents_idempotency_table)
            .where(
                and_(
                    documents_idempotency_table.c.actor_id == actor_id,
                    documents_idempotency_table.c.endpoint == endpoint,
                    documents_idempotency_table.c.target_id == target_id,
                    documents_idempotency_table.c.idempotency_key_hash == key_hash,
                    documents_idempotency_table.c.request_fingerprint == fingerprint,
                    documents_idempotency_table.c.status == "reserved",
                )
            )
            .values(
                status="completed",
                response_json=_json(response),
                completed_at_utc=self._current_time(),
            )
        ).rowcount
        if updated != 1:
            raise PlatformError(
                "idempotency_key_conflict",
                "The idempotency key conflicts with a previous request",
                {},
                409,
            )

    def _refresh_upload_batch(
        self, connection: Connection, batch_id: str | None, now: datetime
    ) -> None:
        if not batch_id:
            return
        states = [
            str(row[0])
            for row in connection.execute(
                select(upload_batch_items_table.c.result_state).where(
                    upload_batch_items_table.c.upload_batch_id == batch_id
                )
            ).all()
        ]
        if not states:
            state = "pending"
        elif any(item in {"running", "retry_wait"} for item in states):
            state = "running"
        elif any(item == "pending" for item in states):
            state = "pending"
        elif all(item in {"succeeded", "deduplicated"} for item in states):
            state = "succeeded"
        elif any(item in {"succeeded", "deduplicated"} for item in states):
            state = "partial"
        else:
            state = "failed"
        connection.execute(
            update(upload_batches_table)
            .where(upload_batches_table.c.id == batch_id)
            .values(state=state, updated_at_utc=now)
        )

    def _validate_upload_media(self, filename: str, media_kind: str) -> None:
        """Single-file upload contract: unsupported media -> 415, declared/content
        mismatch -> 422 (mirrors the frontend upload contract)."""
        declared = media_kind.strip()
        by_type = _UPLOAD_MEDIA_KINDS_BY_TYPE.get(declared.lower())
        extension = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        by_extension = _UPLOAD_MEDIA_KINDS_BY_EXTENSION.get(extension)
        if by_type is not None and by_extension is not None and by_type != by_extension:
            raise PlatformError(
                "upload_content_type_mismatch",
                "File content does not match the declared media type",
                {"file": filename},
                422,
            )
        if (
            declared
            and declared != "application/octet-stream"
            and by_type is None
            and by_extension is None
        ):
            raise PlatformError(
                "unsupported_media_type",
                "Media type is not supported",
                {"file": filename},
                415,
            )

    def _file_fingerprint(self, file: DocumentUpload) -> dict[str, Any]:
        validate_upload_security(
            media_kind=file.media_kind, content=file.content, scanner=self._malware_scanner
        )
        if len(file.content) > self._max_upload_bytes:
            raise PlatformError(
                "upload_too_large",
                "Upload exceeds the maximum size",
                {"max_bytes": self._max_upload_bytes},
                413,
            )
        normalized, normalized_name = self._normalize_filename(file.filename)
        return {
            "filename": normalized,
            "normalized_filename": normalized_name,
            "media_kind": file.media_kind,
            "size_bytes": len(file.content),
            "content_hash_sha256": self._hash(file.content),
            # Deterministic function of the payload: stable across idempotent
            # replays. Metadata only — a hit never rejects the upload (A2).
            "security_risk_fact": scan_prompt_injection_risk(
                media_kind=file.media_kind, content=file.content
            ),
        }

    @staticmethod
    def _dedup_claim_predicate(
        *, space_id: str, normalized_filename: str, content_hash: str
    ) -> Any:
        return and_(
            upload_dedup_claims_table.c.space_id == space_id,
            upload_dedup_claims_table.c.normalized_filename == normalized_filename,
            upload_dedup_claims_table.c.content_hash_sha256 == content_hash,
        )

    def _assert_replacement_claim_available(
        self,
        connection: Connection,
        *,
        document: Mapping[str, Any],
        info: Mapping[str, Any],
    ) -> None:
        claim = (
            connection.execute(
                select(upload_dedup_claims_table)
                .where(
                    self._dedup_claim_predicate(
                        space_id=str(document["space_id"]),
                        normalized_filename=str(info["normalized_filename"]),
                        content_hash=str(info["content_hash_sha256"]),
                    )
                )
                .with_for_update()
            )
            .mappings()
            .one_or_none()
        )
        if claim is None or str(claim["document_id"]) == str(document["id"]):
            return
        owner = connection.execute(
            select(documents_table.c.lifecycle_status).where(
                documents_table.c.id == claim["document_id"]
            )
        ).scalar_one_or_none()
        if owner not in {
            None,
            DocumentLifecycle.DELETED.value,
            DocumentLifecycle.PENDING_DELETE.value,
        }:
            raise PlatformError(
                "duplicate_document",
                "A document with the same name and content already exists",
                {"document_id": str(claim["document_id"])},
                409,
            )

    def _replace_dedup_claim(
        self,
        connection: Connection,
        *,
        document: Mapping[Any, Any],
        version: Mapping[Any, Any],
        now: datetime,
    ) -> None:
        space_id = str(document["space_id"])
        normalized_filename = self._normalize_filename(str(version["file_name"]))[1]
        content_hash = str(version["content_hash_sha256"])
        predicate = self._dedup_claim_predicate(
            space_id=space_id,
            normalized_filename=normalized_filename,
            content_hash=content_hash,
        )
        target_claim = (
            connection.execute(select(upload_dedup_claims_table).where(predicate).with_for_update())
            .mappings()
            .one_or_none()
        )

        if target_claim is not None and str(target_claim["document_id"]) != str(document["id"]):
            owner = connection.execute(
                select(documents_table.c.lifecycle_status).where(
                    documents_table.c.id == target_claim["document_id"]
                )
            ).scalar_one_or_none()
            if owner not in {
                None,
                DocumentLifecycle.DELETED.value,
                DocumentLifecycle.PENDING_DELETE.value,
            }:
                raise PlatformError(
                    "duplicate_document",
                    "A document with the same name and content already exists",
                    {
                        "document_id": str(target_claim["document_id"]),
                        "publication_claim_conflict": True,
                    },
                    409,
                )
            connection.execute(upload_dedup_claims_table.delete().where(predicate))
            target_claim = None

        if target_claim is None:
            claimed = _insert_do_nothing(
                connection,
                upload_dedup_claims_table,
                {
                    "space_id": space_id,
                    "normalized_filename": normalized_filename,
                    "content_hash_sha256": content_hash,
                    "document_id": str(document["id"]),
                    "document_version_id": str(version["id"]),
                    "created_at_utc": now,
                },
                index_elements=["space_id", "normalized_filename", "content_hash_sha256"],
            )
            if not claimed:
                winner = connection.execute(
                    select(upload_dedup_claims_table.c.document_id)
                    .where(predicate)
                    .with_for_update()
                ).scalar_one_or_none()
                raise PlatformError(
                    "duplicate_document",
                    "A document with the same name and content already exists",
                    ({"document_id": str(winner)} if winner is not None else {})
                    | {"publication_claim_conflict": True},
                    409,
                )
        else:
            connection.execute(
                upload_dedup_claims_table.update()
                .where(predicate)
                .values(document_version_id=str(version["id"]))
            )

        # A document owns exactly one effective claim.  Delete its old claim
        # only after the replacement claim has been secured.
        connection.execute(
            upload_dedup_claims_table.delete().where(
                and_(
                    upload_dedup_claims_table.c.document_id == str(document["id"]),
                    ~predicate,
                )
            )
        )

    @staticmethod
    def _security_degradations(info: Mapping[str, Any]) -> list[Mapping[str, Any]]:
        fact = info.get("security_risk_fact")
        return [fact] if fact is not None else []

    def _put_original(
        self, document_id: str, version_id: str, file: DocumentUpload
    ) -> tuple[str, str]:
        content_hash = self._hash(file.content)
        object_key = f"documents/{document_id}/{version_id}/original"
        self._object_store.put(
            object_key,
            file.content,
            ObjectMetadata(
                content_type=file.media_kind,
                size_bytes=len(file.content),
                checksum_sha256=content_hash,
            ),
        )
        return object_key, content_hash

    def _deduplicated_upload_item(
        self,
        connection: Connection,
        *,
        batch_id: str,
        now: datetime,
        info: Mapping[Any, Any],
        claim: Mapping[Any, Any],
    ) -> dict[str, Any]:
        existing = (
            connection.execute(
                select(
                    documents_table.c.id,
                    documents_table.c.active_version_id,
                    documents_table.c.pending_version_id,
                ).where(documents_table.c.id == claim["document_id"])
            )
            .mappings()
            .one_or_none()
        )
        version_id = (
            (existing["active_version_id"] or existing["pending_version_id"])
            if existing is not None
            else None
        )
        connection.execute(
            upload_batch_items_table.insert().values(
                id=_new_id("batch_item"),
                upload_batch_id=batch_id,
                document_id=claim["document_id"],
                submission_id=None,
                file_name=info["filename"],
                content_hash_sha256=info["content_hash_sha256"],
                result_state="deduplicated",
                deduplicated=True,
                job_id=None,
                rejection_reason=None,
                created_at_utc=now,
                updated_at_utc=now,
            )
        )
        return {
            "document_id": claim["document_id"],
            "document_version_id": version_id,
            "job_id": None,
            "publication_id": None,
            "filename": info["filename"],
            "deduplicated": True,
            "status": "deduplicated",
        }

    def create_initial_upload(
        self,
        *,
        principal: Any,
        space_id: str,
        files: Sequence[DocumentUpload],
        idempotency_key: str | None,
    ) -> dict[str, Any]:
        key = self._required_key(idempotency_key)
        if not files:
            raise PlatformError("validation_error", "At least one file is required", {}, 422)
        normalized_files = [self._file_fingerprint(file) for file in files]
        fingerprint = canonical_request_fingerprint(
            sorted(
                normalized_files,
                key=lambda item: (
                    item["normalized_filename"],
                    item["content_hash_sha256"],
                    item["media_kind"],
                ),
            )
        )
        actor_id = str(principal.user_id)
        endpoint = "documents.initial_upload"
        with self._engine.begin() as connection:
            # Same-transaction ACL (设计 §9.1.1): the grant and the department-row
            # lock live in this write transaction, so a concurrent deactivation
            # cannot interleave between the check and the new job.
            self._authorize(principal, space_id, "manage", connection=connection)
            replay = self._idempotency_replay(
                connection,
                actor_id=actor_id,
                endpoint=endpoint,
                target_id=space_id,
                key=key,
                fingerprint=fingerprint,
            )
            if replay is not None:
                return replay
            now = self._current_time()
            self._check_quota(connection, principal)
            batch_id = _new_id("batch")
            connection.execute(
                upload_batches_table.insert().values(
                    id=batch_id,
                    actor_user_id=actor_id,
                    space_id=space_id,
                    state="running",
                    created_at_utc=now,
                    updated_at_utc=now,
                )
            )
            items: list[dict[str, Any]] = []
            for file, info in zip(files, normalized_files, strict=True):
                claim = (
                    connection.execute(
                        select(upload_dedup_claims_table).where(
                            and_(
                                upload_dedup_claims_table.c.space_id == space_id,
                                upload_dedup_claims_table.c.normalized_filename
                                == info["normalized_filename"],
                                upload_dedup_claims_table.c.content_hash_sha256
                                == info["content_hash_sha256"],
                            )
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
                if claim is not None:
                    items.append(
                        self._deduplicated_upload_item(
                            connection,
                            batch_id=batch_id,
                            now=now,
                            info=info,
                            claim=claim,
                        )
                    )
                    continue

                document_id = _new_id("doc")
                version_id = _new_id("version")
                job_id = _new_id("job")
                publication_id = _new_id("publication")
                object_key, content_hash = self._put_original(document_id, version_id, file)
                connection.execute(
                    documents_table.insert().values(
                        id=document_id,
                        space_id=space_id,
                        lifecycle_status=DocumentLifecycle.ACTIVE.value,
                        active_version_id=None,
                        pending_version_id=version_id,
                        active_operation_job_id=job_id,
                        deletion_id=None,
                        version=1,
                        name=info["filename"],
                        normalized_name=info["normalized_filename"],
                        media_kind=file.media_kind,
                        uploaded_at_utc=now,
                        created_by_user_id=actor_id,
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
                        content_hash_sha256=content_hash,
                        object_manifest_json={
                            "object_key": object_key,
                            "size_bytes": len(file.content),
                        },
                        original_object_key=object_key,
                        file_name=info["filename"],
                        media_kind=file.media_kind,
                        size_bytes=len(file.content),
                        created_by_user_id=actor_id,
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
                        upload_batch_id=batch_id,
                        active_attempt_id=None,
                        active_publication_id=publication_id,
                        version=1,
                        replay_generation=0,
                        next_attempt_at_utc=None,
                        failure_reason=None,
                        degradations_json=_json(self._security_degradations(info)),
                        processing_summary_json={},
                        usage_json=None,
                        ocr_low_confidence=False,
                        notification_event_ids_json=[],
                        created_by_user_id=actor_id,
                        quota_role_snapshot=str(principal.role),
                        quota_department_id_snapshot=getattr(principal, "department_id", None),
                        quota_exempt_reason=None,
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
                        generation_id=self._current_index_generation(connection),
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
                        "space_id": space_id,
                        "normalized_filename": info["normalized_filename"],
                        "content_hash_sha256": content_hash,
                        "document_id": document_id,
                        "document_version_id": version_id,
                        "created_at_utc": now,
                    },
                    index_elements=["space_id", "normalized_filename", "content_hash_sha256"],
                )
                if not claimed:
                    self._object_store.delete(object_key)
                    connection.execute(
                        publications_table.delete().where(publications_table.c.id == publication_id)
                    )
                    connection.execute(
                        ingestion_jobs_table.delete().where(ingestion_jobs_table.c.id == job_id)
                    )
                    connection.execute(
                        document_versions_table.delete().where(
                            document_versions_table.c.id == version_id
                        )
                    )
                    connection.execute(
                        documents_table.delete().where(documents_table.c.id == document_id)
                    )
                    claim = (
                        connection.execute(
                            select(upload_dedup_claims_table).where(
                                and_(
                                    upload_dedup_claims_table.c.space_id == space_id,
                                    upload_dedup_claims_table.c.normalized_filename
                                    == info["normalized_filename"],
                                    upload_dedup_claims_table.c.content_hash_sha256 == content_hash,
                                )
                            )
                        )
                        .mappings()
                        .one()
                    )
                    items.append(
                        self._deduplicated_upload_item(
                            connection,
                            batch_id=batch_id,
                            now=now,
                            info=info,
                            claim=claim,
                        )
                    )
                    continue
                connection.execute(
                    upload_batch_items_table.insert().values(
                        id=_new_id("batch_item"),
                        upload_batch_id=batch_id,
                        document_id=document_id,
                        submission_id=None,
                        file_name=info["filename"],
                        content_hash_sha256=content_hash,
                        result_state="pending",
                        deduplicated=False,
                        job_id=job_id,
                        rejection_reason=None,
                        created_at_utc=now,
                        updated_at_utc=now,
                    )
                )
                items.append(
                    {
                        "document_id": document_id,
                        "document_version_id": version_id,
                        "job_id": job_id,
                        "publication_id": publication_id,
                        "filename": info["filename"],
                        "deduplicated": False,
                        "status": "pending",
                    }
                )
            response = {"upload_batch_id": batch_id, "items": items}
            self._refresh_upload_batch(connection, batch_id, now)
            self._complete_idempotency(
                connection,
                actor_id=actor_id,
                endpoint=endpoint,
                target_id=space_id,
                key=key,
                fingerprint=fingerprint,
                response=response,
            )
            return response

    def replace_version(
        self,
        *,
        principal: Any,
        document_id: str,
        expected_version: int,
        file: DocumentUpload,
        idempotency_key: str | None,
    ) -> dict[str, Any]:
        key = self._required_key(idempotency_key)
        if expected_version < 1:
            raise PlatformError("validation_error", "expected_version is invalid", {}, 422)
        self._validate_upload_media(file.filename, file.media_kind)
        info = self._file_fingerprint(file)
        with self._engine.begin() as connection:
            document = self._locked_document(connection, document_id)
            if document is None:
                raise PlatformError("document_not_found", "Document was not found", {}, 404)
            self._authorize(principal, str(document["space_id"]), "manage")
            fingerprint = canonical_request_fingerprint(
                {"expected_version": expected_version, "file": info}
            )
            actor_id = str(principal.user_id)
            endpoint = "documents.replace_version"
            replay = self._idempotency_replay(
                connection,
                actor_id=actor_id,
                endpoint=endpoint,
                target_id=document_id,
                key=key,
                fingerprint=fingerprint,
            )
            if replay is not None:
                return replay
            self._assert_document_writable(document, expected_version)
            self._assert_replacement_claim_available(
                connection,
                document=document,
                info=info,
            )
            active = None
            if document["active_version_id"]:
                active = (
                    connection.execute(
                        select(document_versions_table)
                        .where(document_versions_table.c.id == document["active_version_id"])
                        .with_for_update()
                    )
                    .mappings()
                    .one_or_none()
                )
            if active is not None and active["content_hash_sha256"] == info["content_hash_sha256"]:
                response = {
                    "document_id": document_id,
                    "document_version_id": active["id"],
                    "job_id": None,
                    "version": document["version"],
                    "deduplicated": True,
                    "status": "active",
                }
                self._complete_idempotency(
                    connection,
                    actor_id=actor_id,
                    endpoint=endpoint,
                    target_id=document_id,
                    key=key,
                    fingerprint=fingerprint,
                    response=response,
                )
                return response
            self._check_quota(connection, principal)
            now = self._current_time()
            version_id = _new_id("version")
            job_id = _new_id("job")
            publication_id = _new_id("publication")
            object_key, content_hash = self._put_original(document_id, version_id, file)
            version_number = (
                int(
                    connection.execute(
                        select(func.max(document_versions_table.c.version_number)).where(
                            document_versions_table.c.document_id == document_id
                        )
                    ).scalar_one()
                    or 0
                )
                + 1
            )
            next_document_version = int(document["version"]) + 1
            connection.execute(
                document_versions_table.insert().values(
                    id=version_id,
                    document_id=document_id,
                    version_number=version_number,
                    status=DocumentVersionState.PENDING.value,
                    content_hash_sha256=content_hash,
                    object_manifest_json={
                        "object_key": object_key,
                        "size_bytes": len(file.content),
                    },
                    original_object_key=object_key,
                    file_name=info["filename"],
                    media_kind=file.media_kind,
                    size_bytes=len(file.content),
                    created_by_user_id=str(principal.user_id),
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
                    operation="replace",
                    state=IngestionJobState.PENDING.value,
                    stage="queued",
                    base_active_version_id=document["active_version_id"],
                    upload_batch_id=None,
                    active_attempt_id=None,
                    active_publication_id=publication_id,
                    version=next_document_version,
                    replay_generation=0,
                    next_attempt_at_utc=None,
                    failure_reason=None,
                    degradations_json=_json(self._security_degradations(info)),
                    processing_summary_json={},
                    usage_json=None,
                    ocr_low_confidence=False,
                    notification_event_ids_json=[],
                    created_by_user_id=str(principal.user_id),
                    quota_role_snapshot=str(principal.role),
                    quota_department_id_snapshot=getattr(principal, "department_id", None),
                    quota_exempt_reason=None,
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
                    generation_id=self._current_index_generation(connection),
                    status=PublicationState.STAGED.value,
                    resource_manifest_json={},
                    created_at_utc=now,
                    activated_at_utc=None,
                    superseded_at_utc=None,
                    discarded_at_utc=None,
                )
            )
            connection.execute(
                update(documents_table)
                .where(documents_table.c.id == document_id)
                .values(
                    pending_version_id=version_id,
                    active_operation_job_id=job_id,
                    version=next_document_version,
                    updated_at_utc=now,
                )
            )
            response = {
                "document_id": document_id,
                "document_version_id": version_id,
                "job_id": job_id,
                "publication_id": publication_id,
                "version": next_document_version,
                "deduplicated": False,
                "status": "pending",
            }
            self._complete_idempotency(
                connection,
                actor_id=actor_id,
                endpoint=endpoint,
                target_id=document_id,
                key=key,
                fingerprint=fingerprint,
                response=response,
            )
            return response

    @staticmethod
    def _assert_document_writable(document: Mapping[str, Any], expected_version: int) -> None:
        lifecycle = str(document["lifecycle_status"])
        if lifecycle == DocumentLifecycle.PENDING_DELETE.value:
            raise PlatformError("document_pending_delete", "Document is pending deletion", {}, 409)
        if lifecycle == DocumentLifecycle.DELETED.value:
            raise PlatformError("document_deleted", "Document has been deleted", {}, 410)
        if int(document["version"]) != expected_version:
            raise PlatformError(
                "document_version_conflict",
                "The document version does not match",
                {"expected_version": int(document["version"])},
                409,
            )
        if document["active_operation_job_id"] is not None:
            raise PlatformError(
                "document_operation_in_progress", "A document operation is active", {}, 409
            )

    @staticmethod
    def _audit(
        connection: Connection,
        *,
        actor_id: str,
        resource_type: str,
        resource_id: str,
        result: str,
        occurred_at: datetime,
    ) -> None:
        context = current_context()
        connection.execute(
            platform_audit_table.insert().values(
                actor_id=actor_id,
                resource_type=resource_type,
                resource_id=resource_id,
                request_id=context.request_id if context is not None else "req_documents",
                occurred_at_utc=occurred_at,
                result=result,
                details_json={},
            )
        )

    def _audit_best_effort(
        self,
        *,
        actor_id: str,
        resource_type: str,
        resource_id: str,
        result: str,
    ) -> None:
        """Persist a failure/observation audit outside the rolled-back transaction.

        Failure facts must outlive the failing transaction's rollback (same
        pattern as identity's archive-restore alert); losing one is logged, never
        propagated to the caller.
        """

        try:
            with self._engine.begin() as connection:
                self._audit(
                    connection,
                    actor_id=actor_id,
                    resource_type=resource_type,
                    resource_id=resource_id,
                    result=result,
                    occurred_at=self._current_time(),
                )
        except Exception:  # noqa: BLE001 - audit must never mask the original failure
            logger.warning("documents audit write failed for %s/%s", resource_type, resource_id)

    def list_documents(
        self,
        *,
        principal: Any,
        space_id: str,
        q: str | None = None,
        page: int = 1,
        page_size: int = 50,
    ) -> dict[str, Any]:
        self._authorize(principal, space_id, "read")
        if page < 1 or page_size < 1 or page_size > 200:
            raise PlatformError("validation_error", "Pagination is invalid", {}, 422)
        with self._engine.connect() as connection:
            filters = [
                documents_table.c.space_id == space_id,
                documents_table.c.lifecycle_status == DocumentLifecycle.ACTIVE.value,
                documents_table.c.active_version_id.is_not(None),
                document_versions_table.c.status == DocumentVersionState.ACTIVE.value,
                publications_table.c.status == PublicationState.ACTIVE.value,
            ]
            if q:
                filters.append(documents_table.c.name.ilike(f"%{q.strip()}%"))
            base = documents_table.join(
                document_versions_table,
                document_versions_table.c.id == documents_table.c.active_version_id,
            ).join(
                publications_table,
                and_(
                    publications_table.c.document_id == documents_table.c.id,
                    publications_table.c.document_version_id == documents_table.c.active_version_id,
                ),
            )
            total = int(
                connection.execute(
                    select(func.count()).select_from(base).where(*filters)
                ).scalar_one()
            )
            rows = (
                connection.execute(
                    select(
                        documents_table,
                        document_versions_table.c.id.label("version_id"),
                        document_versions_table.c.status.label("version_status"),
                        ingestion_jobs_table.c.id.label("operation_job_id"),
                        ingestion_jobs_table.c.operation.label("operation"),
                        ingestion_jobs_table.c.state.label("operation_state"),
                        publications_table.c.resource_manifest_json.label("publication_manifest"),
                    )
                    .select_from(
                        base.outerjoin(
                            ingestion_jobs_table,
                            ingestion_jobs_table.c.id == documents_table.c.active_operation_job_id,
                        )
                    )
                    .where(*filters)
                    .order_by(documents_table.c.uploaded_at_utc.desc())
                    .offset((page - 1) * page_size)
                    .limit(page_size)
                )
                .mappings()
                .all()
            )
        items = []
        for row in rows:
            operation = None
            if row["operation_job_id"] is not None:
                operation = {
                    "job_id": row["operation_job_id"],
                    "operation": row["operation"],
                    "state": row["operation_state"],
                }
            processing_summary = (row["publication_manifest"] or {}).get("processing_summary") or {}
            pages = processing_summary.get("page_count", processing_summary.get("pages", 0))
            images = processing_summary.get("image_count", processing_summary.get("images", 0))
            if isinstance(pages, bool) or not isinstance(pages, int) or pages < 0:
                pages = 0
            if isinstance(images, bool) or not isinstance(images, int) or images < 0:
                images = 0
            metered_pages = _metered_pages(
                page_count=pages,
                image_count=images,
                summary=processing_summary,
            )
            items.append(
                {
                    "id": row["id"],
                    "document_version_id": row["version_id"],
                    "version": row["version"],
                    "name": row["name"],
                    "media_kind": row["media_kind"],
                    "version_status": row["version_status"],
                    "active_operation": operation,
                    "uploaded_at": _timestamp(row["uploaded_at_utc"]),
                    "usage": {"pages": metered_pages, "images": images},
                }
            )
        audit_resource_type = None
        if space_id.startswith("personal:") and space_id != f"personal:{principal.user_id}":
            # Directory-privileged view of another user's personal library: observable read.
            audit_resource_type = "documents.personal_library_view"
        elif space_id.startswith("department:") and str(getattr(principal, "role", "")) in {
            "ops",
            "admin",
        }:
            # Management drilldown of a department library: observable read.
            audit_resource_type = "documents.department_library_view"
        if audit_resource_type is not None:
            try:
                with self._engine.begin() as connection:
                    self._audit(
                        connection,
                        actor_id=str(principal.user_id),
                        resource_type=audit_resource_type,
                        resource_id=space_id,
                        result="succeeded",
                        occurred_at=self._current_time(),
                    )
            except Exception:
                # The drilldown is read-only; the audit write is best-effort and never blocks it.
                logger.warning(
                    "library view audit write failed for %s", audit_resource_type, exc_info=True
                )
        return {"items": items, "total": total, "page": page, "page_size": page_size}

    def list_versions(self, *, principal: Any, document_id: str) -> dict[str, Any]:
        from .read_models import DocumentReadModels

        return DocumentReadModels(self).list_versions(principal=principal, document_id=document_id)

    def preview(
        self,
        *,
        principal: Any,
        document_id: str,
        document_version_id: str | None = None,
        message_id: str | None = None,
    ) -> dict[str, Any]:
        from .read_models import DocumentReadModels

        return DocumentReadModels(self).preview(
            principal=principal,
            document_id=document_id,
            document_version_id=document_version_id,
            message_id=message_id,
        )

    def content(
        self,
        *,
        principal: Any,
        document_id: str,
        document_version_id: str | None = None,
        sheet: str | None = None,
    ) -> PreviewContent:
        from .read_models import DocumentReadModels

        return DocumentReadModels(self).content(
            principal=principal,
            document_id=document_id,
            document_version_id=document_version_id,
            sheet=sheet,
        )

    def content_head_supported(
        self,
        *,
        principal: Any,
        document_id: str,
        document_version_id: str | None = None,
    ) -> bool:
        from .read_models import DocumentReadModels

        return DocumentReadModels(self).content_head_supported(
            principal=principal,
            document_id=document_id,
            document_version_id=document_version_id,
        )

    def get_upload_batch(self, *, principal: Any, upload_batch_id: str) -> dict[str, Any]:
        from .read_models import DocumentReadModels

        return DocumentReadModels(self).get_upload_batch(
            principal=principal,
            upload_batch_id=upload_batch_id,
        )

    def restore_version(
        self,
        *,
        principal: Any,
        document_id: str,
        document_version_id: str,
        expected_version: int,
        idempotency_key: str | None,
    ) -> dict[str, Any]:
        try:
            return self._restore_version_locked(
                principal=principal,
                document_id=document_id,
                document_version_id=document_version_id,
                expected_version=expected_version,
                idempotency_key=idempotency_key,
            )
        except PlatformError as exc:
            # §9.3 审计事实：版本恢复失败（失败事实必须越过回滚落库）。
            if exc.code in {"document_version_not_restorable", "document_version_purged"}:
                self._audit_best_effort(
                    actor_id=str(principal.user_id),
                    resource_type="documents.version_restore",
                    resource_id=document_id,
                    result="failed",
                )
            raise

    def _restore_version_locked(
        self,
        *,
        principal: Any,
        document_id: str,
        document_version_id: str,
        expected_version: int,
        idempotency_key: str | None,
    ) -> dict[str, Any]:
        key = self._required_key(idempotency_key)
        actor_id = str(principal.user_id)
        endpoint = "documents.restore_version"
        fingerprint = canonical_request_fingerprint(
            {"document_version_id": document_version_id, "expected_version": expected_version}
        )
        with self._engine.begin() as connection:
            document = self._locked_document(connection, document_id)
            if document is None:
                raise PlatformError("document_not_found", "Document was not found", {}, 404)
            self._authorize(principal, str(document["space_id"]), "manage")
            replay = self._idempotency_replay(
                connection,
                actor_id=actor_id,
                endpoint=endpoint,
                target_id=document_id,
                key=key,
                fingerprint=fingerprint,
            )
            if replay is not None:
                return replay
            self._assert_document_writable(document, expected_version)
            self._check_quota(connection, principal)
            source = (
                connection.execute(
                    select(document_versions_table).where(
                        and_(
                            document_versions_table.c.id == document_version_id,
                            document_versions_table.c.document_id == document_id,
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
            if source is None:
                raise PlatformError(
                    "document_version_not_found", "Document version was not found", {}, 404
                )
            if source["status"] != DocumentVersionState.SUPERSEDED.value:
                raise PlatformError(
                    "document_version_not_restorable", "The version cannot be restored", {}, 409
                )
            restore_info = {
                "normalized_filename": self._normalize_filename(str(source["file_name"]))[1],
                "content_hash_sha256": str(source["content_hash_sha256"]),
            }
            self._assert_replacement_claim_available(
                connection,
                document=document,
                info=restore_info,
            )
            now = self._current_time()
            if (
                source["purge_after_at_utc"] is not None
                and _utc(source["purge_after_at_utc"]) <= now
            ):
                raise PlatformError(
                    "document_version_purged",
                    "Document version content was purged",
                    {
                        "document_id": document_id,
                        "document_version_id": document_version_id,
                        "purge_after_at_utc": _utc(source["purge_after_at_utc"]).isoformat(),
                    },
                    409,
                )
            version_id = _new_id("version")
            job_id = _new_id("job")
            publication_id = _new_id("publication")
            object_key = f"documents/{document_id}/{version_id}/original"
            try:
                self._object_store.copy(str(source["original_object_key"]), object_key)
            except (StorageKeyError, KeyError) as exc:
                raise PlatformError(
                    "document_version_purged",
                    "Document version content was purged",
                    {"document_id": document_id, "document_version_id": document_version_id},
                    409,
                ) from exc
            content_hash = str(source["content_hash_sha256"])
            restored_size = int(source["size_bytes"])
            version_number = (
                int(
                    connection.execute(
                        select(func.max(document_versions_table.c.version_number)).where(
                            document_versions_table.c.document_id == document_id
                        )
                    ).scalar_one()
                    or 0
                )
                + 1
            )
            next_version = int(document["version"]) + 1
            connection.execute(
                document_versions_table.insert().values(
                    id=version_id,
                    document_id=document_id,
                    version_number=version_number,
                    status=DocumentVersionState.PENDING.value,
                    content_hash_sha256=content_hash,
                    object_manifest_json={"object_key": object_key, "size_bytes": restored_size},
                    original_object_key=object_key,
                    file_name=source["file_name"],
                    media_kind=source["media_kind"],
                    size_bytes=restored_size,
                    created_by_user_id=actor_id,
                    activated_at_utc=None,
                    terminal_at_utc=None,
                    superseded_at_utc=None,
                    purge_after_at_utc=None,
                    purged_at_utc=None,
                    restored_from_version_id=document_version_id,
                    created_at_utc=now,
                    updated_at_utc=now,
                )
            )
            connection.execute(
                ingestion_jobs_table.insert().values(
                    id=job_id,
                    document_id=document_id,
                    document_version_id=version_id,
                    operation="replace",
                    state=IngestionJobState.PENDING.value,
                    stage="queued",
                    base_active_version_id=document["active_version_id"],
                    upload_batch_id=None,
                    active_attempt_id=None,
                    active_publication_id=publication_id,
                    version=next_version,
                    replay_generation=0,
                    next_attempt_at_utc=None,
                    failure_reason=None,
                    degradations_json=[],
                    processing_summary_json={},
                    usage_json=None,
                    ocr_low_confidence=False,
                    notification_event_ids_json=[],
                    created_by_user_id=actor_id,
                    quota_role_snapshot=str(principal.role),
                    quota_department_id_snapshot=getattr(principal, "department_id", None),
                    quota_exempt_reason=None,
                    created_at_utc=now,
                    updated_at_utc=now,
                )
            )
            connection.execute(
                document_version_restore_holds_table.insert().values(
                    id=_new_id("restore_hold"),
                    document_version_id=document_version_id,
                    job_id=job_id,
                    created_at_utc=now,
                )
            )
            connection.execute(
                publications_table.insert().values(
                    id=publication_id,
                    document_id=document_id,
                    document_version_id=version_id,
                    job_id=job_id,
                    attempt_id="pending",
                    generation_id=self._current_index_generation(connection),
                    status=PublicationState.STAGED.value,
                    resource_manifest_json={},
                    created_at_utc=now,
                    activated_at_utc=None,
                    superseded_at_utc=None,
                    discarded_at_utc=None,
                )
            )
            connection.execute(
                update(documents_table)
                .where(documents_table.c.id == document_id)
                .values(
                    pending_version_id=version_id,
                    active_operation_job_id=job_id,
                    version=next_version,
                    updated_at_utc=now,
                )
            )
            response = {
                "document_id": document_id,
                "document_version_id": version_id,
                "restored_from_version_id": document_version_id,
                "job_id": job_id,
                "publication_id": publication_id,
                "version": next_version,
                "status": "pending",
            }
            self._audit(
                connection,
                actor_id=actor_id,
                resource_type="documents.version_restore",
                resource_id=document_id,
                result="succeeded",
                occurred_at=now,
            )
            self._complete_idempotency(
                connection,
                actor_id=actor_id,
                endpoint=endpoint,
                target_id=document_id,
                key=key,
                fingerprint=fingerprint,
                response=response,
            )
            return response

    def accept_processing_receipt(
        self,
        *,
        principal: Any | None = None,
        job_id: str,
        receipt: IndexProcessingReceipt | Mapping[str, Any],
    ) -> dict[str, Any]:
        try:
            return self._accept_processing_receipt_transaction(
                principal=principal,
                job_id=job_id,
                receipt=receipt,
            )
        except PlatformError as exc:
            # §9.3 审计事实：执行中授权失效（部门停用/调动导致），越过回滚落库。
            if exc.code in {"authorization_changed", "submission_grant_invalid"}:
                self._audit_best_effort(
                    actor_id=(
                        str(principal.user_id)
                        if principal is not None
                        else "system:documents-worker"
                    ),
                    resource_type="documents.job_authorization",
                    resource_id=job_id,
                    result=exc.code,
                )
            if exc.code == "generation_conflict":
                # 07-4.9.21 代际冲突自动重暂存：废弃冲突 attempt 后按可重试失败
                # 记账（预算沿用 4 次上限），下一 attempt 在 claim 时取当前活动
                # 代际重走 stage→publish；不再向调用方止步于 409 拒绝。
                attempt_id = (
                    receipt.attempt_id
                    if isinstance(receipt, IndexProcessingReceipt)
                    else receipt.get("attempt_id") if isinstance(receipt, Mapping) else None
                )
                fencing_token = None
                if isinstance(attempt_id, str):
                    with self._engine.connect() as connection:
                        fencing_token = connection.execute(
                            select(ingestion_attempts_table.c.fencing_token).where(
                                ingestion_attempts_table.c.id == attempt_id
                            )
                        ).scalar_one_or_none()
                if isinstance(attempt_id, str) and fencing_token is not None:
                    self.fail_job(
                        job_id=job_id,
                        reason="generation_conflict",
                        retryable=True,
                        attempt_id=attempt_id,
                        fencing_token=int(fencing_token),
                    )
                    with self._engine.connect() as connection:
                        job_row = (
                            connection.execute(
                                select(ingestion_jobs_table).where(
                                    ingestion_jobs_table.c.id == job_id
                                )
                            )
                            .mappings()
                            .one_or_none()
                        )
                    if job_row is not None:
                        return self._job_response(job_row)
                raise
            details = dict(exc.details)
            if exc.code != "duplicate_document" or not details.get("publication_claim_conflict"):
                raise
            attempt_id = details.get("attempt_id")
            fencing_token = details.get("fencing_token")
            if isinstance(attempt_id, str) and fencing_token is not None:
                self.fail_job(
                    job_id=job_id,
                    reason="duplicate_document",
                    retryable=False,
                    attempt_id=attempt_id,
                    fencing_token=int(fencing_token),
                )
            raise PlatformError(
                "duplicate_document",
                "A document with the same name and content already exists",
                (
                    {"document_id": details["document_id"]}
                    if details.get("document_id") is not None
                    else {}
                ),
                409,
            ) from exc

    def _accept_processing_receipt_transaction(
        self,
        *,
        principal: Any | None = None,
        job_id: str,
        receipt: IndexProcessingReceipt | Mapping[str, Any],
    ) -> dict[str, Any]:
        with self._engine.begin() as connection:
            job_document_id = connection.execute(
                select(ingestion_jobs_table.c.document_id).where(
                    ingestion_jobs_table.c.id == job_id
                )
            ).scalar_one_or_none()
            if job_document_id is None:
                raise PlatformError("job_not_found", "Ingestion job was not found", {}, 404)
            document = self._locked_document(connection, str(job_document_id))
            if document is None:
                raise PlatformError("document_not_found", "Document was not found", {}, 404)
            job = (
                connection.execute(
                    select(ingestion_jobs_table)
                    .where(ingestion_jobs_table.c.id == job_id)
                    .with_for_update()
                )
                .mappings()
                .one_or_none()
            )
            if job is None:
                raise PlatformError("job_not_found", "Ingestion job was not found", {}, 404)
            if job["state"] == IngestionJobState.SUCCEEDED.value:
                return self._job_response(job)
            if job["state"] != IngestionJobState.RUNNING.value or job["active_attempt_id"] is None:
                raise PlatformError(
                    "fence_conflict",
                    "The processing job does not have a current running attempt",
                    {},
                    409,
                )
            active_attempt_id = str(job["active_attempt_id"])
            raw_receipt_attempt_id = (
                receipt.attempt_id
                if isinstance(receipt, IndexProcessingReceipt)
                else receipt.get("attempt_id") if isinstance(receipt, Mapping) else None
            )
            if (
                not isinstance(raw_receipt_attempt_id, str)
                or raw_receipt_attempt_id != active_attempt_id
            ):
                raise PlatformError(
                    "fence_conflict", "The processing attempt is no longer current", {}, 409
                )
            attempt = (
                connection.execute(
                    select(ingestion_attempts_table)
                    .where(ingestion_attempts_table.c.id == active_attempt_id)
                    .with_for_update()
                )
                .mappings()
                .one_or_none()
            )

            def reject_receipt(
                code: str,
                message: str,
                status_code: int = 409,
                retryable: bool = False,
            ) -> NoReturn:
                self._discard_indexing_attempt(connection, active_attempt_id)
                raise PlatformError(code, message, {}, status_code, retryable)

            if attempt is None or attempt["state"] != "running":
                reject_receipt("fence_conflict", "The processing attempt is no longer current")
            if (
                attempt["lease_expires_at_utc"] is None
                or _utc(attempt["lease_expires_at_utc"]) <= self._current_time()
            ):
                reject_receipt("fence_conflict", "The processing attempt lease has expired")
            try:
                if isinstance(receipt, IndexProcessingReceipt):
                    typed_receipt = receipt
                elif isinstance(receipt, Mapping):
                    typed_receipt = IndexProcessingReceipt.from_mapping(receipt)
                else:
                    raise PlatformError(
                        "validation_error", "Processing receipt is invalid", {}, 422
                    )
            except PlatformError as exc:
                reject_receipt(exc.code, exc.message, exc.status_code, exc.retryable)
            receipt = typed_receipt.to_mapping()
            receipt_attempt_id = receipt.get("attempt_id")
            receipt_fencing_token = receipt.get("fencing_token")
            if str(receipt_attempt_id) != active_attempt_id:
                raise PlatformError(
                    "fence_conflict", "The processing attempt is no longer current", {}, 409
                )
            if receipt_fencing_token is None:
                reject_receipt("fence_conflict", "The processing attempt is no longer current")
            if int(receipt_fencing_token) != int(attempt["fencing_token"]):
                reject_receipt("fence_conflict", "The processing attempt is no longer current")
            staging_request = attempt["staging_request_json"] or {}
            try:
                expected_authorization_fence = self._worker_authorization_fence(
                    connection, job=job, document=document
                )
            except PlatformError as exc:
                if exc.code == "submission_grant_invalid":
                    reject_receipt(exc.code, exc.message)
                raise
            if (
                dict(staging_request.get("authorization_fence") or {})
                != expected_authorization_fence
            ):
                reject_receipt(
                    "processing_receipt_conflict",
                    "The processing authorization fence is no longer current",
                )
            try:
                self._validate_direct_receipt_authorization(
                    principal=principal,
                    job=job,
                    document=document,
                )
            except PlatformError as exc:
                reject_receipt(exc.code, exc.message)
            expected_generation = staging_request.get("expected_generation_id")
            receipt_generation = receipt.get("generation_id")
            if not expected_generation or str(receipt_generation) != str(expected_generation):
                reject_receipt(
                    "generation_conflict",
                    "The processing receipt generation is no longer current",
                )
            expected_receipt_values = {
                "job_id": job["id"],
                "document_id": job["document_id"],
                "document_version_id": job["document_version_id"],
                "publication_id": job["active_publication_id"],
            }
            if any(
                str(receipt.get(key)) != str(expected)
                for key, expected in expected_receipt_values.items()
            ):
                reject_receipt(
                    "processing_receipt_conflict", "Processing receipt does not match the job"
                )
            publication = (
                connection.execute(
                    select(publications_table)
                    .where(publications_table.c.id == receipt["publication_id"])
                    .with_for_update()
                )
                .mappings()
                .one_or_none()
            )
            if document["lifecycle_status"] == DocumentLifecycle.PENDING_DELETE.value:
                reject_receipt("document_pending_delete", "Document is pending deletion")
            if document["lifecycle_status"] == DocumentLifecycle.DELETED.value:
                self._discard_indexing_attempt(connection, active_attempt_id)
                raise PlatformError("document_deleted", "Document has been deleted", {}, 410)
            if document["active_operation_job_id"] != job_id:
                reject_receipt(
                    "fence_conflict",
                    "The processing job is no longer the active document operation",
                )
            if job["document_version_id"]:
                version_row = (
                    connection.execute(
                        select(document_versions_table)
                        .where(document_versions_table.c.id == job["document_version_id"])
                        .with_for_update()
                    )
                    .mappings()
                    .one_or_none()
                )
                if version_row is None:
                    reject_receipt(
                        "processing_receipt_conflict", "The document version is no longer available"
                    )
                input_hash = receipt.get("input_content_hash")
                if input_hash is not None and str(input_hash) != str(
                    version_row["content_hash_sha256"]
                ):
                    reject_receipt(
                        "processing_receipt_conflict",
                        "Processing receipt input does not match the document version",
                    )
            if publication is None or publication["status"] != PublicationState.STAGED.value:
                reject_receipt("processing_receipt_conflict", "Publication is not staged")
            request = self._index_request_from_attempt(attempt)
            if request is None:
                reject_receipt(
                    "processing_receipt_conflict",
                    "The processing attempt has no staging request",
                )
            try:
                typed_receipt.validate_against(request)
            except PlatformError as exc:
                reject_receipt(exc.code, exc.message)
            if self._indexing_handoff_port is None:
                reject_receipt(
                    "indexing_handoff_unavailable",
                    "Index publication handoff is not configured",
                    503,
                    True,
                )
            publish_result = self._indexing_handoff_port.publish(
                request,
                connection=connection,
                receipt=typed_receipt,
            )
            publish_state = (
                publish_result.get("state")
                if isinstance(publish_result, Mapping)
                else getattr(publish_result, "state", None)
            )
            if publish_state != "published":
                reject_receipt(
                    "indexing_publish_failed",
                    "The indexing handoff did not confirm publication",
                )
            final_document = self._locked_document(connection, str(job["document_id"]))
            if final_document is None:
                reject_receipt("document_not_found", "Document was not found")
            final_job = (
                connection.execute(
                    select(ingestion_jobs_table)
                    .where(ingestion_jobs_table.c.id == job_id)
                    .with_for_update()
                )
                .mappings()
                .one_or_none()
            )
            if (
                final_job is None
                or final_job["state"] != IngestionJobState.RUNNING.value
                or str(final_job["active_attempt_id"]) != active_attempt_id
            ):
                reject_receipt("fence_conflict", "The processing job is no longer current")
            if final_document["lifecycle_status"] == DocumentLifecycle.PENDING_DELETE.value:
                reject_receipt("document_pending_delete", "Document is pending deletion")
            if final_document["lifecycle_status"] == DocumentLifecycle.DELETED.value:
                reject_receipt("document_deleted", "Document has been deleted", 410)
            if final_document["active_operation_job_id"] != job_id:
                reject_receipt(
                    "fence_conflict",
                    "The processing job is no longer the active document operation",
                )
            if final_document["active_version_id"] != final_job["base_active_version_id"]:
                reject_receipt(
                    "document_version_changed",
                    "The document active version has changed since this job was created",
                )
            try:
                self._worker_authorization_fence(connection, job=final_job, document=final_document)
                self._validate_direct_receipt_authorization(
                    principal=principal,
                    job=final_job,
                    document=final_document,
                )
            except PlatformError as exc:
                reject_receipt(exc.code, exc.message, exc.status_code, exc.retryable)
            document = final_document
            job = final_job
            now = self._current_time()
            attempt_id = str(receipt.get("attempt_id") or _new_id("attempt"))
            existing_attempt = connection.execute(
                select(ingestion_attempts_table.c.id).where(
                    ingestion_attempts_table.c.id == attempt_id
                )
            ).scalar_one_or_none()
            if existing_attempt is None:
                connection.execute(
                    ingestion_attempts_table.insert().values(
                        id=attempt_id,
                        job_id=job_id,
                        attempt_number=1,
                        cycle_attempt_number=1,
                        replay_generation=job["replay_generation"],
                        state="succeeded",
                        lease_owner=None,
                        lease_expires_at_utc=None,
                        fencing_token=1,
                        publication_id=publication["id"],
                        staging_request_json={},
                        processing_receipt_json=dict(receipt),
                        failure_class=None,
                        failure_reason=None,
                        created_at_utc=now,
                        updated_at_utc=now,
                    )
                )
            else:
                connection.execute(
                    update(ingestion_attempts_table)
                    .where(ingestion_attempts_table.c.id == attempt_id)
                    .values(
                        state="succeeded",
                        lease_expires_at_utc=None,
                        processing_receipt_json=_json(dict(receipt)),
                        updated_at_utc=now,
                    )
                )
            connection.execute(
                update(publications_table)
                .where(publications_table.c.id == publication["id"])
                .values(
                    status=PublicationState.ACTIVE.value,
                    attempt_id=attempt_id,
                    generation_id=str(receipt.get("generation_id") or publication["generation_id"]),
                    resource_manifest_json=_json(dict(receipt)),
                    activated_at_utc=now,
                )
            )
            connection.execute(
                update(publications_table)
                .where(
                    and_(
                        publications_table.c.document_id == job["document_id"],
                        publications_table.c.status == PublicationState.ACTIVE.value,
                        publications_table.c.id != publication["id"],
                    )
                )
                .values(status=PublicationState.SUPERSEDED.value, superseded_at_utc=now)
            )
            if job["operation"] in {"initial", "replace"}:
                version = (
                    connection.execute(
                        select(document_versions_table).where(
                            document_versions_table.c.id == job["document_version_id"]
                        )
                    )
                    .mappings()
                    .one()
                )
                try:
                    self._replace_dedup_claim(
                        connection,
                        document=document,
                        version=version,
                        now=now,
                    )
                except PlatformError as exc:
                    if exc.code != "duplicate_document":
                        raise
                    raise PlatformError(
                        exc.code,
                        exc.message,
                        {
                            **dict(exc.details),
                            "attempt_id": active_attempt_id,
                            "fencing_token": int(attempt["fencing_token"]),
                        },
                        exc.status_code,
                        exc.retryable,
                    ) from exc
                connection.execute(
                    update(document_versions_table)
                    .where(document_versions_table.c.id == version["id"])
                    .values(
                        status=DocumentVersionState.ACTIVE.value,
                        activated_at_utc=now,
                        updated_at_utc=now,
                    )
                )
                if document["active_version_id"] and document["active_version_id"] != version["id"]:
                    connection.execute(
                        update(document_versions_table)
                        .where(document_versions_table.c.id == document["active_version_id"])
                        .values(
                            status=DocumentVersionState.SUPERSEDED.value,
                            superseded_at_utc=now,
                            terminal_at_utc=now,
                            purge_after_at_utc=now + timedelta(days=self._version_retention_days),
                            updated_at_utc=now,
                        )
                    )
                connection.execute(
                    update(documents_table)
                    .where(documents_table.c.id == job["document_id"])
                    .values(
                        active_version_id=version["id"],
                        pending_version_id=None,
                        active_operation_job_id=None,
                        name=version["file_name"],
                        normalized_name=self._normalize_filename(str(version["file_name"]))[1],
                        media_kind=version["media_kind"],
                        updated_at_utc=now,
                    )
                )
            else:
                connection.execute(
                    update(documents_table)
                    .where(documents_table.c.id == job["document_id"])
                    .values(active_operation_job_id=None, updated_at_utc=now)
                )
            usage = self._record_publication_quota(
                connection,
                job=job,
                publication=publication,
                document=document,
                receipt=receipt,
                published_at=now,
            )
            if usage is None:
                quota_charge_status = "not_applicable"
                quota_charge_reason = "quota_service_unavailable"
            else:
                quota_charge_status = str(usage["quota_charge_status"])
                quota_charge_reason = usage["quota_charge_reason"]
            connection.execute(
                update(publications_table)
                .where(publications_table.c.id == publication["id"])
                .values(
                    quota_charge_status=quota_charge_status,
                    quota_charge_reason=quota_charge_reason,
                )
            )
            notification_event_ids = self._publish_ingestion_notifications(
                connection,
                job=job,
                publication=publication,
                receipt=receipt,
                occurred_at=now,
            )
            connection.execute(
                update(ingestion_jobs_table)
                .where(ingestion_jobs_table.c.id == job_id)
                .values(
                    state=IngestionJobState.SUCCEEDED.value,
                    stage=None,
                    active_attempt_id=attempt_id,
                    processing_summary_json=_json(dict(receipt)),
                    usage_json=usage,
                    quota_charge_status=quota_charge_status,
                    quota_charge_reason=quota_charge_reason,
                    # Upload-time security facts (A2) survive the receipt
                    # replacing the column: preserve them ahead of the
                    # pipeline's own degradations.
                    degradations_json=_json(
                        [
                            *(
                                item
                                for item in (job["degradations_json"] or [])
                                if isinstance(item, Mapping)
                                and item.get("kind") == INJECTION_RISK_KIND
                            ),
                            *receipt.get("degradations", []),
                        ]
                    ),
                    ocr_low_confidence=bool(receipt.get("ocr_low_confidence", False)),
                    notification_event_ids_json=notification_event_ids,
                    updated_at_utc=now,
                )
            )
            if job["upload_batch_id"]:
                connection.execute(
                    update(upload_batch_items_table)
                    .where(upload_batch_items_table.c.job_id == job_id)
                    .values(result_state="succeeded", updated_at_utc=now)
                )
                self._refresh_upload_batch(connection, job["upload_batch_id"], now)
            self._append_index_change(connection, job, publication, document["space_id"], now)
            return self._job_response(
                {
                    **dict(job),
                    "state": IngestionJobState.SUCCEEDED.value,
                    "active_attempt_id": attempt_id,
                    "active_publication_id": publication["id"],
                }
            )

    def delete_document(
        self,
        *,
        principal: Any,
        document_id: str,
        expected_version: int,
        idempotency_key: str | None,
        transaction_id: str | None = None,
        connection: Connection | None = None,
        system_actor: str | None = None,
    ) -> dict[str, Any]:
        key = self._required_key(idempotency_key)
        if expected_version < 1:
            raise PlatformError("validation_error", "expected_version is invalid", {}, 422)
        actor_id = system_actor or str(principal.user_id)
        endpoint = "documents.delete"
        with self._engine.begin() if connection is None else nullcontext(connection) as connection:
            document = self._locked_document(connection, document_id)
            if document is None:
                raise PlatformError("document_not_found", "Document was not found", {}, 404)
            fingerprint = canonical_request_fingerprint(
                {"expected_version": expected_version, "document_id": document_id}
            )
            # Internal system-actor calls (account deletion §9.2.1) skip the
            # request-idempotency ledger: determinism is guaranteed by the
            # caller's (user_deletion_id, document_id) key plus the document
            # lifecycle state machine, and the outer identity transaction is
            # the unit of recovery.
            replay = (
                None
                if system_actor is not None
                else self._idempotency_replay(
                    connection,
                    actor_id=actor_id,
                    endpoint=endpoint,
                    target_id=document_id,
                    key=key,
                    fingerprint=fingerprint,
                )
            )
            if replay is not None:
                return replay
            lifecycle = str(document["lifecycle_status"])
            if system_actor is not None and lifecycle in (
                DocumentLifecycle.PENDING_DELETE.value,
                DocumentLifecycle.DELETED.value,
            ):
                # Internal account-deletion call: the per-document workflow was
                # already delegated with the deterministic (user_deletion_id,
                # document_id) key, so reuse it instead of failing the retry.
                return {
                    "document_id": document_id,
                    "deletion_id": str(document["deletion_id"] or ""),
                    "state": lifecycle,
                    "version": int(document["version"]),
                    "lifecycle_status": lifecycle,
                    "deletion_requested_at": _timestamp(document["updated_at_utc"]),
                }
            if lifecycle == DocumentLifecycle.PENDING_DELETE.value:
                raise PlatformError(
                    "document_pending_delete", "Document is pending deletion", {}, 409
                )
            if lifecycle == DocumentLifecycle.DELETED.value:
                raise PlatformError("document_deleted", "Document has been deleted", {}, 410)
            if system_actor is None:
                self._authorize(principal, str(document["space_id"]), "manage")
            if int(document["version"]) != expected_version:
                raise PlatformError(
                    "document_version_conflict",
                    "The document version does not match",
                    {"expected_version": int(document["version"])},
                    409,
                )
            if document["space_id"] == "public":
                self._assert_public_source_manifest(
                    connection,
                    document_id=document_id,
                    document_version_id=document["active_version_id"],
                )
            if self._lifecycle_port is None:
                raise PlatformError(
                    "document_lifecycle_unavailable",
                    "Document notification redaction is not configured",
                    {},
                    503,
                    retryable=True,
                )
            version_ids = tuple(
                str(row[0])
                for row in connection.execute(
                    select(document_versions_table.c.id)
                    .where(document_versions_table.c.document_id == document_id)
                    .with_for_update()
                ).all()
            )
            deletion_id = _new_id("deletion")
            operation_id = f"{deletion_id}:document_notification_redaction"
            tx_id = transaction_id or _new_id("tx")
            command = DocumentNotificationRedactionCommand(
                operation_id=operation_id,
                caller_principal="documents",
                deletion_id=deletion_id,
                document_id=document_id,
                document_version_ids=version_ids,
                reason="document_pending_delete",
                transaction_id=tx_id,
                mode="inline",
                canonical_input_fingerprint="pending",
            )
            command = DocumentNotificationRedactionCommand(
                operation_id=command.operation_id,
                caller_principal=command.caller_principal,
                deletion_id=command.deletion_id,
                document_id=command.document_id,
                document_version_ids=command.document_version_ids,
                reason=command.reason,
                transaction_id=command.transaction_id,
                mode=command.mode,
                canonical_input_fingerprint=self._outbox_redaction_fingerprint(command),
            )
            receipt = self._lifecycle_port.redact_document_notifications(
                command,
                connection=connection,
            )
            if getattr(receipt, "state", None) != "completed":
                raise PlatformError(
                    "document_redaction_incomplete",
                    "Document notification redaction did not complete",
                    {},
                    503,
                    retryable=True,
                )
            now = self._current_time()
            new_document_version = int(document["version"]) + 1
            active_attempts = (
                connection.execute(
                    select(ingestion_attempts_table)
                    .join(
                        ingestion_jobs_table,
                        ingestion_jobs_table.c.id == ingestion_attempts_table.c.job_id,
                    )
                    .where(
                        and_(
                            ingestion_jobs_table.c.document_id == document_id,
                            ingestion_jobs_table.c.state.in_(
                                [
                                    IngestionJobState.PENDING.value,
                                    IngestionJobState.RUNNING.value,
                                    IngestionJobState.RETRY_WAIT.value,
                                ]
                            ),
                            ingestion_attempts_table.c.state == "running",
                        )
                    )
                    .with_for_update()
                )
                .mappings()
                .all()
            )
            for attempt in active_attempts:
                self._discard_indexing_attempt(connection, str(attempt["id"]))
                connection.execute(
                    update(ingestion_attempts_table)
                    .where(ingestion_attempts_table.c.id == attempt["id"])
                    .values(
                        state="cancelled",
                        lease_expires_at_utc=None,
                        updated_at_utc=now,
                    )
                )
            connection.execute(
                insert(document_deletions_table).values(
                    id=deletion_id,
                    document_id=document_id,
                    requested_by_user_id=actor_id,
                    version=new_document_version,
                    status="pending_delete",
                    requested_at_utc=now,
                    completed_at_utc=None,
                    notification_redaction_operation_id=operation_id,
                    notification_redaction_receipt_json={
                        "operation_id": receipt.operation_id,
                        "state": receipt.state,
                        "redacted_notification_count": receipt.redacted_notification_count,
                        "already_redacted_count": receipt.already_redacted_count,
                    },
                    physical_cleanup_json={},
                )
            )
            connection.execute(
                update(documents_table)
                .where(documents_table.c.id == document_id)
                .values(
                    lifecycle_status=DocumentLifecycle.PENDING_DELETE.value,
                    deletion_id=deletion_id,
                    active_version_id=None,
                    pending_version_id=None,
                    active_operation_job_id=None,
                    version=new_document_version,
                    updated_at_utc=now,
                )
            )
            cancelled_jobs = connection.execute(
                update(ingestion_jobs_table)
                .where(
                    and_(
                        ingestion_jobs_table.c.document_id == document_id,
                        ingestion_jobs_table.c.state.in_(
                            [
                                IngestionJobState.PENDING.value,
                                IngestionJobState.RUNNING.value,
                                IngestionJobState.RETRY_WAIT.value,
                            ]
                        ),
                    )
                )
                .values(state=IngestionJobState.CANCELLED.value, stage=None, updated_at_utc=now)
            )
            if active_attempts or (cancelled_jobs.rowcount or 0) > 0:
                # §9.3 审计事实：在途 job 取消（确有取消时记录）。
                self._audit(
                    connection,
                    actor_id=actor_id,
                    resource_type="documents.deletion",
                    resource_id=document_id,
                    result="jobs_cancelled",
                    occurred_at=now,
                )
            connection.execute(
                update(publications_table)
                .where(
                    and_(
                        publications_table.c.document_id == document_id,
                        publications_table.c.status == PublicationState.STAGED.value,
                    )
                )
                .values(status=PublicationState.DISCARDED.value, discarded_at_utc=now)
            )
            connection.execute(
                update(publications_table)
                .where(
                    and_(
                        publications_table.c.document_id == document_id,
                        publications_table.c.status == PublicationState.ACTIVE.value,
                    )
                )
                .values(status=PublicationState.SUPERSEDED.value, superseded_at_utc=now)
            )
            connection.execute(
                update(document_versions_table)
                .where(
                    and_(
                        document_versions_table.c.document_id == document_id,
                        document_versions_table.c.status == DocumentVersionState.ACTIVE.value,
                    )
                )
                .values(
                    status=DocumentVersionState.SUPERSEDED.value,
                    superseded_at_utc=now,
                    terminal_at_utc=now,
                    purge_after_at_utc=now + timedelta(days=self._version_retention_days),
                    updated_at_utc=now,
                )
            )
            connection.execute(
                update(document_versions_table)
                .where(
                    and_(
                        document_versions_table.c.document_id == document_id,
                        document_versions_table.c.status == DocumentVersionState.PENDING.value,
                    )
                )
                .values(
                    status=DocumentVersionState.CANCELLED.value,
                    terminal_at_utc=now,
                    purge_after_at_utc=now + timedelta(days=self._version_retention_days),
                    updated_at_utc=now,
                )
            )
            connection.execute(
                upload_dedup_claims_table.delete().where(
                    upload_dedup_claims_table.c.document_id == document_id
                )
            )
            self._append_delete_index_change(connection, document, now)
            response = {
                "document_id": document_id,
                "deletion_id": deletion_id,
                "state": DocumentLifecycle.PENDING_DELETE.value,
                "version": new_document_version,
                "lifecycle_status": DocumentLifecycle.PENDING_DELETE.value,
                "deletion_requested_at": _timestamp(now),
            }
            self._audit(
                connection,
                actor_id=actor_id,
                resource_type="documents.delete",
                resource_id=document_id,
                result="succeeded",
                occurred_at=now,
            )
            if system_actor is None:
                self._complete_idempotency(
                    connection,
                    actor_id=actor_id,
                    endpoint=endpoint,
                    target_id=document_id,
                    key=key,
                    fingerprint=fingerprint,
                    response=response,
                )
            return response

    delete = delete_document

    def delete_personal_documents_for_account(
        self,
        connection: Connection,
        *,
        user_id: str,
        user_deletion_id: str,
    ) -> int:
        """§9.2.1 account-deletion integration for personal-space documents.

        Creates or reuses one document permanent-deletion workflow per
        personal document of ``user_id`` with the deterministic idempotency
        key ``(user_deletion_id, document_id)`` and returns how many personal
        documents are not yet in the ``deleted`` tombstone state. Shared-space
        contributions are never touched here.
        """

        rows = (
            connection.execute(
                text("""
                SELECT d.id, d.version
                FROM documents d
                JOIN identity_space s ON s.id = d.space_id
                WHERE s.owner_user_id = :user_id
                  AND d.lifecycle_status != 'deleted'
                ORDER BY d.id
                """),
                {"user_id": user_id},
            )
            .mappings()
            .all()
        )
        for row in rows:
            self.delete_document(
                principal=_AccountDeletionPrincipal(user_id),
                document_id=str(row["id"]),
                expected_version=int(row["version"]),
                idempotency_key=f"user-deletion:{user_deletion_id}:{row['id']}"[:255],
                connection=connection,
                system_actor="system:account-deletion",
            )
        remaining = connection.execute(
            text("""
                SELECT COUNT(*) FROM documents d
                JOIN identity_space s ON s.id = d.space_id
                WHERE s.owner_user_id = :user_id
                  AND d.lifecycle_status != 'deleted'
                """),
            {"user_id": user_id},
        ).scalar_one()
        return int(remaining)

    def _append_delete_index_change(
        self, connection: Connection, document: Mapping[str, Any], now: datetime
    ) -> None:
        revision_id = _new_id("index_revision")
        connection.execute(
            index_revisions_table.insert().values(
                id=revision_id,
                document_id=document["id"],
                revision=self._next_index_revision(connection),
                generation_id="delete",
                created_at_utc=now,
            )
        )
        connection.execute(
            index_changes_table.insert().values(
                id=_new_id("index_change"),
                document_id=document["id"],
                document_version_id=document["active_version_id"],
                publication_id=None,
                revision_id=revision_id,
                change_type="delete",
                space_id=document["space_id"],
                created_at_utc=now,
            )
        )
        if document["space_id"] == "public":
            self._record_public_graph_source_change(
                connection,
                document_id=str(document["id"]),
                change_type="delete",
            )

    def finalize_deletion(self, *, document_id: str, deletion_id: str) -> dict[str, Any]:
        with self._engine.begin() as connection:
            document = self._locked_document(connection, document_id)
            if document is None:
                raise PlatformError("document_not_found", "Document was not found", {}, 404)
            if document["deletion_id"] != deletion_id:
                raise PlatformError(
                    "deletion_conflict", "Deletion does not match the document", {}, 409
                )
            if document["lifecycle_status"] == DocumentLifecycle.DELETED.value:
                return {"document_id": document_id, "state": "deleted"}
            if document["lifecycle_status"] != DocumentLifecycle.PENDING_DELETE.value:
                raise PlatformError(
                    "deletion_not_pending", "Document deletion is not pending", {}, 409
                )
            active_jobs = int(
                connection.execute(
                    select(func.count())
                    .select_from(ingestion_jobs_table)
                    .where(
                        and_(
                            ingestion_jobs_table.c.document_id == document_id,
                            ingestion_jobs_table.c.state.in_(
                                [
                                    IngestionJobState.PENDING.value,
                                    IngestionJobState.RUNNING.value,
                                    IngestionJobState.RETRY_WAIT.value,
                                ]
                            ),
                        )
                    )
                ).scalar_one()
            )
            if active_jobs:
                raise PlatformError(
                    "deletion_cleanup_blocked", "Document still has active work", {}, 409
                )
            now = self._current_time()
            if self._has_active_read_lease(connection, document_id=document_id, now=now):
                raise PlatformError(
                    "deletion_cleanup_blocked", "Document has active readers", {}, 409
                )
            versions = (
                connection.execute(
                    select(document_versions_table)
                    .where(document_versions_table.c.document_id == document_id)
                    .with_for_update()
                )
                .mappings()
                .all()
            )
            if any(
                self._has_active_restore_hold(connection, document_version_id=str(version["id"]))
                for version in versions
            ):
                raise PlatformError(
                    "deletion_cleanup_blocked", "Document has active restore work", {}, 409
                )
            deletion = (
                connection.execute(
                    select(document_deletions_table).where(
                        document_deletions_table.c.id == deletion_id
                    )
                )
                .mappings()
                .one_or_none()
            )
            if deletion is None:
                raise PlatformError(
                    "deletion_not_found", "Document deletion was not found", {}, 404
                )
            connection.execute(
                update(document_deletions_table)
                .where(document_deletions_table.c.id == deletion_id)
                .values(status="cleaning")
            )
            if str(deletion["status"]) == "pending_delete":
                # §9.3 审计事实：删除目标清单封存（仅首个 finalize pass 记录一次）。
                self._audit(
                    connection,
                    actor_id="system_purge_worker",
                    resource_type="documents.deletion",
                    resource_id=document_id,
                    result="cleanup_targets_sealed",
                    occurred_at=now,
                )
            all_targets_done = True
            for version in versions:
                if not self._stage_cleanup_targets(
                    connection,
                    target_table=document_deletion_cleanup_targets_table,
                    owner_field="deletion_id",
                    owner_id=deletion_id,
                    version=dict(version),
                    now=now,
                ):
                    all_targets_done = False
                    continue
                self._tombstone_version(connection, str(version["id"]), now)
            if all_targets_done:
                connection.execute(
                    update(documents_table)
                    .where(documents_table.c.id == document_id)
                    .values(
                        lifecycle_status=DocumentLifecycle.DELETED.value,
                        space_id=None,
                        active_version_id=None,
                        pending_version_id=None,
                        active_operation_job_id=None,
                        name=None,
                        normalized_name=None,
                        media_kind=None,
                        created_by_user_id=None,
                        updated_at_utc=now,
                    )
                )
                connection.execute(
                    update(document_deletions_table)
                    .where(document_deletions_table.c.id == deletion_id)
                    .values(status="completed", completed_at_utc=now)
                )
                for fact in ("cleanup_completed", "document_deleted"):
                    # §9.3 审计事实：清理完成、逻辑文档进入 deleted。
                    self._audit(
                        connection,
                        actor_id="system_purge_worker",
                        resource_type="documents.deletion",
                        resource_id=document_id,
                        result=fact,
                        occurred_at=now,
                    )
                return {"document_id": document_id, "state": "deleted"}
        newly_completed, had_failure = self._execute_cleanup_targets(
            document_deletion_cleanup_targets_table,
            "deletion_id",
            deletion_id,
            resource_context={"document_id": document_id},
            retry_audit_resource_type="documents.deletion",
        )
        if newly_completed and not had_failure:
            return self.finalize_deletion(document_id=document_id, deletion_id=deletion_id)
        return {"document_id": document_id, "state": "cleaning"}

    def purge_retained_versions(self, *, limit: int = 100) -> list[str]:
        if limit < 1 or limit > 1000:
            raise PlatformError("validation_error", "limit is invalid", {}, 422)
        with self._engine.begin() as connection:
            now = self._current_time()
            candidates = (
                connection.execute(
                    select(document_versions_table)
                    .where(
                        and_(
                            document_versions_table.c.status.in_(
                                [
                                    DocumentVersionState.SUPERSEDED.value,
                                    DocumentVersionState.FAILED.value,
                                    DocumentVersionState.CANCELLED.value,
                                    DocumentVersionState.PURGING.value,
                                ]
                            ),
                            document_versions_table.c.purge_after_at_utc.is_not(None),
                            document_versions_table.c.purge_after_at_utc <= now,
                        )
                    )
                    .order_by(
                        document_versions_table.c.purge_after_at_utc, document_versions_table.c.id
                    )
                    .limit(limit)
                    .with_for_update()
                )
                .mappings()
                .all()
            )
            purged: list[str] = []
            incomplete: list[tuple[str, str]] = []
            for version in candidates:
                document = self._locked_document(connection, str(version["document_id"]))
                if (
                    document is None
                    or document["lifecycle_status"] != DocumentLifecycle.ACTIVE.value
                ):
                    continue
                active_jobs = int(
                    connection.execute(
                        select(func.count())
                        .select_from(ingestion_jobs_table)
                        .where(
                            and_(
                                ingestion_jobs_table.c.document_version_id == version["id"],
                                ingestion_jobs_table.c.state.in_(
                                    [
                                        IngestionJobState.PENDING.value,
                                        IngestionJobState.RUNNING.value,
                                        IngestionJobState.RETRY_WAIT.value,
                                    ]
                                ),
                            )
                        )
                    ).scalar_one()
                )
                if (
                    active_jobs
                    or self._has_active_read_lease(
                        connection, document_version_id=str(version["id"]), now=now
                    )
                    or self._has_active_restore_hold(
                        connection, document_version_id=str(version["id"])
                    )
                ):
                    continue
                connection.execute(
                    update(document_versions_table)
                    .where(document_versions_table.c.id == version["id"])
                    .values(status=DocumentVersionState.PURGING.value, updated_at_utc=now)
                )
                self._audit(
                    connection,
                    actor_id="system_purge_worker",
                    resource_type="documents.version_purging",
                    resource_id=str(version["id"]),
                    result="succeeded",
                    occurred_at=now,
                )
                if self._stage_cleanup_targets(
                    connection,
                    target_table=document_version_cleanup_targets_table,
                    owner_field="document_version_id",
                    owner_id=str(version["id"]),
                    version=dict(version),
                    now=now,
                ):
                    self._tombstone_version(connection, str(version["id"]), now)
                    purged.append(str(version["id"]))
                else:
                    incomplete.append((str(version["id"]), str(version["document_id"])))
        for version_id, document_id in incomplete:
            newly_completed, had_failure = self._execute_cleanup_targets(
                document_version_cleanup_targets_table,
                "document_version_id",
                version_id,
                resource_context={
                    "document_id": document_id,
                    "document_version_id": version_id,
                },
                retry_audit_resource_type="documents.version_cleanup",
            )
            if not newly_completed or had_failure:
                continue
            with self._engine.begin() as connection:
                document = self._locked_document(connection, document_id)
                if (
                    document is None
                    or document["lifecycle_status"] != DocumentLifecycle.ACTIVE.value
                ):
                    continue
                version_row = (
                    connection.execute(
                        select(document_versions_table)
                        .where(document_versions_table.c.id == version_id)
                        .with_for_update()
                    )
                    .mappings()
                    .one_or_none()
                )
                if (
                    version_row is None
                    or version_row["status"] != DocumentVersionState.PURGING.value
                ):
                    continue
                if self._stage_cleanup_targets(
                    connection,
                    target_table=document_version_cleanup_targets_table,
                    owner_field="document_version_id",
                    owner_id=version_id,
                    version=dict(version_row),
                    now=self._current_time(),
                ):
                    self._tombstone_version(connection, version_id, self._current_time())
                    purged.append(version_id)
        return purged

    def reindex(
        self,
        *,
        principal: Any,
        document_id: str,
        expected_version: int,
        idempotency_key: str | None,
    ) -> dict[str, Any]:
        key = self._required_key(idempotency_key)
        if expected_version < 1:
            raise PlatformError("validation_error", "expected_version is invalid", {}, 422)
        actor_id = str(principal.user_id)
        endpoint = "documents.reindex"
        fingerprint = canonical_request_fingerprint({"expected_version": expected_version})
        with self._engine.begin() as connection:
            document = self._locked_document(connection, document_id)
            if document is None:
                raise PlatformError("document_not_found", "Document was not found", {}, 404)
            self._authorize(principal, str(document["space_id"]), "manage")
            replay = self._idempotency_replay(
                connection,
                actor_id=actor_id,
                endpoint=endpoint,
                target_id=document_id,
                key=key,
                fingerprint=fingerprint,
            )
            if replay is not None:
                return replay
            self._assert_document_writable(document, expected_version)
            self._check_quota(connection, principal)
            if document["active_version_id"] is None:
                raise PlatformError(
                    "document_not_published", "Document has no active version", {}, 409
                )
            now = self._current_time()
            job_id = _new_id("job")
            publication_id = _new_id("publication")
            next_version = int(document["version"]) + 1
            connection.execute(
                ingestion_jobs_table.insert().values(
                    id=job_id,
                    document_id=document_id,
                    document_version_id=document["active_version_id"],
                    operation="reindex",
                    state=IngestionJobState.PENDING.value,
                    stage="queued",
                    base_active_version_id=document["active_version_id"],
                    upload_batch_id=None,
                    active_attempt_id=None,
                    active_publication_id=publication_id,
                    version=next_version,
                    replay_generation=0,
                    next_attempt_at_utc=None,
                    failure_reason=None,
                    degradations_json=[],
                    processing_summary_json={},
                    usage_json=None,
                    ocr_low_confidence=False,
                    notification_event_ids_json=[],
                    created_by_user_id=actor_id,
                    quota_role_snapshot=str(principal.role),
                    quota_department_id_snapshot=getattr(principal, "department_id", None),
                    quota_exempt_reason=None,
                    created_at_utc=now,
                    updated_at_utc=now,
                )
            )
            connection.execute(
                publications_table.insert().values(
                    id=publication_id,
                    document_id=document_id,
                    document_version_id=document["active_version_id"],
                    job_id=job_id,
                    attempt_id="pending",
                    generation_id=self._current_index_generation(connection),
                    status=PublicationState.STAGED.value,
                    resource_manifest_json={},
                    created_at_utc=now,
                    activated_at_utc=None,
                    superseded_at_utc=None,
                    discarded_at_utc=None,
                )
            )
            connection.execute(
                update(documents_table)
                .where(documents_table.c.id == document_id)
                .values(active_operation_job_id=job_id, version=next_version, updated_at_utc=now)
            )
            response = {
                "document_id": document_id,
                "document_version_id": document["active_version_id"],
                "job_id": job_id,
                "publication_id": publication_id,
                "version": next_version,
                "status": "pending",
            }
            self._complete_idempotency(
                connection,
                actor_id=actor_id,
                endpoint=endpoint,
                target_id=document_id,
                key=key,
                fingerprint=fingerprint,
                response=response,
            )
            return response

    def cancel_job(self, *, principal: Any, job_id: str) -> dict[str, Any]:
        with self._engine.begin() as connection:
            job_document_id = connection.execute(
                select(ingestion_jobs_table.c.document_id).where(
                    ingestion_jobs_table.c.id == job_id
                )
            ).scalar_one_or_none()
            if job_document_id is None:
                raise PlatformError("job_not_found", "Ingestion job was not found", {}, 404)
            document = self._locked_document(connection, str(job_document_id))
            if document is None:
                raise PlatformError("document_not_found", "Document was not found", {}, 404)
            job = (
                connection.execute(
                    select(ingestion_jobs_table)
                    .where(ingestion_jobs_table.c.id == job_id)
                    .with_for_update()
                )
                .mappings()
                .one_or_none()
            )
            if job is None:
                raise PlatformError("job_not_found", "Ingestion job was not found", {}, 404)
            self._authorize(principal, str(document["space_id"]), "manage")
            state = str(job["state"])
            if state == IngestionJobState.CANCELLED.value:
                return self._job_response(job)
            if state not in {
                IngestionJobState.PENDING.value,
                IngestionJobState.RUNNING.value,
                IngestionJobState.RETRY_WAIT.value,
            }:
                raise PlatformError(
                    "job_not_cancellable", "The ingestion job cannot be cancelled", {}, 409
                )
            now = self._current_time()
            self._discard_indexing_attempt(connection, job["active_attempt_id"])
            connection.execute(
                update(ingestion_jobs_table)
                .where(
                    and_(
                        ingestion_jobs_table.c.id == job_id,
                        ingestion_jobs_table.c.state == state,
                    )
                )
                .values(
                    state=IngestionJobState.CANCELLED.value,
                    stage=None,
                    cancelled_by_user_id=str(principal.user_id),
                    updated_at_utc=now,
                )
            )
            if job["active_attempt_id"]:
                # 递增 fencing token：持有旧 token 的 worker 提交结果时会被
                # receipt 校验拒绝，无法继续推进已取消的状态机。
                next_fencing_token = (
                    int(
                        connection.execute(
                            select(func.max(ingestion_attempts_table.c.fencing_token)).where(
                                ingestion_attempts_table.c.job_id == job_id
                            )
                        ).scalar_one()
                        or 0
                    )
                    + 1
                )
                connection.execute(
                    update(ingestion_attempts_table)
                    .where(ingestion_attempts_table.c.id == job["active_attempt_id"])
                    .values(
                        state="cancelled",
                        fencing_token=next_fencing_token,
                        lease_expires_at_utc=None,
                        updated_at_utc=now,
                    )
                )
            connection.execute(
                update(publications_table)
                .where(
                    and_(
                        publications_table.c.job_id == job_id,
                        publications_table.c.status == PublicationState.STAGED.value,
                    )
                )
                .values(status=PublicationState.DISCARDED.value, discarded_at_utc=now)
            )
            if job["operation"] in {"initial", "replace"} and job["document_version_id"]:
                connection.execute(
                    update(document_versions_table)
                    .where(document_versions_table.c.id == job["document_version_id"])
                    .values(
                        status=DocumentVersionState.CANCELLED.value,
                        terminal_at_utc=now,
                        purge_after_at_utc=now + timedelta(days=self._version_retention_days),
                        updated_at_utc=now,
                    )
                )
                connection.execute(
                    update(documents_table)
                    .where(documents_table.c.id == job["document_id"])
                    .values(
                        pending_version_id=None,
                        active_operation_job_id=None,
                        updated_at_utc=now,
                    )
                )
                connection.execute(
                    upload_dedup_claims_table.delete().where(
                        and_(
                            upload_dedup_claims_table.c.document_id == job["document_id"],
                            upload_dedup_claims_table.c.document_version_id
                            == job["document_version_id"],
                        )
                    )
                )
            else:
                connection.execute(
                    update(documents_table)
                    .where(documents_table.c.id == job["document_id"])
                    .values(active_operation_job_id=None, updated_at_utc=now)
                )
            if job["upload_batch_id"]:
                connection.execute(
                    update(upload_batch_items_table)
                    .where(upload_batch_items_table.c.job_id == job_id)
                    .values(result_state="cancelled", updated_at_utc=now)
                )
                self._refresh_upload_batch(connection, job["upload_batch_id"], now)
            return {
                "job_id": job_id,
                "state": IngestionJobState.CANCELLED.value,
                "replay_generation": job["replay_generation"],
            }

    def fail_job(
        self,
        *,
        job_id: str,
        reason: str,
        retryable: bool = False,
        attempt_id: str | None = None,
        fencing_token: int | None = None,
    ) -> dict[str, Any]:
        with self._engine.begin() as connection:
            job_document_id = connection.execute(
                select(ingestion_jobs_table.c.document_id).where(
                    ingestion_jobs_table.c.id == job_id
                )
            ).scalar_one_or_none()
            if job_document_id is None:
                raise PlatformError("job_not_found", "Ingestion job was not found", {}, 404)
            document = self._locked_document(connection, str(job_document_id))
            if document is None:
                raise PlatformError("document_not_found", "Document was not found", {}, 404)
            job = (
                connection.execute(
                    select(ingestion_jobs_table)
                    .where(ingestion_jobs_table.c.id == job_id)
                    .with_for_update()
                )
                .mappings()
                .one_or_none()
            )
            if job is None:
                raise PlatformError("job_not_found", "Ingestion job was not found", {}, 404)
            if job["state"] in {
                IngestionJobState.SUCCEEDED.value,
                IngestionJobState.FAILED.value,
                IngestionJobState.CANCELLED.value,
                IngestionJobState.DEAD_LETTER.value,
            }:
                raise PlatformError(
                    "job_not_failable", "The ingestion job is already terminal", {}, 409
                )
            if job["state"] != IngestionJobState.RUNNING.value or job["active_attempt_id"] is None:
                raise PlatformError(
                    "fence_conflict", "The ingestion job has no current running attempt", {}, 409
                )
            active_attempt_id = str(job["active_attempt_id"])
            attempt = (
                connection.execute(
                    select(ingestion_attempts_table)
                    .where(ingestion_attempts_table.c.id == active_attempt_id)
                    .with_for_update()
                )
                .mappings()
                .one_or_none()
            )
            if (
                attempt is None
                or attempt["state"] != "running"
                or attempt_id != active_attempt_id
                or fencing_token is None
                or int(fencing_token) != int(attempt["fencing_token"])
            ):
                raise PlatformError(
                    "fence_conflict", "The processing attempt is no longer current", {}, 409
                )
            attempt_count = int(
                connection.execute(
                    select(func.count())
                    .select_from(ingestion_attempts_table)
                    .where(
                        and_(
                            ingestion_attempts_table.c.job_id == job_id,
                            ingestion_attempts_table.c.replay_generation
                            == job["replay_generation"],
                        )
                    )
                ).scalar_one()
            )
            will_retry = retryable and attempt_count < 4
            if will_retry:
                state = IngestionJobState.RETRY_WAIT.value
            elif retryable:
                state = IngestionJobState.DEAD_LETTER.value
            else:
                state = IngestionJobState.FAILED.value
            now = self._current_time()
            retry_delay = None
            if will_retry:
                base_minutes = (1, 5, 30)[attempt_count - 1]
                retry_delay = timedelta(
                    seconds=base_minutes * 60 * (80 + secrets.randbelow(41)) / 100
                )
            self._discard_indexing_attempt(connection, active_attempt_id)
            connection.execute(
                update(ingestion_attempts_table)
                .where(ingestion_attempts_table.c.id == active_attempt_id)
                .values(
                    state="failed",
                    lease_expires_at_utc=None,
                    failure_reason=reason,
                    updated_at_utc=now,
                )
            )
            connection.execute(
                update(ingestion_jobs_table)
                .where(
                    and_(
                        ingestion_jobs_table.c.id == job_id,
                        ingestion_jobs_table.c.state == IngestionJobState.RUNNING.value,
                        ingestion_jobs_table.c.active_attempt_id == active_attempt_id,
                    )
                )
                .values(
                    state=state,
                    stage=None,
                    active_attempt_id=None,
                    failure_reason=reason,
                    next_attempt_at_utc=now + retry_delay if retry_delay is not None else None,
                    updated_at_utc=now,
                )
            )
            connection.execute(
                update(publications_table)
                .where(
                    and_(
                        publications_table.c.job_id == job_id,
                        publications_table.c.status == PublicationState.STAGED.value,
                    )
                )
                .values(status=PublicationState.DISCARDED.value, discarded_at_utc=now)
            )
            if not will_retry:
                if job["operation"] in {"initial", "replace"} and job["document_version_id"]:
                    connection.execute(
                        update(document_versions_table)
                        .where(document_versions_table.c.id == job["document_version_id"])
                        .values(
                            status=DocumentVersionState.FAILED.value,
                            terminal_at_utc=now,
                            purge_after_at_utc=now + timedelta(days=self._version_retention_days),
                            updated_at_utc=now,
                        )
                    )
                    connection.execute(
                        update(documents_table)
                        .where(documents_table.c.id == job["document_id"])
                        .values(
                            pending_version_id=None,
                            active_operation_job_id=None,
                            updated_at_utc=now,
                        )
                    )
                    connection.execute(
                        upload_dedup_claims_table.delete().where(
                            and_(
                                upload_dedup_claims_table.c.document_id == job["document_id"],
                                upload_dedup_claims_table.c.document_version_id
                                == job["document_version_id"],
                            )
                        )
                    )
                else:
                    connection.execute(
                        update(documents_table)
                        .where(documents_table.c.id == job["document_id"])
                        .values(active_operation_job_id=None, updated_at_utc=now)
                    )
            if job["upload_batch_id"]:
                connection.execute(
                    update(upload_batch_items_table)
                    .where(upload_batch_items_table.c.job_id == job_id)
                    .values(result_state="retry_wait" if will_retry else state, updated_at_utc=now)
                )
                self._refresh_upload_batch(connection, job["upload_batch_id"], now)
            return {"job_id": job_id, "state": state, "failure_reason": reason}

    def replay_job(
        self,
        *,
        principal: Any,
        job_id: str,
        idempotency_key: str | None,
    ) -> dict[str, Any]:
        if getattr(principal, "role", None) != "ops":
            raise PlatformError("forbidden", "Ops access is required", {}, 403)
        key = self._required_key(idempotency_key)
        actor_id = str(principal.user_id)
        endpoint = "documents.job_replay"
        fingerprint = canonical_request_fingerprint({"job_id": job_id})
        with self._engine.begin() as connection:
            job_document_id = connection.execute(
                select(ingestion_jobs_table.c.document_id).where(
                    ingestion_jobs_table.c.id == job_id
                )
            ).scalar_one_or_none()
            if job_document_id is None:
                raise PlatformError("job_not_found", "Ingestion job was not found", {}, 404)
            document = self._locked_document(connection, str(job_document_id))
            if document is None:
                raise PlatformError("document_not_found", "Document was not found", {}, 404)
            job = (
                connection.execute(
                    select(ingestion_jobs_table)
                    .where(ingestion_jobs_table.c.id == job_id)
                    .with_for_update()
                )
                .mappings()
                .one_or_none()
            )
            if job is None:
                raise PlatformError("job_not_found", "Ingestion job was not found", {}, 404)
            replay = self._idempotency_replay(
                connection,
                actor_id=actor_id,
                endpoint=endpoint,
                target_id=job_id,
                key=key,
                fingerprint=fingerprint,
            )
            if replay is not None:
                return replay
            if job["state"] not in {
                IngestionJobState.FAILED.value,
                IngestionJobState.DEAD_LETTER.value,
                IngestionJobState.CANCELLED.value,
            }:
                raise PlatformError(
                    "job_not_replayable", "The ingestion job cannot be replayed", {}, 409
                )
            self._assert_replay_eligible(
                connection,
                job=job,
                document=document,
                principal=principal,
            )
            now = self._current_time()
            connection.execute(
                update(document_versions_table)
                .where(document_versions_table.c.id == job["document_version_id"])
                .values(
                    status=DocumentVersionState.PENDING.value,
                    terminal_at_utc=None,
                    purge_after_at_utc=None,
                    updated_at_utc=now,
                )
            )
            publication_id = _new_id("publication")
            replay_generation = int(job["replay_generation"]) + 1
            connection.execute(
                publications_table.insert().values(
                    id=publication_id,
                    document_id=job["document_id"],
                    document_version_id=job["document_version_id"],
                    job_id=job_id,
                    attempt_id="pending",
                    generation_id=self._current_index_generation(connection),
                    status=PublicationState.STAGED.value,
                    resource_manifest_json={},
                    created_at_utc=now,
                    activated_at_utc=None,
                    superseded_at_utc=None,
                    discarded_at_utc=None,
                )
            )
            connection.execute(
                update(ingestion_jobs_table)
                .where(
                    and_(
                        ingestion_jobs_table.c.id == job_id,
                        ingestion_jobs_table.c.state.in_(
                            [
                                IngestionJobState.FAILED.value,
                                IngestionJobState.DEAD_LETTER.value,
                                IngestionJobState.CANCELLED.value,
                            ]
                        ),
                        ingestion_jobs_table.c.replay_generation == job["replay_generation"],
                    )
                )
                .values(
                    state=IngestionJobState.PENDING.value,
                    stage="queued",
                    active_attempt_id=None,
                    active_publication_id=publication_id,
                    replay_generation=replay_generation,
                    failure_reason=None,
                    next_attempt_at_utc=None,
                    **(
                        {
                            "created_by_user_id": actor_id,
                            "quota_role_snapshot": str(getattr(principal, "role", "ops")),
                            "quota_department_id_snapshot": getattr(
                                principal, "department_id", None
                            ),
                        }
                        if job["quota_exempt_reason"] != "shared_library_submission"
                        else {}
                    ),
                    updated_at_utc=now,
                )
            )
            connection.execute(
                update(documents_table)
                .where(documents_table.c.id == job["document_id"])
                .values(active_operation_job_id=job_id, updated_at_utc=now)
            )
            if job["upload_batch_id"]:
                connection.execute(
                    update(upload_batch_items_table)
                    .where(upload_batch_items_table.c.job_id == job_id)
                    .values(result_state="pending", updated_at_utc=now)
                )
                self._refresh_upload_batch(connection, job["upload_batch_id"], now)
            response = {
                "job_id": job_id,
                "state": IngestionJobState.PENDING.value,
                "replay_generation": replay_generation,
                "publication_id": publication_id,
            }
            self._complete_idempotency(
                connection,
                actor_id=actor_id,
                endpoint=endpoint,
                target_id=job_id,
                key=key,
                fingerprint=fingerprint,
                response=response,
            )
            return response

    def list_jobs(
        self,
        *,
        principal: Any,
        limit: int = 50,
        space_id: str | None = None,
    ) -> dict[str, Any]:
        if limit < 1 or limit > 200:
            raise PlatformError("validation_error", "limit is invalid", {}, 422)
        with self._engine.connect() as connection:
            query = (
                select(
                    ingestion_jobs_table,
                    documents_table.c.space_id,
                    documents_table.c.name,
                    publications_table.c.status.label("current_publication_status"),
                )
                .join(documents_table, documents_table.c.id == ingestion_jobs_table.c.document_id)
                .outerjoin(
                    publications_table,
                    publications_table.c.id == ingestion_jobs_table.c.active_publication_id,
                )
            )
            if space_id is not None:
                self._authorize(principal, space_id, "read")
                query = query.where(documents_table.c.space_id == space_id)
            elif self._identity_access is None:
                pass
            rows = (
                connection.execute(
                    query.order_by(ingestion_jobs_table.c.created_at_utc.desc()).limit(limit + 1)
                )
                .mappings()
                .all()
            )
            visible = []
            for row in rows:
                can_manage = self._identity_access is None
                if space_id is None and self._identity_access is not None:
                    try:
                        self._authorize(principal, str(row["space_id"]), "read")
                    except PlatformError:
                        continue
                if self._identity_access is not None:
                    try:
                        self._authorize(principal, str(row["space_id"]), "manage")
                        can_manage = True
                    except PlatformError:
                        pass
                notification_event_ids = []
                if (
                    row["state"] == IngestionJobState.SUCCEEDED.value
                    and row["current_publication_status"] == PublicationState.ACTIVE.value
                    and str(row["created_by_user_id"]) == str(principal.user_id)
                ):
                    notification_event_ids = list(row["notification_event_ids_json"] or [])
                visible.append(
                    {
                        **self._job_response(row),
                        "name": row["name"],
                        "space_id": row["space_id"],
                        "stage": (
                            row["stage"]
                            if row["state"]
                            in {IngestionJobState.PENDING.value, IngestionJobState.RUNNING.value}
                            else None
                        ),
                        "next_attempt_at": (
                            _timestamp(row["next_attempt_at_utc"])
                            if row["next_attempt_at_utc"]
                            else None
                        ),
                        "failure_reason": (
                            row["failure_reason"]
                            if row["state"]
                            in {IngestionJobState.FAILED.value, IngestionJobState.DEAD_LETTER.value}
                            else None
                        ),
                        "degradations": [
                            {"kind": str(item.get("kind"))}
                            for item in row["degradations_json"]
                            if isinstance(item, Mapping)
                        ],
                        "ocr_low_confidence": row["ocr_low_confidence"],
                        "publication_id": row["active_publication_id"],
                        "processing_summary": row["processing_summary_json"],
                        "usage": row["usage_json"] if row["state"] == "succeeded" else None,
                        "allowed_actions": self._allowed_job_actions(
                            connection, row, principal, can_manage=can_manage
                        ),
                        "notification_event_ids": notification_event_ids,
                        "created_at": _timestamp(row["created_at_utc"]),
                    }
                )
        return {
            "items": visible[:limit],
            "limit": limit,
            "max_limit": 200,
            "has_more": len(visible) > limit,
        }

    def claim_job(
        self,
        *,
        worker_id: str,
        job_id: str | None = None,
        lease_ttl: timedelta = timedelta(minutes=5),
    ):
        from .jobs import DocumentsJobCoordinator

        return DocumentsJobCoordinator(self, lease_ttl=lease_ttl).claim(
            worker_id=worker_id,
            job_id=job_id,
        )

    def create_submission(
        self,
        *,
        principal: Any,
        space_id: str,
        file: DocumentUpload,
        idempotency_key: str | None,
        idempotency_item_index: int | None = None,
    ) -> dict[str, Any]:
        from .submissions import SubmissionService

        return SubmissionService(self).create(
            principal=principal,
            space_id=space_id,
            file=file,
            idempotency_key=idempotency_key,
            idempotency_item_index=idempotency_item_index,
        )

    def list_submissions(self, *, principal: Any, status: str | None = None) -> dict[str, Any]:
        from .submissions import SubmissionService

        return SubmissionService(self).list(principal=principal, status=status)

    def list_approval_submissions(
        self, *, principal: Any, target_kind: str | None = None, target_space_id: str | None = None
    ) -> dict[str, Any]:
        from .submissions import SubmissionService

        return SubmissionService(self).list_approvals(
            principal=principal,
            target_kind=target_kind,
            target_space_id=target_space_id,
        )

    def submission_content(
        self, *, principal: Any, submission_id: str
    ) -> tuple[bytes, ObjectMetadata, str]:
        from .submissions import SubmissionService

        return SubmissionService(self).content(principal=principal, submission_id=submission_id)

    def approve_submission(
        self,
        *,
        principal: Any,
        submission_id: str,
        expected_version: int,
        idempotency_key: str | None,
    ) -> dict[str, Any]:
        from .submissions import SubmissionService

        return SubmissionService(self).approve(
            principal=principal,
            submission_id=submission_id,
            expected_version=expected_version,
            idempotency_key=idempotency_key,
        )

    def reject_submission(
        self,
        *,
        principal: Any,
        submission_id: str,
        expected_version: int,
        idempotency_key: str | None,
        reason: str | None = None,
    ) -> dict[str, Any]:
        from .submissions import SubmissionService

        return SubmissionService(self).reject(
            principal=principal,
            submission_id=submission_id,
            expected_version=expected_version,
            idempotency_key=idempotency_key,
            reason=reason,
        )

    def withdraw_submission(
        self,
        *,
        principal: Any,
        submission_id: str,
        expected_version: int,
        idempotency_key: str | None,
    ) -> dict[str, Any]:
        from .submissions import SubmissionService

        return SubmissionService(self).withdraw(
            principal=principal,
            submission_id=submission_id,
            expected_version=expected_version,
            idempotency_key=idempotency_key,
        )

    def delete_submission(
        self,
        *,
        principal: Any,
        submission_id: str,
        expected_version: int,
        idempotency_key: str | None,
    ) -> None:
        from .submissions import SubmissionService

        return SubmissionService(self).delete(
            principal=principal,
            submission_id=submission_id,
            expected_version=expected_version,
            idempotency_key=idempotency_key,
        )

    def cleanup_scheduled_submissions(self, *, limit: int = 100) -> list[str]:
        from .submissions import SubmissionService

        return list(SubmissionService(self).cleanup_scheduled(limit=limit))

    def _allowed_job_actions(
        self,
        connection: Connection,
        job: Mapping[Any, Any],
        principal: Any,
        *,
        can_manage: bool,
    ) -> list[str]:
        if job["state"] in {"pending", "running", "retry_wait"}:
            return ["cancel"] if can_manage else []
        if (
            job["state"] in {"failed", "cancelled", "dead_letter"}
            and can_manage
            and getattr(principal, "role", None) == "ops"
            and self._can_replay_job(connection, job=job, principal=principal)
        ):
            return ["replay"]
        return []

    def _append_index_change(
        self,
        connection: Connection,
        job: Mapping[Any, Any],
        publication: Mapping[Any, Any],
        space_id: str,
        now: datetime,
    ) -> None:
        revision_id = _new_id("index_revision")
        connection.execute(
            index_revisions_table.insert().values(
                id=revision_id,
                document_id=job["document_id"],
                revision=self._next_index_revision(connection),
                generation_id=publication["generation_id"],
                created_at_utc=now,
            )
        )
        connection.execute(
            index_changes_table.insert().values(
                id=_new_id("index_change"),
                document_id=job["document_id"],
                document_version_id=job["document_version_id"],
                publication_id=publication["id"],
                revision_id=revision_id,
                change_type=str(job["operation"] if job["operation"] != "initial" else "publish"),
                space_id=space_id,
                created_at_utc=now,
            )
        )

        if space_id == "public":
            self._record_public_graph_source_change(
                connection,
                document_id=str(job["document_id"]),
                change_type=str(job["operation"] if job["operation"] != "initial" else "publish"),
            )

    def _record_public_graph_source_change(
        self,
        connection: Connection,
        *,
        document_id: str,
        change_type: str,
    ) -> None:
        source_service = self._public_graph_source_service
        if source_service is None:
            raise PlatformError(
                "public_graph_source_unavailable",
                "Public graph source service is not configured",
                {"retryable": True},
                503,
            )
        source_service.record_source_change(
            connection=connection,
            space_id="public",
            document_id=document_id,
            change_type=change_type,
            publications=self._current_public_source_publications(connection),
        )

    @staticmethod
    def _assert_public_source_manifest(
        connection: Connection,
        *,
        document_id: str,
        document_version_id: str | None,
    ) -> None:
        if document_version_id is None:
            return
        publication = (
            connection.execute(
                select(publications_table.c.resource_manifest_json).where(
                    and_(
                        publications_table.c.document_id == document_id,
                        publications_table.c.document_version_id == document_version_id,
                        publications_table.c.status == PublicationState.ACTIVE.value,
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        if publication is None:
            return
        manifest = dict(publication["resource_manifest_json"] or {})
        manifest_id = str(manifest.get("content_manifest_id") or "").strip()
        manifest_hash = str(manifest.get("content_manifest_hash") or "").strip()
        if not manifest_id or not manifest_hash:
            raise PlatformError(
                "public_source_manifest_invalid",
                "Public document source manifest is incomplete",
                {"document_id": document_id},
                409,
            )

    @staticmethod
    def _current_index_revision(connection: Connection) -> int:
        counter = connection.execute(
            select(documents_instance_counters_table.c.value).where(
                documents_instance_counters_table.c.counter_name == "index_revision"
            )
        ).scalar_one_or_none()
        return int(counter or 0)

    @staticmethod
    def _next_index_revision(connection: Connection) -> int:
        _insert_do_nothing(
            connection,
            documents_instance_counters_table,
            {"counter_name": "index_revision", "value": 0},
            index_elements=["counter_name"],
        )
        counter = (
            connection.execute(
                select(documents_instance_counters_table)
                .where(documents_instance_counters_table.c.counter_name == "index_revision")
                .with_for_update()
            )
            .mappings()
            .one()
        )
        current = int(counter["value"])
        next_revision = current + 1
        result = connection.execute(
            update(documents_instance_counters_table)
            .where(
                and_(
                    documents_instance_counters_table.c.counter_name == "index_revision",
                    documents_instance_counters_table.c.value == current,
                )
            )
            .values(value=next_revision)
        )
        if result.rowcount != 1:
            raise RuntimeError("Could not allocate the next index revision")
        return next_revision

    @staticmethod
    def _current_public_source_publications(connection: Connection) -> list[dict[str, str]]:
        rows = (
            connection.execute(
                select(
                    documents_table.c.id.label("document_id"),
                    document_versions_table.c.id.label("document_version_id"),
                    publications_table.c.id.label("publication_id"),
                    publications_table.c.resource_manifest_json,
                )
                .select_from(
                    documents_table.join(
                        document_versions_table,
                        document_versions_table.c.id == documents_table.c.active_version_id,
                    ).join(
                        publications_table,
                        and_(
                            publications_table.c.document_id == documents_table.c.id,
                            publications_table.c.document_version_id
                            == document_versions_table.c.id,
                        ),
                    )
                )
                .where(
                    and_(
                        documents_table.c.space_id == "public",
                        documents_table.c.lifecycle_status == DocumentLifecycle.ACTIVE.value,
                        document_versions_table.c.status == DocumentVersionState.ACTIVE.value,
                        publications_table.c.status == PublicationState.ACTIVE.value,
                    )
                )
                .order_by(documents_table.c.id, publications_table.c.id)
            )
            .mappings()
            .all()
        )
        publications: list[dict[str, str]] = []
        for row in rows:
            manifest = dict(row["resource_manifest_json"] or {})
            manifest_id = str(manifest.get("content_manifest_id") or "").strip()
            manifest_hash = str(manifest.get("content_manifest_hash") or "").strip()
            if not manifest_id or not manifest_hash:
                # Isolate historical bad rows: they drop out of the snapshot instead of
                # blocking every public publish/delete for the whole space.
                continue
            publications.append(
                {
                    "document_id": str(row["document_id"]),
                    "document_version_id": str(row["document_version_id"]),
                    "publication_id": str(row["publication_id"]),
                    "content_manifest_id": manifest_id,
                    "content_manifest_hash": manifest_hash,
                }
            )
        return publications

    @staticmethod
    def _job_response(job: Mapping[Any, Any]) -> dict[str, Any]:
        return {
            "job_id": job["id"],
            "document_id": job["document_id"],
            "document_version_id": job["document_version_id"],
            "operation": job["operation"],
            "base_active_version_id": job["base_active_version_id"],
            "upload_batch_id": job["upload_batch_id"],
            "state": job["state"],
            "replay_generation": job["replay_generation"],
        }


class _AccountDeletionPrincipal:
    """Minimal principal shim for internal system-actor document deletion."""

    user_id: str

    def __init__(self, user_id: str) -> None:
        self.user_id = user_id
        self.role = "admin"
        self.department_id = None


class DocumentsPersonalDocumentDeletion:
    """PersonalDocumentDeletionPort adapter owned by the documents domain."""

    def __init__(self, documents_service: DocumentsService) -> None:
        self._documents_service = documents_service

    def pending_personal_documents(
        self,
        connection: Connection,
        *,
        user_id: str,
        user_deletion_id: str,
    ) -> int:
        return self._documents_service.delete_personal_documents_for_account(
            connection,
            user_id=user_id,
            user_deletion_id=user_deletion_id,
        )
