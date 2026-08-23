from __future__ import annotations

from datetime import UTC, datetime
from hashlib import sha256
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, delete, select

from app.documents.schema import (
    document_versions_table,
    documents_metadata,
    documents_table,
    publications_table,
)
from app.indexing import (
    GenerationManager,
    GraphComponentCoordinator,
    IndexChunk,
    RetrievalProfile,
    RetrievalReleaseService,
    SqlAlchemyIndexingRepository,
    indexing_metadata,
)
from app.indexing.schema import (
    index_chunks_table,
    index_operations_table,
    retrieval_releases_table,
)
from app.platform.errors import PlatformError


def _engine():
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    documents_metadata.create_all(engine)
    indexing_metadata.create_all(engine)
    return engine


def _metrics() -> dict[str, float]:
    return {
        "p50_ms": 1,
        "p95_ms": 2,
        "p99_ms": 3,
        "error_rate": 0,
        "vram_mb": 1,
        "hit_at_k": 0.9,
        "mrr": 0.9,
        "ndcg": 0.9,
        "refusal": 1.0,
    }


def _acceptance_suite() -> dict[str, object]:
    return {
        "acl_assertions": {"space_isolation": "passed"},
        "hardware_profile": {"accelerator": "test"},
        "thresholds": {"p50_ms": 10, "p95_ms": 20, "p99_ms": 30, "error_rate": 1, "vram_mb": 10},
        "samples": {
            name: [{"sample_id": f"{name}-1", "input": name, "expected": "pass"}]
            for name in (
                "phrase_query",
                "proper_noun_query",
                "quoted_exact_query",
                "real_question",
                "acl_filter",
                "sparse_exact_hit",
                "refusal",
            )
        },
        "quality_thresholds": {"hit_at_k": 0.8, "mrr": 0.8, "ndcg": 0.8, "refusal": 0.9},
    }


def _insert_active_publication(engine) -> str:
    content = b"durable release source"
    content_hash = sha256(content).hexdigest()
    now = datetime(2026, 8, 12, tzinfo=UTC)
    with engine.begin() as connection:
        connection.execute(
            documents_table.insert().values(
                id="document_1",
                space_id="space_1",
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
                size_bytes=len(content),
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
                resource_manifest_json={"content_manifest_hash": content_hash},
                created_at_utc=now,
                activated_at_utc=now,
                superseded_at_utc=None,
                discarded_at_utc=None,
            )
        )
    return content_hash


def test_retrieval_release_rejects_an_incomplete_frozen_chinese_suite() -> None:
    engine = _engine()
    SqlAlchemyIndexingRepository(engine).active_generation_id()
    releases = RetrievalReleaseService(engine)
    suite = _acceptance_suite()
    suite["samples"]["phrase_query"] = []

    with pytest.raises(PlatformError) as error:
        releases.stage(
            generation_id="generation_initial",
            profile=RetrievalProfile(),
            acceptance_suite=suite,
        )

    assert error.value.code == "validation_error"


def test_generation_release_rejects_a_missing_active_publication_chunk() -> None:
    engine = _engine()
    content_hash = _insert_active_publication(engine)
    repository = SqlAlchemyIndexingRepository(engine)

    def build(generation, source, connection) -> None:
        chunk = IndexChunk(
            chunk_id="chunk_1",
            generation_id=generation.generation_id,
            publication_id=str(source["publication_id"]),
            document_id=str(source["document_id"]),
            document_version_id=str(source["document_version_id"]),
            space_id=str(source["space_id"]),
            text="indexed content",
            embedding_text="indexed content",
            locator={},
            snippet="indexed content",
            media_kind="text/markdown",
            manifest_hash=content_hash,
        )
        repository.record_published_chunks(
            SimpleNamespace(
                expected_generation_id=generation.generation_id,
                publication_id=source["publication_id"],
            ),
            [chunk],
            connection=connection,
        )

    repository.set_generation_builder(build)
    generation = repository.create_staging(generation_id="generation_missing_chunk")
    with engine.begin() as connection:
        connection.execute(
            delete(index_chunks_table).where(
                index_chunks_table.c.generation_id == generation.generation_id
            )
        )

    with pytest.raises(PlatformError) as error:
        repository.release(generation.generation_id)

    assert error.value.code == "release_gate_failed"


def test_generation_release_rejects_mismatched_dense_manifest_shape() -> None:
    repository = SqlAlchemyIndexingRepository(
        _engine(),
        generation_configuration={
            "embedding_model": "model-a",
            "embedding_dimension": 768,
            "embedding_metric": "cosine",
        },
    )
    generation = repository.create_staging([], generation_id="generation_bad_dense")
    repository.set_component_state(
        generation.generation_id,
        "dense",
        "ready",
        manifest={
            "embedding_model": "model-a",
            "embedding_dimension": 1024,
            "embedding_metric": "cosine",
        },
    )

    with pytest.raises(PlatformError) as error:
        repository.release(generation.generation_id)

    assert error.value.code == "release_gate_failed"


