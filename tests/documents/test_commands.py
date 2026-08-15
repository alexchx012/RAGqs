from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, func, select, update

from app.documents.indexing import NoopIndexingHandoff
from app.documents.schema import (
    document_versions_table,
    documents_metadata,
    documents_table,
    index_revisions_table,
    ingestion_attempts_table,
)
from app.documents.service import DocumentsService, DocumentUpload
from app.identity.service import AuthPrincipal
from app.platform.errors import PlatformError
from app.platform.storage import MemoryObjectStore


class _Identity:
    def authorize_space(self, *, principal, space_id: str, action: str) -> str:
        assert principal.user_id == "user_1"
        assert space_id == "space_1"
        assert action in {"manage", "contribute", "read"}
        return "manage"


@pytest.fixture()
def service():
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    documents_metadata.create_all(engine)
    return DocumentsService(
        engine,
        now=lambda: datetime(2026, 1, 1, tzinfo=UTC),
        object_store=MemoryObjectStore(),
        identity_access=_Identity(),
        indexing_handoff_port=NoopIndexingHandoff(),
    )


@pytest.fixture()
def principal() -> AuthPrincipal:
    return AuthPrincipal(
        user_id="user_1",
        auth_session_id="session_1",
        username="alice",
        role="user",
        department_id=None,
    )


def _upload(name: str = "guide.txt", content: bytes = b"hello") -> DocumentUpload:
    return DocumentUpload(
        filename=name,
        content=content,
        media_kind="text/plain",
    )


def _service_with_upload_limit(service, max_upload_bytes: int) -> DocumentsService:
    return DocumentsService(
        service._engine,
        now=service._now,
        object_store=service._object_store,
        identity_access=service._identity_access,
        indexing_handoff_port=service._indexing_handoff_port,
        max_upload_bytes=max_upload_bytes,
    )


def _accept(service, principal, item, *, stage_resources=None):
    lease = service.claim_job(worker_id="worker_1", job_id=item["job_id"])
    with service._engine.connect() as connection:
        input_content_hash = connection.execute(
            select(document_versions_table.c.content_hash_sha256).where(
                document_versions_table.c.id == item["document_version_id"]
            )
        ).scalar_one()
        staging_request = connection.execute(
            select(ingestion_attempts_table.c.staging_request_json).where(
                ingestion_attempts_table.c.id == lease.attempt_id
            )
        ).scalar_one()
    resources = stage_resources
    if resources is None:
        resources = [
            {
                "backend_kind": "index_chunk",
                "resource_id": f"{lease.attempt_id}:{lease.publication_id}:chunk_1",
                "attempt_id": lease.attempt_id,
                "publication_id": lease.publication_id,
                "fencing_token": lease.fencing_token,
                "document_id": item["document_id"],
                "document_version_id": item["document_version_id"],
                "generation_id": lease.expected_generation_id,
            }
        ]
    else:
        # 调用方（如 retention 测试）只需给出 backend_kind/resource_id；
        # 补齐 durable-search-indexing 收紧后的资源身份键，保证回执与
        # staging request 的逐资源一致性校验通过。
        resources = [
            {
                **resource,
                "attempt_id": lease.attempt_id,
                "publication_id": lease.publication_id,
                "fencing_token": lease.fencing_token,
                "document_id": item["document_id"],
                "document_version_id": item["document_version_id"],
                "generation_id": lease.expected_generation_id,
            }
            for resource in stage_resources
        ]
    return service.accept_processing_receipt(
        principal=principal,
        job_id=item["job_id"],
        receipt={
            "job_id": item["job_id"],
            "attempt_id": lease.attempt_id,
            "fencing_token": lease.fencing_token,
            "publication_id": lease.publication_id,
            "generation_id": lease.expected_generation_id,
            "document_id": item["document_id"],
            "document_version_id": item["document_version_id"],
            "input_content_hash": input_content_hash,
            "stage_resources": resources,
            "processing_config_version": "test-v1",
            "model_version": "test-model-v1",
            "prompt_version": "test-prompt-v1",
            "processing_summary": {
                "pages": 1,
                "images": 0,
                "chunk_count": len(resources),
                "page_count": 1,
                "image_count": 0,
                "table_count": 0,
                "ocr": {},
                "tree": {},
                "cr": {},
            },
            "locator_snippet_integrity": {"locators_valid": True, "snippets_valid": True},
            "index_component_results": {"dense": {"state": "succeeded"}},
            "content_manifest_id": f"manifest-{item['publication_id']}",
            "content_manifest_hash": f"manifest-hash-{item['publication_id']}",
            "failure": None,
            "degradations": [],
            "authorization_fence": dict(lease.authorization_fence),
            "input_manifest_hash": staging_request["input_manifest_hash"],
            "processing_profile_version": staging_request["processing_profile_version"],
            "stage_resource_ids": [resource["resource_id"] for resource in resources],
            "space_id": staging_request["space_id"],
            "operation": staging_request["operation"],
            "base_active_version_id": staging_request["base_active_version_id"],
            "index_revision_at_start": staging_request["index_revision_at_start"],
            "object_manifest_ref": staging_request["object_manifest_ref"],
            "processing_config_snapshot": staging_request["processing_config_snapshot"],
        },
    )


