from __future__ import annotations

import io
from datetime import UTC, datetime
from hashlib import sha256

import pytest
from openpyxl import Workbook
from sqlalchemy import create_engine

from app.documents.indexing import IndexStagingRequest
from app.documents.public_graph import PublicGraphSourceService
from app.documents.schema import (
    document_versions_table,
    documents_metadata,
    documents_table,
    publications_table,
)
from app.indexing import (
    ContentProcessor,
    GenerationManager,
    GraphComponentCoordinator,
    IndexGenerationComponentInput,
    SqlAlchemyGenerationManager,
    SqlAlchemyIndexingRepository,
    indexing_metadata,
)
from app.platform.errors import PlatformError


class _Outbox:
    def publish_public_graph_source_change(self, **event: object) -> str:
        del event
        return "event_1"


def _request() -> IndexStagingRequest:
    return IndexStagingRequest(
        job_id="job_1",
        attempt_id="attempt_1",
        fencing_token=1,
        publication_id="publication_1",
        document_id="document_1",
        document_version_id="version_1",
        space_id="space_1",
        operation="initial",
        base_active_version_id=None,
        expected_generation_id="generation_initial",
        index_revision_at_start=0,
        object_manifest_ref="manifest_1",
        processing_config_snapshot={},
        authorization_fence={"actor_id": "user_1"},
        input_manifest_hash="manifest_hash_1",
        processing_profile_version="profile_1",
    )


def _source(engine):
    return PublicGraphSourceService(
        engine,
        trusted_consumers={"indexing": {"indexer_1"}},
        outbox_port=_Outbox(),
    )


def _source_snapshot(source, *, document_id: str = "document_1", publications=()):
    return source.record_source_change(
        space_id="public",
        document_id=document_id,
        change_type="publish",
        publications=publications,
    )


def _graph_input(snapshot, source, *, operation_id: str, target_generation_id: str):
    return IndexGenerationComponentInput(
        component_kind="public_graph",
        target_generation_id=target_generation_id,
        target_generation_fence="generation_fence_1",
        source_snapshot=snapshot,
        source_manifest_hash=snapshot.source_manifest_hash,
        source_head_fence=source.get_current_head().source_head_fence,
        operation_id=operation_id,
    )


def _reserve_and_activate_graph(coordinator, manager, source):
    snapshot = _source_snapshot(source)
    grant = coordinator.reserve_graph_component_stage(
        graph_build_id="graph_1",
        expected_source_revision=snapshot.source_revision,
        expected_active_generation_id=manager.active_generation_id,
        operation_id="stage_1",
        component_input=_graph_input(
            snapshot, source, operation_id="stage_1", target_generation_id="generation_graph"
        ),
    )
    receipt = coordinator.stage_public_graph_component(
        grant,
        graph_resource_manifest_hash="graph_manifest_1",
        graph_resource_ids=["graph_resource_1"],
        build_receipt_hash="build_receipt_1",
    )
    coordinator.release_graph_component(
        target_generation_id=grant.target_generation_id,
        target_generation_fence=grant.target_generation_fence,
        component_stage_id=receipt.component_stage_id,
        source_revision=receipt.source_revision,
        source_manifest_hash=receipt.source_manifest_hash,
        source_head_fence=receipt.source_head_fence,
        operation_id="release_1",
    )
    return receipt


