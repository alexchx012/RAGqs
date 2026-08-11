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
    )
    receipt = IndexProcessingReceipt(
        job_id="job_1",
        attempt_id="attempt_1",
        fencing_token=3,
        publication_id="publication_1",
        document_id="doc_1",
        document_version_id="version_1",
        input_content_hash="hash_1",
        stage_resources=(),
        processing_config_version="config_1",
        generation_id="generation_1",
        authorization_fence={"kind": "direct_ingest", "actor_id": "user_1"},
        model_version="model_1",
        prompt_version="prompt_1",
        processing_summary={
            "chunk_count": 1,
            "page_count": 1,
            "image_count": 0,
            "table_count": 0,
            "ocr": {},
            "tree": {},
            "cr": {},
        },
        locator_snippet_integrity={"locators_valid": True, "snippets_valid": True},
        index_component_results={"dense": {"state": "succeeded"}},
        content_manifest_id="manifest_1",
        content_manifest_hash="manifest_hash_1",
        failure=None,
        degradations=(),
    )
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
    )
    receipt = IndexProcessingReceipt(
        job_id="job_1",
        attempt_id="attempt_2",
        fencing_token=3,
        publication_id="publication_1",
        document_id="doc_1",
        document_version_id="version_1",
        input_content_hash="hash_1",
        stage_resources=(),
        processing_config_version="config_1",
        generation_id="generation_1",
        authorization_fence={"kind": "direct_ingest", "actor_id": "user_1"},
        model_version="model_1",
        prompt_version="prompt_1",
        processing_summary={
            "chunk_count": 1,
            "page_count": 1,
            "image_count": 0,
            "table_count": 0,
            "ocr": {},
            "tree": {},
            "cr": {},
        },
        locator_snippet_integrity={"locators_valid": True, "snippets_valid": True},
        index_component_results={"dense": {"state": "succeeded"}},
        content_manifest_id="manifest_1",
        content_manifest_hash="manifest_hash_1",
        failure=None,
        degradations=(),
    )
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
    )
    receipt = IndexProcessingReceipt(
        job_id="job_1",
        attempt_id="attempt_1",
        fencing_token=3,
        publication_id="publication_1",
        document_id="doc_1",
        document_version_id="version_1",
        input_content_hash="hash_1",
        stage_resources=(),
        processing_config_version="config_1",
        generation_id="generation_1",
        authorization_fence={"kind": "direct_ingest", "actor_id": "user_2"},
        model_version="model_1",
        prompt_version="prompt_1",
        processing_summary={
            "chunk_count": 1,
            "page_count": 1,
            "image_count": 0,
            "table_count": 0,
            "ocr": {},
            "tree": {},
            "cr": {},
        },
        locator_snippet_integrity={"locators_valid": True, "snippets_valid": True},
        index_component_results={"dense": {"state": "succeeded"}},
        content_manifest_id="manifest_1",
        content_manifest_hash="manifest_hash_1",
        failure=None,
        degradations=(),
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
