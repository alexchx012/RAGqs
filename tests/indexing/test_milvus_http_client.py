from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from app.indexing.milvus import HttpMilvusClient
from app.platform.errors import PlatformError


class V2Recorder:
    """Minimal Milvus REST v2 fake: records requests and answers canned responses."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, str, dict[str, Any]]] = []
        self.collections: set[str] = {"existing"}

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8")) if request.content else {}
        path = request.url.path
        self.requests.append((request.method, path, body))
        if path == "/v2/vectordb/collections/describe":
            name = str(body.get("collectionName"))
            if name not in self.collections:
                return httpx.Response(200, json={"code": 100, "message": "can't find collection"})
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "collectionName": name,
                        "fields": [
                            {"fieldName": "id", "type": "VarChar"},
                            {
                                "fieldName": "vector",
                                "type": "FloatVector",
                                "params": [{"key": "dim", "value": "4"}],
                            },
                        ],
                        "indexes": [{"fieldName": "vector", "metricType": "COSINE"}],
                    },
                },
            )
        if path == "/v2/vectordb/collections/list":
            return httpx.Response(200, json={"code": 0, "data": []})
        if path == "/v2/vectordb/collections/create":
            self.collections.add(str(body.get("collectionName")))
            return httpx.Response(200, json={"code": 0, "data": {}})
        if path == "/v2/vectordb/entities/insert":
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {"insertCount": len(body.get("data") or []), "insertIds": []},
                },
            )
        if path == "/v2/vectordb/entities/delete":
            return httpx.Response(200, json={"code": 0, "data": {"deleteCount": 2}})
        if path == "/v2/vectordb/entities/query":
            return httpx.Response(200, json={"code": 0, "data": [{"id": "a"}, {"id": "b"}]})
        if path == "/v2/vectordb/entities/search":
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": [
                        {"id": "a", "distance": 0.9},
                        {"id": "b", "distance": 0.5},
                    ],
                },
            )
        return httpx.Response(404, json={"code": 404, "message": "unknown path"})


def _client(recorder: V2Recorder, **kwargs: Any) -> HttpMilvusClient:
    return HttpMilvusClient(
        "http://milvus.test",
        transport=httpx.MockTransport(recorder.handler),
        **kwargs,
    )


def test_health_uses_v2_collection_list() -> None:
    recorder = V2Recorder()
    _client(recorder).health()
    assert recorder.requests[0] == ("POST", "/v2/vectordb/collections/list", {})


def test_has_collection_describe_and_not_found_is_false() -> None:
    recorder = V2Recorder()
    client = _client(recorder)
    assert client.has_collection("existing") is True
    assert client.has_collection("missing") is False
    paths = [path for _method, path, _body in recorder.requests]
    assert paths == ["/v2/vectordb/collections/describe"] * 2


def test_describe_collection_exposes_dimension_and_metric() -> None:
    recorder = V2Recorder()
    description = _client(recorder).describe_collection("existing")
    vector_field = next(f for f in description["fields"] if f["fieldName"] == "vector")
    assert vector_field["params"] == [{"key": "dim", "value": "4"}]
    assert description["indexes"][0]["metricType"] == "COSINE"


def test_create_collection_posts_v2_schema_and_never_duplicates() -> None:
    recorder = V2Recorder()
    client = _client(recorder, allow_create=True)
    client.create_collection("fresh", dimension=4, metric="cosine")
    method, path, body = recorder.requests[-1]
    assert (method, path) == ("POST", "/v2/vectordb/collections/create")
    assert body["collectionName"] == "fresh"
    assert body["schema"]["fields"][1]["elementTypeParams"] == {"dim": "4"}
    assert body["indexParams"][0]["metricType"] == "COSINE"
    # existing collection must not be re-created
    before = len(recorder.requests)
    client.create_collection("existing", dimension=4, metric="cosine")
    assert recorder.requests[-1][1] == "/v2/vectordb/collections/describe"
    assert len(recorder.requests) == before + 1


def test_create_collection_requires_allow_create_when_missing() -> None:
    recorder = V2Recorder()
    with pytest.raises(PlatformError) as error:
        _client(recorder).create_collection("missing", dimension=4, metric="cosine")
    assert error.value.code == "milvus_collection_missing"


def test_entities_use_v2_payloads() -> None:
    recorder = V2Recorder()
    client = _client(recorder)
    client.insert("existing", [{"id": "a", "vector": [0.1, 0.2, 0.3, 0.4]}])
    assert client.delete("existing", 'id == "a"') == 2
    rows = client.query("existing", 'status == "staged"', ("id",), limit=5, offset=1)
    assert tuple(row["id"] for row in rows) == ("a", "b")
    hits = client.search(
        "existing",
        [0.1, 0.2, 0.3, 0.4],
        limit=2,
        offset=0,
        expr='status == "published"',
        metric="cosine",
        output_fields=("id",),
    )
    assert [score for _row, score in hits] == [0.9, 0.5]
    paths = [path for _method, path, body in recorder.requests]
    assert paths == [
        "/v2/vectordb/entities/insert",
        "/v2/vectordb/entities/delete",
        "/v2/vectordb/entities/query",
        "/v2/vectordb/entities/search",
    ]
    _method, _path, query_body = recorder.requests[2]
    assert query_body["collectionName"] == "existing"
    assert query_body["outputFields"] == ["id"]
    assert query_body["limit"] == 5
    assert query_body["offset"] == 1
    _method, _path, search_body = recorder.requests[3]
    assert search_body["searchParams"]["metricType"] == "COSINE"
    assert search_body["filter"] == 'status == "published"'


def test_nonzero_code_raises_milvus_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(200, json={"code": 1100, "message": "rate limited"})

    client = HttpMilvusClient("http://milvus.test", transport=httpx.MockTransport(handler))
    with pytest.raises(PlatformError) as error:
        client.health()
    assert error.value.code == "milvus_unavailable"
    assert error.value.details.get("milvus_code") == 1100


def test_bearer_token_header_when_configured() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"code": 0, "data": []})

    client = HttpMilvusClient(
        "http://milvus.test",
        token="secret-token",
        transport=httpx.MockTransport(handler),
    )
    client.health()
    assert seen[0].headers["authorization"] == "Bearer secret-token"
