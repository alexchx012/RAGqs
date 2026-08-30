from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select

from app.documents.indexing import NoopIndexingHandoff
from app.documents.schema import (
    document_deletion_cleanup_targets_table,
    document_version_cleanup_targets_table,
)
from app.outbox.ports import DocumentNotificationRedactionReceipt

from .test_commands import _accept, _upload


@dataclass
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


class _FailOnceStore:
    def __init__(self, store) -> None:
        self._store = store
        self._fail_next_delete = True

    def __getattr__(self, name):
        return getattr(self._store, name)

    def delete(self, key: str) -> None:
        if self._fail_next_delete:
            self._fail_next_delete = False
            raise RuntimeError("temporary object store outage")
        self._store.delete(key)


class _FailOnceDerivedCleanup(NoopIndexingHandoff):
    def __init__(self) -> None:
        self.cleaned: list[tuple[str, str]] = []
        self._failed = False

    def cleanup_resource(self, resource, *, connection) -> None:
        del connection
        target = (str(resource["backend_kind"]), str(resource["resource_id"]))
        self.cleaned.append(target)
        if target == ("index", "index-1") and not self._failed:
            self._failed = True
            raise RuntimeError("temporary derived-resource outage")


class _AlwaysFailDerivedCleanup(NoopIndexingHandoff):
    def __init__(self) -> None:
        self.calls = 0

    def cleanup_resource(self, resource, *, connection) -> None:
        del resource, connection
        self.calls += 1
        raise RuntimeError("persistent derived-resource outage")


def test_completed_delete_becomes_non_descriptive_tombstone(service, principal) -> None:
    service._lifecycle_port = _Lifecycle()
    created = service.create_initial_upload(
        principal=principal,
        space_id="space_1",
        files=[_upload()],
        idempotency_key="upload-1",
    )
    item = created["items"][0]
    _accept(service, principal, item)
    deletion = service.delete_document(
        principal=principal,
        document_id=item["document_id"],
        expected_version=2,
        idempotency_key="delete-1",
    )
    service.finalize_deletion(document_id=item["document_id"], deletion_id=deletion["deletion_id"])
    assert service.list_documents(principal=principal, space_id="space_1")["items"] == []


def test_expired_superseded_version_is_purged_through_cleanup_targets(service, principal) -> None:
    created = service.create_initial_upload(
        principal=principal,
        space_id="space_1",
        files=[_upload()],
        idempotency_key="upload-retention-1",
    )["items"][0]
    _accept(service, principal, created)
    replacement = service.replace_version(
        principal=principal,
        document_id=created["document_id"],
        expected_version=2,
        file=_upload(content=b"replacement"),
        idempotency_key="replace-retention-1",
    )
    _accept(service, principal, replacement)
    service._now = lambda: datetime(2026, 2, 1, tzinfo=UTC)

    assert service.purge_retained_versions(limit=10) == [created["document_version_id"]]
    versions = service.list_versions(principal=principal, document_id=created["document_id"])
    retired = next(
        item
        for item in versions["items"]
        if item["document_version_id"] == created["document_version_id"]
    )
    assert retired["status"] == "purged"
    assert retired["content_available"] is False


def test_deletion_cleanup_retries_only_failed_targets(service, principal) -> None:
    service._lifecycle_port = _Lifecycle()
    item = service.create_initial_upload(
        principal=principal,
        space_id="space_1",
        files=[_upload()],
        idempotency_key="upload-delete-retry-1",
    )["items"][0]
    _accept(service, principal, item)
    service._object_store = _FailOnceStore(service._object_store)
    deletion = service.delete_document(
        principal=principal,
        document_id=item["document_id"],
        expected_version=2,
        idempotency_key="delete-retry-1",
    )

    assert service.finalize_deletion(
        document_id=item["document_id"], deletion_id=deletion["deletion_id"]
    ) == {"document_id": item["document_id"], "state": "cleaning"}
    with service._engine.connect() as connection:
        target = (
            connection.execute(
                select(document_deletion_cleanup_targets_table).where(
                    document_deletion_cleanup_targets_table.c.backend_kind == "object_store"
                )
            )
            .mappings()
            .one()
        )
    assert target["state"] == "failed"
    assert target["attempt_count"] == 1

    assert service.finalize_deletion(
        document_id=item["document_id"], deletion_id=deletion["deletion_id"]
    ) == {"document_id": item["document_id"], "state": "deleted"}
    with service._engine.connect() as connection:
        target = (
            connection.execute(
                select(document_deletion_cleanup_targets_table).where(
                    document_deletion_cleanup_targets_table.c.backend_kind == "object_store"
                )
            )
            .mappings()
            .one()
        )
    assert target["state"] == "completed"
    assert target["attempt_count"] == 2


