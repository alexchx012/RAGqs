from __future__ import annotations

import pytest

from app.documents.indexing import IndexProcessingReceipt, IndexStagingRequest
from app.platform.errors import PlatformError


def test_staging_request_and_receipt_have_stable_identity() -> None:
    request = IndexStagingRequest(
        job_id="job_1",
        attempt_id="attempt_1",
        fencing_token=3,
        publication_id="publication_1",
        document_id="doc_1",
        document_version_id="version_1",
        space_id="space_1",
        operation="initial",
        base_active_version_id=None,
        expected_generation_id="generation_1",
        index_revision_at_start=0,
        object_manifest_ref="manifest_1",
        processing_config_snapshot={"parser": "none"},
        authorization_fence={"kind": "direct_ingest", "actor_id": "user_1"},
        input_manifest_hash="input_manifest_1",
        processing_profile_version="profile_1",
    )
    receipt = _receipt_for(request)
    assert request.job_id == receipt.job_id
    assert receipt.to_mapping()["publication_id"] == "publication_1"


def test_receipt_rejects_identity_mismatch() -> None:
    request = IndexStagingRequest(
        job_id="job_1",
        attempt_id="attempt_1",
        fencing_token=3,
        publication_id="publication_1",
        document_id="doc_1",
        document_version_id="version_1",
        space_id="space_1",
        operation="initial",
        base_active_version_id=None,
        expected_generation_id="generation_1",
        index_revision_at_start=0,
        object_manifest_ref="manifest_1",
        processing_config_snapshot={},
        authorization_fence={"kind": "direct_ingest", "actor_id": "user_1"},
        input_manifest_hash="input_manifest_1",
        processing_profile_version="profile_1",
    )
    receipt = _receipt_for(request, attempt_id="attempt_2")
    with pytest.raises(PlatformError) as error:
        receipt.validate_against(request)
    assert error.value.code == "processing_receipt_conflict"


def test_receipt_rejects_authorization_fence_mismatch() -> None:
    request = IndexStagingRequest(
        job_id="job_1",
        attempt_id="attempt_1",
        fencing_token=3,
        publication_id="publication_1",
        document_id="doc_1",
        document_version_id="version_1",
        space_id="space_1",
        operation="initial",
        base_active_version_id=None,
        expected_generation_id="generation_1",
        index_revision_at_start=0,
        object_manifest_ref="manifest_1",
        processing_config_snapshot={},
        authorization_fence={"kind": "direct_ingest", "actor_id": "user_1"},
        input_manifest_hash="input_manifest_1",
        processing_profile_version="profile_1",
    )
    receipt = _receipt_for(
        request,
        authorization_fence={"kind": "direct_ingest", "actor_id": "user_2"},
    )

    with pytest.raises(PlatformError) as error:
        receipt.validate_against(request)
    assert error.value.code == "processing_receipt_conflict"


def _request_with_identity(*, snapshot: dict[str, object] | None = None) -> IndexStagingRequest:
    return IndexStagingRequest(
        job_id="job_1",
        attempt_id="attempt_1",
        fencing_token=3,
        publication_id="publication_1",
        document_id="doc_1",
        document_version_id="version_1",
        space_id="space_1",
        operation="initial",
        base_active_version_id=None,
        expected_generation_id="generation_1",
        index_revision_at_start=0,
        object_manifest_ref="manifest_1",
        processing_config_snapshot=snapshot or {},
        authorization_fence={"kind": "direct_ingest", "actor_id": "user_1"},
        input_manifest_hash="input_manifest_1",
        processing_profile_version="profile_1",
    )


