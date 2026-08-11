from __future__ import annotations

import pytest

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