def test_retention_registers_and_cleans_receipt_derived_resources(service, principal) -> None:
    original = service.create_initial_upload(
        principal=principal,
        space_id="space_1",
        files=[_upload()],
        idempotency_key="upload-derived-retention-1",
    )["items"][0]
    _accept(
        service,
        principal,
        original,
        stage_resources=[
            {"backend_kind": "index", "resource_id": "index-old"},
            {"backend_kind": "parsing", "resource_id": "parse-old"},
        ],
    )
    replacement = service.replace_version(
        principal=principal,
        document_id=original["document_id"],
        expected_version=2,
        file=_upload(content=b"replacement"),
        idempotency_key="replace-derived-retention-1",
    )
    _accept(service, principal, replacement)
    service._now = lambda: datetime(2026, 2, 1, tzinfo=UTC)

    assert service.purge_retained_versions(limit=10) == [original["document_version_id"]]
    with service._engine.connect() as connection:
        resources = set(
            connection.execute(
                select(
                    document_version_cleanup_targets_table.c.backend_kind,
                    document_version_cleanup_targets_table.c.resource_id,
                    document_version_cleanup_targets_table.c.state,
                ).where(
                    document_version_cleanup_targets_table.c.document_version_id
                    == original["document_version_id"]
                )
            ).all()
        )
    assert ("index", "index-old", "completed") in resources
    assert ("parsing", "parse-old", "completed") in resources


def test_deletion_waits_for_and_retries_failed_derived_cleanup_target(service, principal) -> None:
    service._lifecycle_port = _Lifecycle()
    derived_cleanup = _FailOnceDerivedCleanup()
    service._indexing_handoff_port = derived_cleanup
    item = service.create_initial_upload(
        principal=principal,
        space_id="space_1",
        files=[_upload()],
        idempotency_key="upload-derived-delete-1",
    )["items"][0]
    _accept(
        service,
        principal,
        item,
        stage_resources=[
            {"backend_kind": "index", "resource_id": "index-1"},
            {"backend_kind": "parsing", "resource_id": "parse-1"},
        ],
    )
    deletion = service.delete_document(
        principal=principal,
        document_id=item["document_id"],
        expected_version=2,
        idempotency_key="delete-derived-1",
    )

    assert service.finalize_deletion(
        document_id=item["document_id"], deletion_id=deletion["deletion_id"]
    ) == {"document_id": item["document_id"], "state": "cleaning"}
    with service._engine.connect() as connection:
        targets = {
            (row["backend_kind"], row["resource_id"]): row["state"]
            for row in connection.execute(
                select(document_deletion_cleanup_targets_table)
            ).mappings()
        }
    assert targets[("index", "index-1")] == "failed"
    assert targets[("parsing", "parse-1")] == "pending"

    assert service.finalize_deletion(
        document_id=item["document_id"], deletion_id=deletion["deletion_id"]
    ) == {"document_id": item["document_id"], "state": "deleted"}
    # 派生索引目标排首段；世代枚举目标缀于 stage 索引资源之后。
    assert derived_cleanup.cleaned[:4] == [
        ("index", "index-1"),
        ("index", "index-1"),
        ("index", "index_generation:generation_initial"),
        ("parsing", "parse-1"),
    ]


def test_deletion_cleanup_stops_retrying_a_target_after_three_failures(service, principal) -> None:
    service._lifecycle_port = _Lifecycle()
    derived_cleanup = _AlwaysFailDerivedCleanup()
    service._indexing_handoff_port = derived_cleanup
    item = service.create_initial_upload(
        principal=principal,
        space_id="space_1",
        files=[_upload()],
        idempotency_key="upload-derived-retry-limit-1",
    )["items"][0]
    _accept(
        service,
        principal,
        item,
        stage_resources=[{"backend_kind": "index", "resource_id": "index-persistent"}],
    )
    deletion = service.delete_document(
        principal=principal,
        document_id=item["document_id"],
        expected_version=2,
        idempotency_key="delete-derived-retry-limit-1",
    )

    for _ in range(3):
        assert service.finalize_deletion(
            document_id=item["document_id"], deletion_id=deletion["deletion_id"]
        ) == {"document_id": item["document_id"], "state": "cleaning"}

    assert service.finalize_deletion(
        document_id=item["document_id"], deletion_id=deletion["deletion_id"]
    ) == {"document_id": item["document_id"], "state": "cleaning"}
    assert derived_cleanup.calls == 3
    with service._engine.connect() as connection:
        target = (
            connection.execute(
                select(document_deletion_cleanup_targets_table).where(
                    document_deletion_cleanup_targets_table.c.backend_kind == "index",
                    document_deletion_cleanup_targets_table.c.resource_id == "index-persistent",
                )
            )
            .mappings()
            .one()
        )
    assert target["state"] == "failed"
    assert target["attempt_count"] == 3
    assert target["last_error"] == "RuntimeError"