def _receipt_for(request: IndexStagingRequest, **changes: object) -> IndexProcessingReceipt:
    values: dict[str, object] = {
        "job_id": request.job_id,
        "attempt_id": request.attempt_id,
        "fencing_token": request.fencing_token,
        "publication_id": request.publication_id,
        "document_id": request.document_id,
        "document_version_id": request.document_version_id,
        "input_content_hash": "hash_1",
        "stage_resources": (),
        "processing_config_version": request.processing_profile_version,
        "generation_id": request.expected_generation_id,
        "authorization_fence": request.authorization_fence,
        "model_version": "model_1",
        "prompt_version": "prompt_1",
        "processing_summary": {
            "chunk_count": 0,
            "page_count": 0,
            "image_count": 0,
            "table_count": 0,
            "ocr": {},
            "tree": {},
            "cr": {},
        },
        "locator_snippet_integrity": {"locators_valid": True, "snippets_valid": True},
        "index_component_results": {"dense": {"state": "succeeded"}},
        "content_manifest_id": "manifest_1",
        "content_manifest_hash": "manifest_hash_1",
        "failure": None,
        "degradations": (),
        "input_manifest_hash": request.input_manifest_hash,
        "processing_profile_version": request.processing_profile_version,
        "space_id": request.space_id,
        "operation": request.operation,
        "base_active_version_id": request.base_active_version_id,
        "index_revision_at_start": request.index_revision_at_start,
        "object_manifest_ref": request.object_manifest_ref,
        "processing_config_snapshot": request.processing_config_snapshot,
    }
    values.update(changes)
    return IndexProcessingReceipt(**values)  # type: ignore[arg-type]


def test_receipt_requires_all_staging_request_echoes() -> None:
    request = _request_with_identity(snapshot={"parser": "none"})
    receipt = _receipt_for(request, processing_config_snapshot={})

    with pytest.raises(PlatformError) as error:
        receipt.validate_against(request)

    assert error.value.code == "processing_receipt_conflict"


@pytest.mark.parametrize(
    "field",
    (
        "space_id",
        "operation",
        "base_active_version_id",
        "index_revision_at_start",
        "object_manifest_ref",
        "processing_config_snapshot",
    ),
)
def test_receipt_mapping_requires_every_staging_request_echo(field: str) -> None:
    mapping = _receipt_for(_request_with_identity()).to_mapping()
    del mapping[field]

    with pytest.raises(PlatformError) as error:
        IndexProcessingReceipt.from_mapping(mapping)

    assert error.value.code == "validation_error"


def test_receipt_requires_stage_resources_for_indexed_chunks() -> None:
    request = _request_with_identity()
    with pytest.raises(PlatformError) as error:
        _receipt_for(
            request,
            processing_summary={
                "chunk_count": 1,
                "page_count": 0,
                "image_count": 0,
                "table_count": 0,
                "ocr": {},
                "tree": {},
                "cr": {},
            },
        )

    assert error.value.code == "validation_error"


def test_receipt_rejects_stage_resource_identity_mismatch() -> None:
    request = _request_with_identity(snapshot={"parser": "none"})
    resource = {
        "backend_kind": "index_chunk",
        "resource_id": "resource_1",
        "attempt_id": "attempt_other",
        "publication_id": request.publication_id,
        "fencing_token": request.fencing_token,
        "document_id": request.document_id,
        "document_version_id": request.document_version_id,
        "generation_id": request.expected_generation_id,
    }
    receipt = _receipt_for(
        request,
        stage_resources=(resource,),
        stage_resource_ids=("resource_1",),
        processing_summary={
            "chunk_count": 1,
            "page_count": 1,
            "image_count": 0,
            "table_count": 0,
            "ocr": {},
            "tree": {},
            "cr": {},
        },
    )

    with pytest.raises(PlatformError) as error:
        receipt.validate_against(request)

    assert error.value.code == "processing_receipt_conflict"


def test_processing_receipt_requires_processing_and_integrity_facts() -> None:
    with pytest.raises(PlatformError) as error:
        IndexProcessingReceipt.from_mapping(
            {
                "job_id": "job_1",
                "attempt_id": "attempt_1",
                "fencing_token": 1,
                "publication_id": "publication_1",
                "document_id": "doc_1",
                "document_version_id": "version_1",
                "input_content_hash": "hash_1",
                "stage_resources": [],
                "processing_config_version": "config_1",
                "generation_id": "generation_1",
                "authorization_fence": {"kind": "direct_ingest", "actor_id": "user_1"},
            }
        )
    assert error.value.code == "validation_error"
