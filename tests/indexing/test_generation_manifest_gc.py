from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime, timedelta

from sqlalchemy import create_engine, select, update

from app.documents.schema import documents_metadata
from app.indexing import (
    GenerationManager,
    SqlAlchemyIndexingRepository,
    indexing_metadata,
)
from app.indexing.schema import index_generations_table, index_operations_table

_REQUIRED_MANIFEST_KEYS = {
    "generation_id",
    "provider",
    "engine_revision",
    "analyzer_revision",
    "tokenizer_revision",
    "sparse_schema_hash",
    "implementation_config_hash",
    "dimension",
    "metric",
    "model_revision",
    "base_revision",
    "applied_revision",
    "last_applied_index_change_id",
    "created_at",
    "published_at",
    "rollback_candidate_until",
    "gc_state",
}


def _assert_complete_manifest(manifest: dict[str, object], generation_id: str) -> None:
    assert _REQUIRED_MANIFEST_KEYS <= manifest.keys()
    assert manifest["generation_id"] == generation_id
    assert manifest["created_at"]
    components = manifest["components"]
    assert isinstance(components, dict)
    assert {"dense", "sparse", "hierarchy", "public_graph"} <= components.keys()
    for name in ("dense", "sparse", "hierarchy"):
        component = components[name]
        assert isinstance(component, dict)
        assert component["component_generation_id"]
        assert component["component_manifest_revision"]
        assert component["reader_lease_binding"] == generation_id
    graph = components["public_graph"]
    assert graph == {
        "graph_generation_id": None,
        "state": "disabled",
        "source_revision": None,
        "source_head_fence": None,
        "component_manifest_revision": "public-graph-v1",
        "reader_lease_binding": generation_id,
    }


def test_sql_and_memory_generation_manifests_are_complete() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    documents_metadata.create_all(engine)
    indexing_metadata.create_all(engine)
    repository = SqlAlchemyIndexingRepository(engine)

    initial = repository.get_generation(repository.active_generation_id())
    staging = repository.create_staging([], generation_id="generation_next")
    _assert_complete_manifest(dict(initial.manifest), initial.generation_id)
    _assert_complete_manifest(dict(staging.manifest), staging.generation_id)

    manager = GenerationManager()
    memory_initial = manager.get_generation(manager.active_generation_id)
    memory_staging = manager.create_staging([], generation_id="generation_memory_next")
    _assert_complete_manifest(dict(memory_initial.manifest), memory_initial.generation_id)
    _assert_complete_manifest(dict(memory_staging.manifest), memory_staging.generation_id)


def test_generation_gc_retries_only_the_failed_component() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    documents_metadata.create_all(engine)
    indexing_metadata.create_all(engine)
    repository = SqlAlchemyIndexingRepository(engine)
    first = repository.create_staging([], generation_id="generation_first")
    repository.release(first.generation_id)
    second = repository.create_staging([], generation_id="generation_second")
    repository.release(second.generation_id)

    calls: Counter[str] = Counter()

    def purge_sparse(generation_id: str, publications: object) -> None:
        del generation_id, publications
        calls["sparse"] += 1
        if calls["sparse"] == 1:
            raise RuntimeError("temporary sparse cleanup failure")

    def purge_vector(generation_id: str, publications: object) -> None:
        del generation_id, publications
        calls["vector"] += 1

    def purge_cache(generation_id: str, publications: object) -> None:
        del generation_id, publications
        calls["cache"] += 1

    repository.set_generation_component_purge("sparse", purge_sparse)
    repository.set_generation_component_purge("vector", purge_vector)
    repository.set_generation_component_purge("cache", purge_cache)
    repository.request_index_generation_gc(
        "generation_initial", reconciliation_run_id="reconcile_1", operation_id="gc_1"
    )

    first_attempt = repository.complete_generation_gc("generation_initial", operation_id="gc_1")
    with engine.connect() as connection:
        first_progress = dict(
            connection.execute(
                select(index_operations_table.c.response_json).where(
                    index_operations_table.c.operation_id == "gc_1"
                )
            ).scalar_one()
        )["component_progress"]

    assert first_attempt.state == "blocked"
    assert first_attempt.blocking_reasons == ("sparse_cleanup_failed",)
    assert first_progress["public_graph"]["state"] == "completed"
    assert first_progress["sparse"] == {
        "state": "failed",
        "attempts": 1,
        "last_error": "cleanup_failed",
    }
    assert first_progress["vector"]["state"] == "pending"
    assert calls == Counter({"sparse": 1})

    completed = repository.complete_generation_gc("generation_initial", operation_id="gc_1")
    with engine.connect() as connection:
        final_progress = dict(
            connection.execute(
                select(index_operations_table.c.response_json).where(
                    index_operations_table.c.operation_id == "gc_1"
                )
            ).scalar_one()
        )["component_progress"]

    assert completed.state == "already_purged"
    assert {value["state"] for value in final_progress.values()} == {"completed"}
    assert calls == Counter({"sparse": 2, "vector": 1, "cache": 1})


# ---------------------------------------------------------------------------
# A62: 回滚窗口过窗后 GC 不再被回滚候选身份阻塞
# ---------------------------------------------------------------------------


def test_rollback_candidate_blocks_gc_only_inside_window() -> None:
    clock = {"now": datetime(2026, 1, 1, 12, 0, tzinfo=UTC)}
    manager = GenerationManager(now=lambda: clock["now"])
    staging = manager.create_staging([])
    manager.release(staging.generation_id)

    in_window = manager.request_index_generation_gc(
        "generation_initial", reconciliation_run_id="run_1", operation_id="gc_in_window"
    )
    assert in_window.state == "blocked"
    assert in_window.blocking_reasons == ("rollback_candidate",)

    clock["now"] = clock["now"] + timedelta(days=8)
    after_window = manager.request_index_generation_gc(
        "generation_initial", reconciliation_run_id="run_1", operation_id="gc_after_window"
    )
    assert after_window.state == "accepted"


def test_sql_gc_blocking_respects_rollback_window() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    documents_metadata.create_all(engine)
    indexing_metadata.create_all(engine)
    repository = SqlAlchemyIndexingRepository(engine)
    first = repository.create_staging([], generation_id="generation_first")
    repository.release(first.generation_id)

    in_window = repository.request_index_generation_gc(
        "generation_initial", reconciliation_run_id="run_1", operation_id="gc_in_window"
    )
    assert in_window.state == "blocked"
    assert "rollback_candidate" in in_window.blocking_reasons

    expired = datetime.now(UTC) - timedelta(days=1)
    with engine.begin() as connection:
        connection.execute(
            update(index_generations_table)
            .where(index_generations_table.c.id == "generation_initial")
            .values(rollback_until_utc=expired)
        )

    after_window = repository.request_index_generation_gc(
        "generation_initial", reconciliation_run_id="run_1", operation_id="gc_after_window"
    )
    assert after_window.state == "accepted"
