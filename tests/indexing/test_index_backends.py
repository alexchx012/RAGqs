from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from app.indexing.embedding import EmbeddingConfig, InMemoryEmbeddingProvider
from app.indexing.meilisearch import (
    HttpMeilisearchClient,
    MeilisearchSparseIndexProvider,
    pretokens,
    probe_meilisearch_volume,
)
from app.indexing.milvus import HttpMilvusClient, MilvusIndexWriter, milvus_collection_name
from app.indexing.models import IndexChunk
from app.platform.errors import PlatformError


def _chunk(chunk_id: str = "chunk_1") -> IndexChunk:
    return IndexChunk(
        chunk_id=chunk_id,
        generation_id="generation_1",
        publication_id="publication_1",
        document_id="document_1",
        document_version_id="version_1",
        space_id="space_1",
        text=f"中文文本 {chunk_id}",
        embedding_text=f"中文文本 {chunk_id}",
        locator={"section_path": chunk_id},
        snippet=chunk_id,
        media_kind="text/plain",
        manifest_hash="manifest_1",
    )


class FakeMilvus:
    def __init__(self) -> None:
        self.collections: dict[str, dict[str, Any]] = {}
        self.rows: dict[str, list[dict[str, Any]]] = {}
        self.dropped: list[str] = []

    def health(self) -> None:
        return None

    def has_collection(self, name: str) -> bool:
        return name in self.collections

    def describe_collection(self, name: str) -> Mapping[str, Any]:
        return self.collections[name]

    def create_collection(self, name: str, *, dimension: int, metric: str) -> None:
        if name in self.collections:
            raise AssertionError("must not overwrite an existing collection")
        self.collections[name] = {
            "fields": [
                {"fieldName": "vector", "elementTypeParams": {"dim": str(dimension)}},
            ],
            "indexes": [{"metricType": {"cosine": "COSINE", "l2": "L2", "ip": "IP"}[metric]}],
        }
        self.rows[name] = []

    def insert(self, name: str, rows: Sequence[Mapping[str, Any]]) -> None:
        store = self.rows.setdefault(name, [])
        incoming = {str(row["id"]): dict(row) for row in rows}
        self.rows[name] = [row for row in store if str(row["id"]) not in incoming]
        self.rows[name].extend(incoming.values())

    def delete(self, name: str, expr: str) -> int:
        before = list(self.rows.get(name, []))
        kept = [row for row in before if not _milvus_match(row, expr)]
        self.rows[name] = kept
        return len(before) - len(kept)

    def query(
        self, name: str, expr: str, output_fields: Sequence[str]
    ) -> tuple[Mapping[str, Any], ...]:
        del output_fields
        return tuple(row for row in self.rows.get(name, []) if _milvus_match(row, expr))

    def search(
        self,
        name: str,
        vector: Sequence[float],
        *,
        limit: int,
        offset: int,
        expr: str,
        metric: str,
        output_fields: Sequence[str],
    ) -> tuple[tuple[Mapping[str, Any], float], ...]:
        del vector, metric, output_fields
        matched = [row for row in self.rows.get(name, []) if _milvus_match(row, expr)]
        page = matched[offset : offset + limit]
        return tuple((row, 1.0 - index * 0.01) for index, row in enumerate(page))


def _milvus_match(row: Mapping[str, Any], expr: str) -> bool:
    clauses = [item.strip() for item in expr.split("&&")]
    for clause in clauses:
        if " in [" in clause:
            field, raw = clause.split(" in ", 1)
            allowed = {
                item.strip().strip('"') for item in raw.strip("[]").split(",") if item.strip()
            }
            if str(row.get(field.strip())) not in allowed:
                return False
            continue
        field, raw = clause.split("==", 1)
        if str(row.get(field.strip())) != raw.strip().strip('"'):
            return False
    return True