def _insert_active_document(connection) -> dict[str, str]:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    content_hash = sha256(b"document").hexdigest()
    connection.execute(
        documents_table.insert().values(
            id="document_1",
            space_id="public",
            lifecycle_status="active",
            active_version_id="version_1",
            pending_version_id=None,
            active_operation_job_id=None,
            deletion_id=None,
            version=1,
            name="document.md",
            normalized_name="document.md",
            media_kind="text/markdown",
            uploaded_at_utc=now,
            created_by_user_id="user_1",
            created_at_utc=now,
            updated_at_utc=now,
        )
    )
    connection.execute(
        document_versions_table.insert().values(
            id="version_1",
            document_id="document_1",
            version_number=1,
            status="active",
            content_hash_sha256=content_hash,
            object_manifest_json={"object_key": "documents/document_1/original"},
            original_object_key="documents/document_1/original",
            file_name="document.md",
            media_kind="text/markdown",
            size_bytes=8,
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
        publications_table.insert().values(
            id="publication_1",
            document_id="document_1",
            document_version_id="version_1",
            job_id="job_1",
            attempt_id="attempt_1",
            generation_id="generation_initial",
            status="active",
            resource_manifest_json={
                "content_manifest_id": "manifest_1",
                "content_manifest_hash": content_hash,
                "processing_profile_version": "profile_1",
                "processing_config_snapshot": {},
            },
            created_at_utc=now,
            activated_at_utc=now,
            superseded_at_utc=None,
            discarded_at_utc=None,
        )
    )
    return {
        "document_id": "document_1",
        "document_version_id": "version_1",
        "publication_id": "publication_1",
        "content_manifest_id": "manifest_1",
        "content_manifest_hash": content_hash,
    }


def test_excel_marks_horizontally_merged_total_row() -> None:
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = "Revenue"
    worksheet.append(["category", "amount", "period"])
    worksheet.append(["North", 10, "January"])
    worksheet.append(["Total", None, None])
    worksheet.merge_cells("A3:C3")
    stream = io.BytesIO()
    workbook.save(stream)

    output = ContentProcessor().process(
        _request(),
        stream.getvalue(),
        media_kind="xlsx",
        content_manifest_id="manifest_1",
        content_manifest_hash="manifest_hash_1",
    )

    assert output.chunks[0].metadata["total_rows"] == [3]
    assert output.receipt.processing_summary["row_groups"][0]["total_rows"] == [3]


def test_stage_rejects_changed_source_head_before_registering_graph_resources() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    documents_metadata.create_all(engine)
    source = _source(engine)
    first = _source_snapshot(source)
    manager = GenerationManager()
    coordinator = GraphComponentCoordinator(manager, source, consumer_id="indexer_1")
    grant = coordinator.reserve_graph_component_stage(
        graph_build_id="graph_1",
        expected_source_revision=first.source_revision,
        expected_active_generation_id=manager.active_generation_id,
        operation_id="stage_1",
        component_input=_graph_input(
            first, source, operation_id="stage_1", target_generation_id="generation_graph"
        ),
    )
    _source_snapshot(source, document_id="document_2")

    with pytest.raises(PlatformError) as error:
        coordinator.stage_public_graph_component(
            grant,
            graph_resource_manifest_hash="graph_manifest_1",
            graph_resource_ids=["graph_resource_1"],
            build_receipt_hash="build_receipt_1",
        )

    assert error.value.code == "graph_source_changed"
    component = manager.get_generation(grant.target_generation_id).manifest["components"][
        "public_graph"
    ]
    assert component["state"] == "stale"
    assert component["graph_resource_ids"] == []


def test_graph_reader_lease_stays_on_request_generation_after_a_later_release() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    documents_metadata.create_all(engine)
    source = _source(engine)
    manager = GenerationManager()
    coordinator = GraphComponentCoordinator(manager, source, consumer_id="indexer_1")
    receipt = _reserve_and_activate_graph(coordinator, manager, source)
    request_lease = manager.acquire_reference_lease()
    manager.create_staging((), generation_id="generation_new")
    manager.release("generation_new")

    graph_lease = coordinator.acquire_current_reader_lease(
        generation_id=request_lease.generation_id
    )

    assert receipt.target_generation_id == request_lease.generation_id
    assert graph_lease.generation_id == request_lease.generation_id
    coordinator.release_reader_lease(graph_lease)
    manager.release_reference_lease(request_lease.lease_id)


def test_graph_reserve_uses_documents_base_snapshot() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    documents_metadata.create_all(engine)
    indexing_metadata.create_all(engine)
    with engine.begin() as connection:
        publication = _insert_active_document(connection)
    source = _source(engine)
    snapshot = _source_snapshot(source, publications=[publication])
    repository = SqlAlchemyIndexingRepository(engine)
    manager = SqlAlchemyGenerationManager(repository)
    coordinator = GraphComponentCoordinator(manager, source, consumer_id="indexer_1")

    grant = coordinator.reserve_graph_component_stage(
        graph_build_id="graph_1",
        expected_source_revision=snapshot.source_revision,
        expected_active_generation_id=manager.active_generation_id,
        operation_id="stage_1",
        component_input=_graph_input(
            snapshot, source, operation_id="stage_1", target_generation_id="generation_graph"
        ),
    )

    base_snapshot = manager.get_generation(grant.target_generation_id).manifest["base_snapshot"]
    assert base_snapshot == [
        {
            "document_id": "document_1",
            "document_version_id": "version_1",
            "publication_id": "publication_1",
            "space_id": "public",
            "manifest": {
                "content_manifest_id": "manifest_1",
                "content_manifest_hash": publication["content_manifest_hash"],
                "processing_profile_version": "profile_1",
                "processing_config_snapshot": {},
            },
            "object_key": "documents/document_1/original",
            "media_kind": "text/markdown",
        }
    ]
