from __future__ import annotations

import base64
from collections.abc import Mapping
from typing import Any

import certifi
import httpx
import pytest

from app.indexing import IndexChunk, OpenSearchSparseIndexProvider
from app.indexing.backends import build_configured_sparse_provider
from app.indexing.opensearch import HttpOpenSearchClient
from app.platform.config import load_platform_settings
from app.platform.errors import PlatformError


class _FakeOpenSearchClient:
    def __init__(self) -> None:
        self.documents: dict[str, dict[str, Any]] = {}
        self.bulk_indexes: list[str] = []
        self.last_query: Mapping[str, Any] | None = None
        self.index_created = False
        self.health_status = "green"
        self.plugin_names = {"analysis-ik"}
        self.heap_max_bytes = 2 * 1024**3
        self.analyze_tokens = ({"token": "知识"}, {"token": "图谱"})

    def health(self) -> Mapping[str, Any]:
        return {"status": self.health_status}

    def version(self) -> Mapping[str, Any]:
        return {"version": {"number": "2.19.1"}}

    def authentication(self) -> Mapping[str, Any]:
        return {"user_name": "admin"}

    def plugins(self) -> Mapping[str, Any]:
        return {"nodes": {"node_1": {"plugins": [{"name": name} for name in self.plugin_names]}}}

    def jvm_stats(self) -> Mapping[str, Any]:
        return {"nodes": {"node_1": {"jvm": {"mem": {"heap_max_in_bytes": self.heap_max_bytes}}}}}

    def analyze(self, index: str, analyzer: str, text: str) -> Mapping[str, Any]:
        del index, analyzer, text
        return {"tokens": self.analyze_tokens}

    def has_index(self, name: str) -> bool:
        del name
        return self.index_created

    def ensure_index(self, name: str) -> None:
        del name
        self.index_created = True

    def bulk(self, index: str, operations: list[tuple[str, str, Mapping[str, Any] | None]]) -> None:
        self.bulk_indexes.append(index)
        for action, identifier, document in operations:
            if action == "delete":
                self.documents.pop(identifier, None)
            elif document is not None:
                self.documents[identifier] = dict(document)

    def delete_by_query(self, index: str, query: Mapping[str, Any]) -> int:
        del index
        filters = query["bool"]["filter"]
        selected = list(self.documents.values())
        for condition in filters:
            for name, values in condition["terms"].items():
                selected = [item for item in selected if str(item.get(name)) in set(values)]
        for item in selected:
            self.documents.pop(str(item["id"]), None)
        return len(selected)

    def search(
        self,
        index: str,
        *,
        query: Mapping[str, Any],
        filters: list[Mapping[str, Any]],
        offset: int,
        limit: int,
    ) -> tuple[tuple[Mapping[str, Any], float], ...]:
        del index
        self.last_query = query
        selected = list(self.documents.values())
        for condition in filters:
            for name, values in condition["terms"].items():
                selected = [item for item in selected if str(item.get(name)) in set(values)]
        selected.sort(key=lambda item: str(item.get("chunk_id", "")))
        return tuple((item, 1.0) for item in selected[offset : offset + limit])


def _chunk(chunk_id: str = "chunk_1") -> IndexChunk:
    return IndexChunk(
        chunk_id=chunk_id,
        generation_id="generation_1",
        publication_id="publication_1",
        document_id="document_1",
        document_version_id="version_1",
        space_id="space_1",
        text="知识图谱 稀疏索引",
        embedding_text="知识图谱 稀疏索引",
        locator={},
        snippet="知识图谱",
        media_kind="text/plain",
        manifest_hash="manifest_hash_1",
    )


def _settings(**overrides: str) -> Any:
    env = {
        "RAG_PLATFORM_PROFILE": "development",
        "RAG_DATABASE_URL": "sqlite+pysqlite:///:memory:",
        "RAG_OBJECT_STORAGE_ENDPOINT": "http://127.0.0.1:9000",
        "RAG_OBJECT_STORAGE_BUCKET": "ragqs",
        "RAG_PROVIDER_NAME": "fake",
        "RAG_INDEX_SPARSE_PROVIDER": "opensearch+ik",
        "RAG_INDEX_SPARSE_URL": "https://127.0.0.1:9200",
        "RAG_INDEX_SPARSE_USERNAME": "admin",
        "RAG_INDEX_SPARSE_PASSWORD": "secret",
        "RAG_INDEX_SPARSE_CA_PATH": certifi.where(),
    }
    env.update(overrides)
    return load_platform_settings(env)


def test_opensearch_client_requires_https_credentials_and_ca() -> None:
    kwargs = {
        "username": "admin",
        "password": "secret",
        "ca_path": certifi.where(),
    }
    with pytest.raises(PlatformError) as error:
        HttpOpenSearchClient("http://127.0.0.1:9200", **kwargs)
    assert error.value.code == "sparse_config_invalid"
    with pytest.raises(PlatformError) as error:
        HttpOpenSearchClient(
            "https://127.0.0.1:9200",
            username="admin",
            password="secret",
            ca_path="missing-ca.crt",
        )
    assert error.value.code == "sparse_config_invalid"
    HttpOpenSearchClient("https://127.0.0.1:9200", **kwargs)