class FakeMeili:
    def __init__(self) -> None:
        self.indexes: dict[str, list[dict[str, Any]]] = {}

    def health(self) -> None:
        return None

    def authorized(self) -> None:
        return None

    def has_index(self, name: str) -> bool:
        return name in self.indexes

    def ensure_index(self, name: str) -> None:
        self.indexes.setdefault(name, [])

    def add_documents(self, name: str, documents: Sequence[Mapping[str, Any]]) -> None:
        store = self.indexes.setdefault(name, [])
        incoming = {str(item["id"]): dict(item) for item in documents}
        self.indexes[name] = [item for item in store if str(item["id"]) not in incoming]
        self.indexes[name].extend(incoming.values())

    def delete_documents(self, name: str, document_ids: Sequence[str]) -> None:
        blocked = set(document_ids)
        self.indexes[name] = [
            item for item in self.indexes.get(name, []) if str(item["id"]) not in blocked
        ]

    def get_documents(
        self, name: str, *, filters: str, limit: int = 1000
    ) -> tuple[Mapping[str, Any], ...]:
        matched = [item for item in self.indexes.get(name, []) if _meili_match(item, filters)]
        return tuple(matched[:limit])

    def search(
        self,
        name: str,
        query: str,
        *,
        filters: str,
        limit: int,
        offset: int,
    ) -> tuple[tuple[Mapping[str, Any], float], ...]:
        matched = [item for item in self.indexes.get(name, []) if _meili_match(item, filters)]
        if query:
            matched.sort(key=lambda item: (0 if query in str(item.get("jieba_tokens", "")) else 1))
        page = matched[offset : offset + limit]
        return tuple((item, 0.9 - index * 0.1) for index, item in enumerate(page))


def _meili_match(row: Mapping[str, Any], filters: str) -> bool:
    groups = [item.strip() for item in filters.split(" AND ")]
    for group in groups:
        if group.startswith("(") and group.endswith(")"):
            options = [item.strip() for item in group[1:-1].split(" OR ")]
            if not any(_meili_clause(row, option) for option in options):
                return False
            continue
        if not _meili_clause(row, group):
            return False
    return True


def _meili_clause(row: Mapping[str, Any], clause: str) -> bool:
    field, raw = clause.split("=", 1)
    return str(row.get(field.strip())) == raw.strip().strip('"')


def _writer(client: FakeMilvus, *, allow_create: bool = True) -> MilvusIndexWriter:
    embedding = InMemoryEmbeddingProvider(
        EmbeddingConfig(
            base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
            api_key="test",
            model="text-embedding-v4",
            revision="text-embedding-v4",
            dimension=4,
            metric="cosine",
        )
    )
    return MilvusIndexWriter(
        client,
        embedding,
        collection_prefix="ragqs",
        allow_create_collection=allow_create,
    )


def test_milvus_collection_name_includes_revision_and_dimension() -> None:
    assert milvus_collection_name("ragqs", "text-embedding-v4", 1024) == (
        "ragqs_text_embedding_v4_1024"
    )


def test_milvus_stage_publish_search_and_idempotent_delete() -> None:
    client = FakeMilvus()
    writer = _writer(client)
    first = writer.stage_chunks(
        "attempt_1",
        "publication_1",
        "document_1",
        "version_1",
        [_chunk()],
    )
    second = writer.stage_chunks(
        "attempt_1",
        "publication_1",
        "document_1",
        "version_1",
        [_chunk()],
    )
    assert first.state == second.state == "staged"
    published = writer.publish_staged("attempt_1", "publication_1")
    assert published.state == "published"
    page = writer.search("中文", ["space_1"], 5, None, generation_id="generation_1")
    assert page.items[0]["publication_id"] == "publication_1"
    assert page.items[0]["generation_id"] == "generation_1"
    assert (
        writer.delete_document_version("document_1", "version_1", generation_id="generation_1") == 1
    )
    empty = writer.search("中文", ["space_1"], 5, None, generation_id="generation_1")
    assert empty.items == ()


def test_milvus_probe_validates_and_never_drops() -> None:
    client = FakeMilvus()
    writer = _writer(client, allow_create=False)
    with pytest.raises(PlatformError) as missing:
        writer.probe()
    assert missing.value.code == "milvus_collection_missing"
    creator = _writer(client, allow_create=True)
    creator.ensure_collection()
    assert creator.collection_name in client.collections
    writer.probe()
    assert client.dropped == []