def test_generation_activation_requires_a_released_matching_retrieval_profile() -> None:
    engine = _engine()
    repository = SqlAlchemyIndexingRepository(engine)
    releases = RetrievalReleaseService(engine)
    repository.set_retrieval_release_gate(releases.is_released_for_generation)
    blocked = repository.create_staging([], generation_id="generation_without_profile")

    with pytest.raises(PlatformError) as error:
        repository.release(blocked.generation_id)

    assert error.value.code == "release_gate_failed"
    candidate = repository.create_staging([], generation_id="generation_with_profile")
    staged = releases.stage(
        generation_id=candidate.generation_id,
        profile=RetrievalProfile(config_snapshot={"analyzer": "default"}),
        acceptance_suite=_acceptance_suite(),
    )
    releases.release(str(staged["id"]), metrics=_metrics())

    assert repository.release(candidate.generation_id).generation_id == candidate.generation_id


def test_retrieval_release_freezes_metrics_and_rejects_changed_component_binding() -> None:
    engine = _engine()
    repository = SqlAlchemyIndexingRepository(engine)
    releases = RetrievalReleaseService(engine)
    candidate = repository.create_staging([], generation_id="generation_with_evidence")
    staged = releases.stage(
        generation_id=candidate.generation_id,
        profile=RetrievalProfile(config_snapshot={"analyzer": "default"}),
        acceptance_suite=_acceptance_suite(),
    )
    releases.release(str(staged["id"]), metrics=_metrics())
    with engine.connect() as connection:
        evidence = dict(
            connection.execute(
                select(retrieval_releases_table.c.acceptance_suite_json).where(
                    retrieval_releases_table.c.id == staged["id"]
                )
            ).scalar_one()
        )
    assert evidence["results"]["metrics"] == _metrics()
    assert evidence["component_manifest_hash"]
    assert evidence["generation_config_hash"]
    assert evidence["profile_config_hash"]

    repository.set_component_state(candidate.generation_id, "tree", "ready")
    with engine.begin() as connection:
        assert releases.is_released_for_generation(candidate.generation_id, connection) is False


def test_rollback_requires_matching_candidate_revision_and_source_receipt() -> None:
    engine = _engine()
    repository = SqlAlchemyIndexingRepository(engine)
    candidate = repository.create_staging([], generation_id="generation_next")
    repository.release(candidate.generation_id)

    with pytest.raises(PlatformError) as error:
        repository.rollback(
            "generation_initial",
            source_receipt={
                "state": "held",
                "candidate_generation_id": "another_generation",
                "applied_revision": 0,
            },
        )

    assert error.value.code == "rollback_not_eligible"


def test_gc_operation_identity_includes_reconciliation_run() -> None:
    engine = _engine()
    repository = SqlAlchemyIndexingRepository(engine)
    repository.request_index_generation_gc(
        "generation_initial", reconciliation_run_id="reconcile_1", operation_id="gc_1"
    )
    with engine.connect() as connection:
        payload = dict(
            connection.execute(
                select(index_operations_table.c.response_json).where(
                    index_operations_table.c.operation_id == "gc_1"
                )
            ).scalar_one()
        )
    assert payload["reconciliation_run_id"] == "reconcile_1"

    with pytest.raises(PlatformError) as error:
        repository.request_index_generation_gc(
            "generation_initial", reconciliation_run_id="reconcile_2", operation_id="gc_1"
        )

    assert error.value.code == "idempotency_key_conflict"


def test_graph_coordinator_restricts_ops_rollback_and_retention_gc_entrypoints() -> None:
    coordinator = GraphComponentCoordinator(GenerationManager(), object())

    with pytest.raises(PlatformError) as rollback_error:
        coordinator.rollback_generation(
            candidate_generation_id="generation_1",
            source_receipt={"source": "held"},
            operation_id="rollback_1",
            caller_principal="user_1",
        )
    with pytest.raises(PlatformError) as gc_error:
        coordinator.request_index_generation_gc(
            candidate_generation_id="generation_1",
            reconciliation_run_id="reconcile_1",
            operation_id="gc_1",
            caller_principal="user_1",
        )
    with pytest.raises(PlatformError) as completion_error:
        coordinator.complete_index_generation_gc(
            candidate_generation_id="generation_1",
            operation_id="gc_1",
            caller_principal="user_1",
        )

    assert rollback_error.value.code == "forbidden"
    assert gc_error.value.code == "forbidden"
    assert completion_error.value.code == "forbidden"
