from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, select

from app.documents.schema import documents_metadata
from app.indexing import (
    DocumentVisibilityFact,
    GenerationManager,
    IndexChunk,
    InMemorySparseIndexProvider,
    RetrievalProfile,
    RetrievalScope,
    RetrievalService,
    SqlAlchemyIndexingRepository,
    indexing_metadata,
)
from app.indexing.backends import probe_configured_backends
from app.indexing.observability import (
    CANDIDATE_FILTER_ROUTE,
    CANDIDATE_REPLENISH_ROUTE,
    COMPONENT_GC_FAILURE_ROUTE,
    COMPONENT_PUBLISH_FAILURE_ROUTE,
    COMPONENT_ROLLBACK_FAILURE_ROUTE,
    INDEX_INTERNAL_OBSERVABILITY_ROUTES,
    PROVIDER_ANALYZER_PROBE_ROUTE,
)
from app.platform.database import core_metadata, platform_observability_sample_table
from app.platform.errors import PlatformError
from app.platform.observability import SqlAlchemyObservabilityMetrics


class _Metrics:
    def __init__(self) -> None:
        self.samples: list[object] = []

    def record(self, sample: object) -> None:
        self.samples.append(sample)


class _Backend:
    provider_name = "test-provider"

    def __init__(self, error: Exception | None = None) -> None:
        self._error = error

    def probe(self) -> None:
        if self._error is not None:
            raise self._error


def _chunk(chunk_id: str) -> IndexChunk:
    return IndexChunk(
        chunk_id=chunk_id,
        generation_id="generation_initial",
        publication_id="publication_1",
        document_id="document_1",
        document_version_id="version_1",
        space_id="space_1",
        text=f"text {chunk_id}",
        embedding_text=f"text {chunk_id}",
        locator={"chunk": chunk_id},
        snippet=None,
        media_kind="text/plain",
        manifest_hash="manifest_1",
    )


def test_backend_probe_records_success_and_failure_without_changing_fail_closed_behavior() -> None:
    metrics = _Metrics()
    probe_configured_backends(_Backend(), metrics=metrics)

    expected = PlatformError("opensearch_unavailable", "probe failed", {}, 503)
    with pytest.raises(PlatformError) as error:
        probe_configured_backends(_Backend(expected), metrics=metrics)

    assert error.value is expected
    assert [sample.route_template for sample in metrics.samples] == [
        PROVIDER_ANALYZER_PROBE_ROUTE,
        PROVIDER_ANALYZER_PROBE_ROUTE,
    ]
    assert [sample.outcome_class for sample in metrics.samples] == ["success", "server_error"]


def test_visibility_filter_and_cursor_replenishment_record_stable_count_metrics() -> None:
    provider = InMemorySparseIndexProvider()
    provider.stage_chunks(
        "attempt_1",
        "publication_1",
        "document_1",
        "version_1",
        [_chunk("chunk_1"), _chunk("chunk_2")],
    )
    provider.publish_staged("attempt_1", "publication_1")
    metrics = _Metrics()

    def facts(candidate: IndexChunk, principal: object) -> DocumentVisibilityFact:
        del principal
        return DocumentVisibilityFact(
            candidate.document_id,
            candidate.space_id,
            "superseded" if candidate.chunk_id == "chunk_1" else "active",
            candidate.document_version_id,
            candidate.publication_id,
            "active",
            candidate.manifest_hash,
            True,
        )

    service = RetrievalService(
        GenerationManager(),
        [provider],
        identity_access=lambda principal: RetrievalScope(frozenset({"space_1"})),
        visibility_facts=facts,
        exact_match_metrics=metrics,
    )
    result = service.search(
        "text",
        principal="user_1",
        profile=RetrievalProfile(top_k=1, candidate_limit=1),
    )

    assert [hit.chunk.chunk_id for hit in result.hits] == ["chunk_2"]
    counts = Counter((sample.route_template, sample.sample_weight) for sample in metrics.samples)
    assert counts[(CANDIDATE_FILTER_ROUTE, 1.0)] == 1
    assert counts[(CANDIDATE_REPLENISH_ROUTE, 1.0)] == 1


def test_generation_publish_rollback_and_gc_failures_are_durable_observability_samples() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    core_metadata.create_all(engine)
    documents_metadata.create_all(engine)
    indexing_metadata.create_all(engine)
    now = datetime.now(UTC)
    metrics = SqlAlchemyObservabilityMetrics(
        engine,
        now=lambda: now,
        allowed_route_templates=INDEX_INTERNAL_OBSERVABILITY_ROUTES,
    )
    repository = SqlAlchemyIndexingRepository(engine, now=lambda: now, operational_metrics=metrics)
    repository.active_generation_id()

    with pytest.raises(PlatformError):
        repository.release("generation_initial", current_revision=1)
    with pytest.raises(PlatformError):
        repository.rollback("generation_missing", source_receipt={})

    first = repository.create_staging([], generation_id="generation_first")
    repository.release(first.generation_id)
    second = repository.create_staging([], generation_id="generation_second")
    repository.release(second.generation_id)
    repository.set_generation_component_purge(
        "sparse",
        lambda generation_id, publications: (_ for _ in ()).throw(
            RuntimeError("provider detail must not be persisted")
        ),
    )
    repository.request_index_generation_gc(
        "generation_initial", reconciliation_run_id="reconcile_1", operation_id="gc_1"
    )
    gc_result = repository.complete_generation_gc("generation_initial", operation_id="gc_1")

    with engine.connect() as connection:
        samples = (
            connection.execute(select(platform_observability_sample_table.c.route_template))
            .scalars()
            .all()
        )

    assert gc_result.blocking_reasons == ("sparse_cleanup_failed",)
    assert COMPONENT_PUBLISH_FAILURE_ROUTE in samples
    assert COMPONENT_ROLLBACK_FAILURE_ROUTE in samples
    assert COMPONENT_GC_FAILURE_ROUTE in samples
    assert "provider detail" not in repr(samples)