def test_meilisearch_writes_jieba_field_and_hides_bm25_from_fusion() -> None:
    client = FakeMeili()
    provider = MeilisearchSparseIndexProvider(
        client,
        index_name="ragqs_chunks",
        allow_create_index=True,
        tokenize=lambda text: f"tokenized {text}",
    )
    provider.stage_chunks(
        "attempt_1",
        "publication_1",
        "document_1",
        "version_1",
        [_chunk()],
    )
    provider.publish_staged("attempt_1", "publication_1")
    stored = client.indexes["ragqs_chunks"][0]
    assert stored["jieba_tokens"].startswith("tokenized ")
    page = provider.search("中文文本", ["space_1"], 5, None, generation_id="generation_1")
    assert page.items[0]["publication_id"] == "publication_1"
    assert page.items[0]["score"] == 0.0


def test_meilisearch_volume_probe_requires_mounted_directory(tmp_path: Path) -> None:
    with pytest.raises(PlatformError) as error:
        probe_meilisearch_volume(str(tmp_path / "missing"))
    assert error.value.code == "meilisearch_volume_missing"
    probe_meilisearch_volume(str(tmp_path))


def test_pretokens_uses_jieba_when_available() -> None:
    pytest.importorskip("jieba")
    tokens = pretokens("中文文本")
    assert "中文" in tokens or "文本" in tokens


def _meili_ready(url: str, api_key: str) -> bool:
    try:
        response = __import__("httpx").get(f"{url.rstrip('/')}/health", timeout=2.0)
    except Exception:
        return False
    return response.status_code < 400


def _milvus_ready(uri: str) -> bool:
    try:
        HttpMilvusClient(uri).health()
    except Exception:
        return False
    return True


@pytest.mark.integration
def test_real_meilisearch_stage_publish_search_with_jieba(tmp_path: Path) -> None:
    url = os.environ.get("RAG_INDEX_SPARSE_URL", "http://127.0.0.1:7700")
    api_key = os.environ.get("RAG_INDEX_SPARSE_API_KEY", "ragqs-dev-meili-key")
    if not _meili_ready(url, api_key):
        pytest.skip(f"Meilisearch is not reachable at {url}")
    pytest.importorskip("jieba")
    index_name = "ragqs_live_chunks"
    provider = MeilisearchSparseIndexProvider(
        HttpMeilisearchClient(url, api_key=api_key),
        index_name=index_name,
        data_path=str(tmp_path),
        allow_create_index=True,
    )
    provider.probe()
    provider.stage_chunks(
        "attempt_live",
        "publication_1",
        "document_1",
        "version_1",
        [_chunk("live_1")],
    )
    published = provider.publish_staged("attempt_live", "publication_1")
    assert published.state == "published"
    page = provider.search("中文文本", ["space_1"], 5, None, generation_id="generation_1")
    assert page.items
    assert page.items[0]["publication_id"] == "publication_1"
    assert page.items[0]["score"] == 0.0
    assert provider.delete_document("document_1", generation_id="generation_1") >= 1


@pytest.mark.integration
def test_real_milvus_stage_publish_search() -> None:
    uri = os.environ.get("RAG_INDEX_VECTOR_URI", "http://127.0.0.1:9091")
    token = os.environ.get("RAG_INDEX_VECTOR_TOKEN") or None
    if not _milvus_ready(uri):
        pytest.skip(f"Milvus is not reachable at {uri}")
    writer = MilvusIndexWriter(
        HttpMilvusClient(uri, token=token, allow_create=True),
        InMemoryEmbeddingProvider(
            EmbeddingConfig(
                base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
                api_key="test",
                model="text-embedding-v4",
                revision="live-test",
                dimension=4,
                metric="cosine",
            )
        ),
        collection_prefix="ragqs",
        allow_create_collection=True,
    )
    writer.ensure_collection()
    writer.probe()
    writer.stage_chunks(
        "attempt_live",
        "publication_1",
        "document_1",
        "version_1",
        [_chunk("live_1")],
    )
    published = writer.publish_staged("attempt_live", "publication_1")
    assert published.state == "published"
    page = writer.search("中文文本", ["space_1"], 5, None, generation_id="generation_1")
    assert page.items
    assert page.items[0]["publication_id"] == "publication_1"
    assert writer.delete_document("document_1", generation_id="generation_1") >= 1
