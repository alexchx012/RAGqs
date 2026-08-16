from __future__ import annotations

from dataclasses import dataclass

import pytest

from app.outbox.ports import DocumentNotificationRedactionReceipt
from app.platform.errors import PlatformError

from .test_commands import _accept, _upload


class _RecordingHandoff:
    def __init__(self) -> None:
        self.discarded: list[object] = []

    def publish(self, request, *, connection, receipt=None) -> None:
        del request, connection, receipt

    def discard(self, request, *, connection) -> None:
        del connection
        self.discarded.append(request)


@dataclass
class _Lifecycle:
    commands: list[object]

    def redact_document_notifications(self, command, *, connection):
        del connection
        self.commands.append(command)
        return DocumentNotificationRedactionReceipt(
            operation_id=command.operation_id,
            deletion_id=command.deletion_id,
            state="completed",
            redacted_notification_count=2,
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


def test_delete_redacts_before_committing_pending_delete(service, principal) -> None:
    lifecycle = _Lifecycle([])
    service._lifecycle_port = lifecycle
    item = _accepted(service, principal)

    response = service.delete_document(
        principal=principal,
        document_id=item["document_id"],
        expected_version=1,
        idempotency_key="delete-1",
    )

    assert response["state"] == "pending_delete"
    assert lifecycle.commands[0].document_id == item["document_id"]
    assert lifecycle.commands[0].document_version_ids == (item["document_version_id"],)
    assert service.list_documents(principal=principal, space_id="space_1")["items"] == []


def test_delete_uses_documents_scoped_lifecycle_command(service, principal) -> None:
    lifecycle = _Lifecycle([])
    service._lifecycle_port = lifecycle
    item = _accepted(service, principal)
    response = service.delete_document(
        principal=principal,
        document_id=item["document_id"],
        expected_version=1,
        idempotency_key="delete-1",
    )

    assert lifecycle.commands[0].deletion_id == response["deletion_id"]
    assert lifecycle.commands[0].caller_principal == "documents"


def test_delete_fails_closed_without_lifecycle_port(service, principal) -> None:
    item = _accepted(service, principal)
    with pytest.raises(PlatformError) as error:
        service.delete_document(
            principal=principal,
            document_id=item["document_id"],
            expected_version=1,
            idempotency_key="delete-1",
        )
    assert error.value.code == "document_lifecycle_unavailable"


def test_delete_discards_every_active_indexing_attempt(service, principal) -> None:
    lifecycle = _Lifecycle([])
    handoff = _RecordingHandoff()
    service._lifecycle_port = lifecycle
    service._indexing_handoff_port = handoff
    created = service.create_initial_upload(
        principal=principal,
        space_id="space_1",
        files=[_upload()],
        idempotency_key="upload-delete-discard",
    )["items"][0]
    lease = service.claim_job(worker_id="worker-delete", job_id=created["job_id"])

    service.delete_document(
        principal=principal,
        document_id=created["document_id"],
        expected_version=1,
        idempotency_key="delete-discard",
    )

    assert [request.attempt_id for request in handoff.discarded] == [lease.attempt_id]
