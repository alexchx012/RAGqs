from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import insert

from app.documents.read_models import DocumentsRetrievalVisibilityPort
from app.documents.schema import document_versions_table, documents_table, publications_table
from app.indexing import IndexChunk
from app.outbox.ports import DocumentNotificationRedactionReceipt
from app.platform.errors import PlatformError

from .test_commands import _accept, _upload


class _Lifecycle:
    def redact_document_notifications(self, command, *, connection):
        del connection
        return DocumentNotificationRedactionReceipt(
            operation_id=command.operation_id,
            deletion_id=command.deletion_id,
            state="completed",
            redacted_notification_count=0,
            already_redacted_count=0,
        )


def _accepted(service, principal):
    created = service.create_initial_upload(
        principal=principal,
        space_id="space_1",
        files=[_upload()],
        idempotency_key="upload-1",
    )
    item = created["items"][0]
    _accept(service, principal, item)
    return item


def test_versions_and_content_only_expose_active_projection(service, principal) -> None:
    item = _accepted(service, principal)
    versions = service.list_versions(principal=principal, document_id=item["document_id"])
    assert versions["active_version_id"] == item["document_version_id"]
    assert versions["items"][0]["content_available"] is True
    body, metadata = service.content(principal=principal, document_id=item["document_id"])
    assert body == b"hello"
    assert metadata.size_bytes == 5


def test_preview_and_content_expose_readable_superseded_version(service, principal) -> None:
    first = _accepted(service, principal)
    replacement = service.replace_version(
        principal=principal,
        document_id=first["document_id"],
        expected_version=1,
        file=_upload(content=b"new content"),
        idempotency_key="replace-1",
    )
    _accept(service, principal, replacement)

    preview = service.preview(
        principal=principal,
        document_id=first["document_id"],
        document_version_id=first["document_version_id"],
    )
    content, _ = service.content(
        principal=principal,
        document_id=first["document_id"],
        document_version_id=first["document_version_id"],
    )

    assert preview["document_version_id"] == first["document_version_id"]
    assert content == b"hello"


def test_pending_document_never_leaks_preview_or_content(service, principal) -> None:
    created = service.create_initial_upload(
        principal=principal,
        space_id="space_1",
        files=[_upload()],
        idempotency_key="upload-1",
    )
    item = created["items"][0]
    with pytest.raises(PlatformError) as error:
        service.preview(principal=principal, document_id=item["document_id"])
    assert error.value.code == "document_unavailable"


def test_upload_batch_summary_counts_items(service, principal) -> None:
    created = service.create_initial_upload(
        principal=principal,
        space_id="space_1",
        files=[_upload(), _upload(name="second.txt")],
        idempotency_key="upload-1",
    )
    batch = service.get_upload_batch(
        principal=principal,
        upload_batch_id=created["upload_batch_id"],
    )
    assert batch["summary"]["total_files"] == 2
    assert batch["summary"]["pending"] == 2


def test_active_read_lease_blocks_physical_document_deletion(service, principal) -> None:
    item = _accepted(service, principal)
    service._lifecycle_port = _Lifecycle()

    assert service.content(principal=principal, document_id=item["document_id"])[0] == b"hello"
    deletion = service.delete_document(
        principal=principal,
        document_id=item["document_id"],
        expected_version=1,
        idempotency_key="delete-after-read",
        capability_token="token",
    )

    with pytest.raises(PlatformError) as error:
        service.finalize_deletion(
            document_id=item["document_id"], deletion_id=deletion["deletion_id"]
        )
    assert error.value.code == "deletion_cleanup_blocked"


def test_visibility_port_withholds_missing_current_content(service) -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    with service._engine.begin() as connection:
        connection.execute(
            insert(documents_table).values(
                id="document_1",
                space_id="space_1",
                lifecycle_status="active",
                active_version_id="version_1",
                pending_version_id=None,
                active_operation_job_id=None,
                deletion_id=None,
                version=1,
                name="guide.txt",
                normalized_name="guide.txt",
                media_kind="text/plain",
                uploaded_at_utc=now,
                created_by_user_id="user_1",
                created_at_utc=now,
                updated_at_utc=now,
            )
        )
        connection.execute(
            insert(document_versions_table).values(
                id="version_1",
                document_id="document_1",
                version_number=1,
                status="active",
                content_hash_sha256="content_1",
                object_manifest_json={},
                original_object_key="missing-object",
                file_name="guide.txt",
                media_kind="text/plain",
                size_bytes=5,
                created_by_user_id="user_1",
                activated_at_utc=now,
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
            insert(publications_table).values(
                id="publication_1",
                document_id="document_1",
                document_version_id="version_1",
                job_id="job_1",
                attempt_id="attempt_1",
                generation_id="generation_initial",
                status="active",
                resource_manifest_json={"content_manifest_hash": "manifest_1"},
                created_at_utc=now,
                activated_at_utc=now,
                superseded_at_utc=None,
                discarded_at_utc=None,
            )
        )
    candidate = IndexChunk(
        chunk_id="chunk_1",
        generation_id="generation_initial",
        publication_id="publication_1",
        document_id="document_1",
        document_version_id="version_1",
        space_id="space_1",
        text="text",
        embedding_text="text",
        locator={},
        snippet="text",
        media_kind="text/plain",
        manifest_hash="manifest_1",
    )

    facts = DocumentsRetrievalVisibilityPort(
        service._engine, service._object_store
    ).get_visibility_facts((candidate,))

    assert facts == {}
