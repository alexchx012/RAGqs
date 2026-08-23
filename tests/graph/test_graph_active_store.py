from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select

from app.graph.schema import graph_entities_table, graph_metadata, graph_relations_table
from app.graph.store import SqlAlchemyPublicGraphStore
from app.platform.errors import PlatformError


def _resource() -> dict[str, object]:
    return {
        "resource_kind": "publication_graph",
        "resource_id": "publication_1",
        "payload": {
            "graph": {
                "source": {"document_id": "document_1", "content_manifest_id": "manifest_1"},
                "nodes": [
                    {
                        "canonical_key": "ragqs",
                        "entity_type": "product",
                        "display_name": "RAGqs",
                        "aliases": ["RAG questions"],
                        "chunk_locator": {"content_manifest_id": "manifest_1"},
                        "extraction_model_revision": "model-v1",
                        "prompt_revision": "prompt-v1",
                        "confidence": 0.99,
                    },
                    {
                        "canonical_key": "retrieval",
                        "entity_type": "capability",
                        "display_name": "Retrieval",
                        "aliases": [],
                        "chunk_locator": {"content_manifest_id": "manifest_1", "chunk": "2"},
                        "extraction_model_revision": "model-v1",
                        "prompt_revision": "prompt-v1",
                        "confidence": 0.95,
                    },
                ],
                "edges": [
                    {
                        "source_key": "ragqs",
                        "target_key": "retrieval",
                        "relation_type": "uses",
                        "directed": True,
                        "properties": {},
                        "chunk_locator": {"content_manifest_id": "manifest_1", "chunk": "1"},
                        "extraction_model_revision": "model-v1",
                        "prompt_revision": "prompt-v1",
                        "confidence": 0.8,
                    }
                ],
            }
        },
    }


def _publication() -> dict[str, str]:
    return {
        "document_id": "document_1",
        "document_version_id": "version_1",
        "publication_id": "publication_1",
        "content_manifest_id": "manifest_1",
        "content_manifest_hash": "hash_1",
    }


def test_active_graph_store_materializes_provenance_queries_and_purges() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    graph_metadata.create_all(engine)
    now = datetime(2026, 8, 23, tzinfo=UTC)
    store = SqlAlchemyPublicGraphStore(engine, now=lambda: now)

    with engine.begin() as connection:
        store.activate(
            connection=connection,
            graph_build_id="graph_build_1",
            graph_generation_id="graph_generation_1",
            index_generation_id="generation_1",
            source_revision=7,
            source_head_fence=9,
            publications=(_publication(),),
            resources=(_resource(),),
        )
        entity = (
            connection.execute(
                select(graph_entities_table).where(graph_entities_table.c.canonical_key == "ragqs")
            )
            .mappings()
            .one()
        )
        relation = connection.execute(select(graph_relations_table)).mappings().one()
    assert entity["space_id"] == "public"
    assert entity["publication_id"] == "publication_1"
    assert entity["canonical_key"] == "ragqs"
    assert relation["source_canonical_key"] == "ragqs"

    lease = SimpleNamespace(
        generation_id="generation_1",
        component_kind="public_graph",
        source_head_fence=9,
        expires_at=now + timedelta(minutes=1),
    )
    candidate = SimpleNamespace(chunk=SimpleNamespace(document_id="document_1"))
    result = store.route("RAGqs", (candidate,), rag_call_limit=1, reader_lease=lease)
    assert result["entities"][0]["canonical_key"] == "ragqs"
    assert "display_name" in result["entities"][0]
    assert "payload" not in result["entities"][0]

    assert store.delete_document_version("generation_1", "document_1", "version_1") == 3
    assert store.purge_generation("generation_1") == 0


def test_active_graph_store_rejects_unprovenanced_provider_output() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    graph_metadata.create_all(engine)
    store = SqlAlchemyPublicGraphStore(engine)
    invalid = _resource()
    graph = invalid["payload"]  # type: ignore[assignment]
    graph["graph"]["nodes"][0].pop("chunk_locator")  # type: ignore[index]
    with pytest.raises(PlatformError) as error, engine.begin() as connection:
        store.activate(
            connection=connection,
            graph_build_id="graph_build_1",
            graph_generation_id="graph_generation_1",
            index_generation_id="generation_1",
            source_revision=7,
            source_head_fence=9,
            publications=(_publication(),),
            resources=(invalid,),
        )
    assert error.value.code == "graph_provider_schema_invalid"


@pytest.mark.parametrize("field", ("chunk_locator", "extraction_model_revision", "prompt_revision"))
def test_active_graph_store_requires_relation_owned_provenance(field: str) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    graph_metadata.create_all(engine)
    store = SqlAlchemyPublicGraphStore(engine)
    invalid = _resource()
    graph = invalid["payload"]  # type: ignore[assignment]
    graph["graph"]["edges"][0].pop(field)  # type: ignore[index]

    with pytest.raises(PlatformError) as error, engine.begin() as connection:
        store.activate(
            connection=connection,
            graph_build_id="graph_build_1",
            graph_generation_id="graph_generation_1",
            index_generation_id="generation_1",
            source_revision=7,
            source_head_fence=9,
            publications=(_publication(),),
            resources=(invalid,),
        )

    assert error.value.code == "graph_provider_schema_invalid"


def test_active_graph_store_rejects_dangling_relation_endpoint() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    graph_metadata.create_all(engine)
    store = SqlAlchemyPublicGraphStore(engine)
    invalid = _resource()
    graph = invalid["payload"]  # type: ignore[assignment]
    graph["graph"]["edges"][0]["target_key"] = "missing"  # type: ignore[index]

    with pytest.raises(PlatformError) as error, engine.begin() as connection:
        store.activate(
            connection=connection,
            graph_build_id="graph_build_1",
            graph_generation_id="graph_generation_1",
            index_generation_id="generation_1",
            source_revision=7,
            source_head_fence=9,
            publications=(_publication(),),
            resources=(invalid,),
        )

    assert error.value.code == "graph_provider_schema_invalid"