def test_initial_upload_stays_staged_until_processing_receipt(service, principal) -> None:
    result = service.create_initial_upload(
        principal=principal,
        space_id="space_1",
        files=[_upload()],
        idempotency_key="upload-1",
    )

    item = result["items"][0]
    assert item["deduplicated"] is False
    assert item["job_id"]
    assert service.list_documents(principal=principal, space_id="space_1")["items"] == []

    _accept(service, principal, item)

    visible = service.list_documents(principal=principal, space_id="space_1")
    assert visible["total"] == 1
    assert visible["items"][0]["document_version_id"] == item["document_version_id"]
    assert visible["items"][0]["usage"] == {"pages": 1, "images": 0}


def test_initial_upload_rejects_an_oversized_file_before_persisting(service, principal) -> None:
    limited = _service_with_upload_limit(service, max_upload_bytes=4)

    with pytest.raises(PlatformError) as error:
        limited.create_initial_upload(
            principal=principal,
            space_id="space_1",
            files=[_upload(content=b"12345")],
            idempotency_key="oversized-initial-upload",
        )

    assert error.value.code == "upload_too_large"
    with limited._engine.connect() as connection:
        assert connection.execute(select(func.count()).select_from(documents_table)).scalar_one() == 0
        assert (
            connection.execute(select(func.count()).select_from(document_versions_table)).scalar_one()
            == 0
        )


def test_replace_rejects_an_oversized_file_without_creating_a_version(service, principal) -> None:
    limited = _service_with_upload_limit(service, max_upload_bytes=4)
    item = limited.create_initial_upload(
        principal=principal,
        space_id="space_1",
        files=[_upload(content=b"okay")],
        idempotency_key="initial-for-oversized-replacement",
    )["items"][0]
    _accept(limited, principal, item)

    with pytest.raises(PlatformError) as error:
        limited.replace_version(
            principal=principal,
            document_id=item["document_id"],
            expected_version=1,
            file=_upload(content=b"12345"),
            idempotency_key="oversized-replacement",
        )

    assert error.value.code == "upload_too_large"
    with limited._engine.connect() as connection:
        assert (
            connection.execute(select(func.count()).select_from(document_versions_table)).scalar_one()
            == 1
        )


def test_same_space_name_and_hash_is_deduplicated(service, principal) -> None:
    first = service.create_initial_upload(
        principal=principal,
        space_id="space_1",
        files=[_upload()],
        idempotency_key="upload-1",
    )
    second = service.create_initial_upload(
        principal=principal,
        space_id="space_1",
        files=[_upload()],
        idempotency_key="upload-2",
    )

    assert second["items"][0]["deduplicated"] is True
    assert second["items"][0]["document_id"] == first["items"][0]["document_id"]
    with service._engine.connect() as connection:
        assert connection.execute(select(documents_table.c.id)).all() == [
            (first["items"][0]["document_id"],)
        ]