def test_http_opensearch_client_sends_authenticated_requests() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        expected = "Basic " + base64.b64encode(b"admin:secret").decode("ascii")
        assert request.headers["Authorization"] == expected
        assert request.url.path == "/_cluster/health"
        return httpx.Response(200, json={"status": "green"})

    client = HttpOpenSearchClient(
        "https://127.0.0.1:9200",
        username="admin",
        password="secret",
        ca_path=certifi.where(),
        transport=httpx.MockTransport(handler),
    )

    assert client.health() == {"status": "green"}


def test_http_opensearch_bulk_targets_the_index_in_the_request_path() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/ragqs_chunks/_bulk"
        assert request.headers["Content-Type"] == "application/x-ndjson"
        lines = request.content.decode("utf-8").splitlines()
        assert lines[0] == '{"index":{"_id":"chunk_1"}}'
        assert lines[2] == '{"delete":{"_id":"chunk_2"}}'
        return httpx.Response(
            200,
            json={"items": [{"index": {"status": 201}}, {"delete": {"status": 200}}]},
        )

    client = HttpOpenSearchClient(
        "https://127.0.0.1:9200",
        username="admin",
        password="secret",
        ca_path=certifi.where(),
        transport=httpx.MockTransport(handler),
    )
    client.bulk(
        "ragqs_chunks",
        [("index", "chunk_1", {"text": "知识图谱"}), ("delete", "chunk_2", None)],
    )


def test_opensearch_startup_probe_rejects_each_failed_gate() -> None:
    client = _FakeOpenSearchClient()
    provider = OpenSearchSparseIndexProvider(client, allow_create_index=True)
    provider.probe()

    for attribute, value, code in (
        ("health_status", "red", "opensearch_unavailable"),
        ("plugin_names", set(), "opensearch_ik_missing"),
        ("heap_max_bytes", 512 * 1024**2, "opensearch_jvm_heap_below_baseline"),
        ("analyze_tokens", (), "opensearch_analyzer_failed"),
    ):
        failing_client = _FakeOpenSearchClient()
        setattr(failing_client, attribute, value)
        failing = OpenSearchSparseIndexProvider(failing_client, allow_create_index=True)
        with pytest.raises(PlatformError) as error:
            failing.probe()
        assert error.value.code == code


def test_opensearch_provider_shares_sparse_stage_publish_discard_search_contract() -> None:
    client = _FakeOpenSearchClient()
    provider = OpenSearchSparseIndexProvider(client, allow_create_index=True)
    chunk = _chunk()

    staged = provider.stage_chunks(
        "attempt_1",
        "publication_1",
        "document_1",
        "version_1",
        (chunk,),
        fencing_token=7,
    )
    replay = provider.stage_chunks(
        "attempt_1",
        "publication_1",
        "document_1",
        "version_1",
        (chunk,),
        fencing_token=7,
    )
    assert staged == replay
    assert staged.state == "staged"
    assert staged.fencing_token == 7

    published = provider.publish_staged("attempt_1", "publication_1", fencing_token=7)
    republished = provider.publish_staged("attempt_1", "publication_1", fencing_token=7)
    assert published == republished
    assert published.state == "published"
    assert not any(item["status"] == "staged" for item in client.documents.values())

    page = provider.search("知识图谱", ("space_1",), 10, None, generation_id="generation_1")
    assert page.cursor is None
    assert page.items[0]["chunk_id"] == "chunk_1"
    assert page.items[0]["score"] == 1.0
    assert client.bulk_indexes == ["ragqs_chunks", "ragqs_chunks"]

    provider.search("“知识图谱”", ("space_1",), 10, None, generation_id="generation_1")
    assert client.last_query == {
        "match_phrase": {"text": {"query": "知识图谱", "analyzer": "ik_smart"}}
    }

    discarded = provider.discard_staged("attempt_1", "publication_1", fencing_token=7)
    assert discarded.state == "discarded"
    assert not client.documents


def test_build_configured_sparse_provider_constructs_opensearch_profile() -> None:
    provider = build_configured_sparse_provider(_settings(), allow_create=True)
    assert isinstance(provider, OpenSearchSparseIndexProvider)
    assert provider.manifest_facts() == {
        "engine_revision": "opensearch-rest-v1",
        "analyzer_revision": "ik-smart-v1",
        "tokenizer_revision": "ik-smart-v1",
    }


def test_build_configured_sparse_provider_requires_complete_opensearch_config() -> None:
    with pytest.raises(RuntimeError, match="OpenSearch requires"):
        build_configured_sparse_provider(
            _settings(**{"RAG_INDEX_SPARSE_PASSWORD": ""}), allow_create=True
        )
