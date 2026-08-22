from __future__ import annotations

from sqlalchemy import select

from app.platform.database import platform_audit_table

from .test_commands import _accept, _upload
from .test_delete_visibility import _Lifecycle


def _audit_rows(service, resource_type: str) -> list[tuple[str, str, str]]:
    with service._engine.connect() as connection:
        return connection.execute(
            select(
                platform_audit_table.c.actor_id,
                platform_audit_table.c.resource_type,
                platform_audit_table.c.result,
            ).where(platform_audit_table.c.resource_type == resource_type)
        ).all()


def test_document_delete_writes_audit_event(service, principal) -> None:
    service._lifecycle_port = _Lifecycle([])
    created = service.create_initial_upload(
        principal=principal,
        space_id="space_1",
        files=[_upload()],
        idempotency_key="audit-upload-1",
    )
    item = created["items"][0]
    _accept(service, principal, item)

    service.delete_document(
        principal=principal,
        document_id=item["document_id"],
        expected_version=1,
        idempotency_key="audit-delete-1",
    )

    assert _audit_rows(service, "documents.delete") == [("user_1", "documents.delete", "succeeded")]


def test_failed_document_delete_writes_no_success_audit(service, principal) -> None:
    service._lifecycle_port = _Lifecycle([])
    created = service.create_initial_upload(
        principal=principal,
        space_id="space_1",
        files=[_upload()],
        idempotency_key="audit-upload-2",
    )
    item = created["items"][0]
    _accept(service, principal, item)

    import pytest

    from app.platform.errors import PlatformError

    with pytest.raises(PlatformError):
        service.delete_document(
            principal=principal,
            document_id=item["document_id"],
            expected_version=99,
            idempotency_key="audit-delete-2",
        )

    assert _audit_rows(service, "documents.delete") == []


def test_version_restore_writes_audit_event(service, principal) -> None:
    created = service.create_initial_upload(
        principal=principal,
        space_id="space_1",
        files=[_upload()],
        idempotency_key="audit-upload-3",
    )
    item = created["items"][0]
    _accept(service, principal, item)
    replacement = service.replace_version(
        principal=principal,
        document_id=item["document_id"],
        expected_version=1,
        file=_upload(content=b"second edition"),
        idempotency_key="audit-replace-3",
    )
    _accept(service, principal, replacement)

    service.restore_version(
        principal=principal,
        document_id=item["document_id"],
        document_version_id=item["document_version_id"],
        expected_version=2,
        idempotency_key="audit-restore-3",
    )

    assert _audit_rows(service, "documents.version_restore") == [
        ("user_1", "documents.version_restore", "succeeded")
    ]
