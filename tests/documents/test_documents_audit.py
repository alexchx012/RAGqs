from __future__ import annotations

import pytest
from sqlalchemy import select

from app.platform.database import platform_audit_table
from app.platform.errors import PlatformError

from .test_commands import _accept, _upload
from .test_delete_visibility import _Lifecycle
from .test_jobs_and_fences import (
    _IndexingHandoff,
    _receipt_contract_fields,
    _receipt_request_echoes,
)


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


def test_version_restore_failure_writes_failure_audit(service, principal) -> None:
    """§9.3 审计事实：版本恢复失败（失败事实必须越过回滚落库）。"""

    created = service.create_initial_upload(
        principal=principal,
        space_id="space_1",
        files=[_upload()],
        idempotency_key="audit-upload-4",
    )
    item = created["items"][0]
    _accept(service, principal, item)

    # active 版本不可恢复（仅 superseded 可恢复）
    with pytest.raises(PlatformError) as error:
        service.restore_version(
            principal=principal,
            document_id=item["document_id"],
            document_version_id=item["document_version_id"],
            expected_version=1,
            idempotency_key="audit-restore-4",
        )
    assert error.value.code == "document_version_not_restorable"

    assert _audit_rows(service, "documents.version_restore") == [
        ("user_1", "documents.version_restore", "failed")
    ]


def test_receipt_authorization_failure_writes_audit(service, principal) -> None:
    """§9.3 审计事实：执行中授权失效（authorization_changed）。"""

    import hashlib

    service._indexing_handoff_port = _IndexingHandoff()
    created = service.create_initial_upload(
        principal=principal,
        space_id="space_1",
        files=[_upload()],
        idempotency_key="audit-acl-fence-1",
    )["items"][0]
    lease = service.claim_job(worker_id="worker-audit", job_id=created["job_id"])

    class _RevokedIdentity:
        def authorize_space(self, *, principal, space_id, action, connection=None):
            del principal, space_id, action
            raise PlatformError("space_action_forbidden", "Access was revoked", {}, 403)

    service._identity_access = _RevokedIdentity()
    with pytest.raises(PlatformError) as error:
        service.accept_processing_receipt(
            principal=principal,
            job_id=created["job_id"],
            receipt={
                "job_id": created["job_id"],
                "attempt_id": lease.attempt_id,
                "fencing_token": lease.fencing_token,
                "publication_id": lease.publication_id,
                "generation_id": lease.expected_generation_id,
                "document_id": created["document_id"],
                "document_version_id": created["document_version_id"],
                "input_content_hash": hashlib.sha256(b"hello").hexdigest(),
                "stage_resources": [],
                "processing_config_version": "v1",
                "authorization_fence": dict(lease.authorization_fence),
                **_receipt_request_echoes(service, lease.attempt_id),
                **_receipt_contract_fields(),
            },
        )
    assert error.value.code == "authorization_changed"

    assert _audit_rows(service, "documents.job_authorization") == [
        ("user_1", "documents.job_authorization", "authorization_changed")
    ]


def test_document_deletion_writes_lifecycle_audit_facts(service, principal) -> None:
    """§9.3 审计事实：删除五事实（在途 job 取消、清单封存、清理完成、进入 deleted）。"""

    service._lifecycle_port = _Lifecycle([])
    created = service.create_initial_upload(
        principal=principal,
        space_id="space_1",
        files=[_upload()],
        idempotency_key="audit-upload-5",
    )
    item = created["items"][0]
    # 不受理：摄取 job 保持 pending，删除时在途 job 被取消并落审计。
    deletion = service.delete_document(
        principal=principal,
        document_id=item["document_id"],
        expected_version=1,
        idempotency_key="audit-delete-5",
    )
    result = service.finalize_deletion(
        document_id=item["document_id"], deletion_id=deletion["deletion_id"]
    )

    assert result == {"document_id": item["document_id"], "state": "deleted"}
    facts = [row[2] for row in _audit_rows(service, "documents.deletion")]
    assert facts == [
        "jobs_cancelled",
        "cleanup_targets_sealed",
        "cleanup_completed",
        "document_deleted",
    ]


def test_failed_cleanup_target_writes_retry_audit(service, principal) -> None:
    """§9.3 审计事实：清理重试（与失败标记同一事务边界）。"""

    from app.platform.storage import MemoryObjectStore

    class _FailingDeleteStore(MemoryObjectStore):
        def delete(self, key: str) -> None:
            raise RuntimeError("object backend unavailable")

    service._lifecycle_port = _Lifecycle([])
    created = service.create_initial_upload(
        principal=principal,
        space_id="space_1",
        files=[_upload()],
        idempotency_key="audit-upload-6",
    )
    item = created["items"][0]
    deletion = service.delete_document(
        principal=principal,
        document_id=item["document_id"],
        expected_version=1,
        idempotency_key="audit-delete-6",
    )
    service._object_store = _FailingDeleteStore()
    result = service.finalize_deletion(
        document_id=item["document_id"], deletion_id=deletion["deletion_id"]
    )

    assert result == {"document_id": item["document_id"], "state": "cleaning"}
    facts = [row[2] for row in _audit_rows(service, "documents.deletion")]
    assert facts == ["jobs_cancelled", "cleanup_targets_sealed", "retried"]


def test_version_cleanup_retry_writes_audit_fact(service, principal) -> None:
    """§9.3 审计事实：版本清理重试（documents.version_cleanup / retried）。"""

    from datetime import UTC, datetime

    from app.platform.storage import MemoryObjectStore

    class _FailingDeleteStore(MemoryObjectStore):
        def delete(self, key: str) -> None:
            raise RuntimeError("object backend unavailable")

    created = service.create_initial_upload(
        principal=principal,
        space_id="space_1",
        files=[_upload()],
        idempotency_key="audit-upload-7",
    )["items"][0]
    _accept(service, principal, created)
    replacement = service.replace_version(
        principal=principal,
        document_id=created["document_id"],
        expected_version=1,
        file=_upload(content=b"replacement"),
        idempotency_key="audit-replace-7",
    )
    _accept(service, principal, replacement)
    service._now = lambda: datetime(2026, 2, 1, tzinfo=UTC)
    service._object_store = _FailingDeleteStore()

    assert service.purge_retained_versions(limit=10) == []

    facts = [row[2] for row in _audit_rows(service, "documents.version_cleanup")]
    assert facts == ["retried"]


def test_submission_create_and_content_read_write_audit_facts(service, principal) -> None:
    """§9.3 审计事实：投稿创建、待审原文件读取。"""

    submission = service.create_submission(
        principal=principal,
        space_id="space_1",
        file=_upload(),
        idempotency_key="audit-submission-1",
    )
    content = service.submission_content(
        principal=principal, submission_id=submission["submission_id"]
    )

    assert content[0] == b"hello"
    assert _audit_rows(service, "documents.submission_create") == [
        ("user_1", "documents.submission_create", "succeeded")
    ]
    assert _audit_rows(service, "documents.submission_content") == [
        ("user_1", "documents.submission_content", "succeeded")
    ]