def test_idempotency_replays_and_conflicts_on_changed_file(service, principal) -> None:
    first = service.create_initial_upload(
        principal=principal,
        space_id="space_1",
        files=[_upload()],
        idempotency_key="upload-1",
    )
    replay = service.create_initial_upload(
        principal=principal,
        space_id="space_1",
        files=[_upload()],
        idempotency_key="upload-1",
    )
    assert replay == first

    with pytest.raises(PlatformError) as error:
        service.create_initial_upload(
            principal=principal,
            space_id="space_1",
            files=[_upload(content=b"changed")],
            idempotency_key="upload-1",
        )
    assert error.value.code == "idempotency_key_conflict"


def test_replace_requires_expected_version_and_keeps_old_publication(service, principal) -> None:
    first = service.create_initial_upload(
        principal=principal,
        space_id="space_1",
        files=[_upload()],
        idempotency_key="upload-1",
    )
    item = first["items"][0]
    _accept(service, principal, item)

    with pytest.raises(PlatformError) as error:
        service.replace_version(
            principal=principal,
            document_id=item["document_id"],
            expected_version=99,
            file=_upload(content=b"new"),
            idempotency_key="replace-1",
        )
    assert error.value.code == "document_version_conflict"

    replacement = service.replace_version(
        principal=principal,
        document_id=item["document_id"],
        expected_version=1,
        file=DocumentUpload(filename="revised.pdf", content=b"new", media_kind="application/pdf"),
        idempotency_key="replace-1",
    )
    assert replacement["job_id"]
    assert (
        service.list_documents(principal=principal, space_id="space_1")["items"][0][
            "document_version_id"
        ]
        == item["document_version_id"]
    )

    _accept(service, principal, replacement)
    active = service.list_documents(principal=principal, space_id="space_1")["items"][0]
    assert active["name"] == "revised.pdf"
    assert active["media_kind"] == "application/pdf"


def test_replace_replays_before_document_lifecycle_validation(service, principal) -> None:
    first = service.create_initial_upload(
        principal=principal,
        space_id="space_1",
        files=[_upload()],
        idempotency_key="upload-1",
    )["items"][0]
    _accept(service, principal, first)

    replacement = service.replace_version(
        principal=principal,
        document_id=first["document_id"],
        expected_version=1,
        file=_upload(content=b"replacement"),
        idempotency_key="replace-1",
    )
    with service._engine.begin() as connection:
        connection.execute(
            update(documents_table)
            .where(documents_table.c.id == first["document_id"])
            .values(lifecycle_status="deleted")
        )

    assert (
        service.replace_version(
            principal=principal,
            document_id=first["document_id"],
            expected_version=1,
            file=_upload(content=b"replacement"),
            idempotency_key="replace-1",
        )
        == replacement
    )


def test_index_revisions_are_instance_wide_monotonic(service, principal) -> None:
    first = service.create_initial_upload(
        principal=principal,
        space_id="space_1",
        files=[_upload(name="first.txt", content=b"first")],
        idempotency_key="upload-1",
    )["items"][0]
    _accept(service, principal, first)
    second = service.create_initial_upload(
        principal=principal,
        space_id="space_1",
        files=[_upload(name="second.txt", content=b"second")],
        idempotency_key="upload-2",
    )["items"][0]
    _accept(service, principal, second)

    with service._engine.connect() as connection:
        revisions = (
            connection.execute(
                select(index_revisions_table.c.revision).order_by(index_revisions_table.c.revision)
            )
            .scalars()
            .all()
        )
    assert revisions == [1, 2]


def test_document_list_projects_usage_from_the_active_publication(service, principal) -> None:
    item = service.create_initial_upload(
        principal=principal,
        space_id="space_1",
        files=[_upload()],
        idempotency_key="upload-usage-1",
    )["items"][0]
    _accept(service, principal, item)

    assert service.list_documents(principal=principal, space_id="space_1")["items"][0]["usage"] == {
        "pages": 1,
        "images": 0,
    }
