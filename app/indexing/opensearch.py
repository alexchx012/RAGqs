from __future__ import annotations

import base64
import functools
import json
import ssl
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import quote, urljoin

import httpx

from app.platform.errors import PlatformError

from .models import IndexChunk, ProviderSearchPage
from .providers import StageResult, validate_stage_chunks, validate_stage_identity

OPENSEARCH_ENGINE_REVISION = "opensearch-rest-v1"
OPENSEARCH_ANALYZER_REVISION = "ik-smart-v1"
OPENSEARCH_TOKENIZER_REVISION = "ik-smart-v1"
_OPENSEARCH_ANALYZER = "ik_smart"
_IK_PLUGIN_NAMES = frozenset({"analysis-ik", "analysis-ik-custom"})
_PAGE_SIZE = 1000


def _cursor_encode(offset: int) -> str:
    return base64.urlsafe_b64encode(str(offset).encode("ascii")).decode("ascii")


def _cursor_decode(cursor: str | None) -> int:
    if not cursor:
        return 0
    try:
        return int(base64.urlsafe_b64decode(cursor.encode("ascii")).decode("ascii"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise PlatformError("retrieval_degradation", "provider cursor is invalid", {}, 409) from exc


def _invalidates_index_cache(method):
    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        try:
            return method(self, *args, **kwargs)
        except PlatformError:
            self._index_ready = False
            raise

    return wrapper


class OpenSearchClient(Protocol):
    def health(self) -> Mapping[str, Any]: ...

    def version(self) -> Mapping[str, Any]: ...

    def authentication(self) -> Mapping[str, Any]: ...

    def plugins(self) -> Mapping[str, Any]: ...

    def jvm_stats(self) -> Mapping[str, Any]: ...

    def analyze(self, index: str, analyzer: str, text: str) -> Mapping[str, Any]: ...

    def has_index(self, name: str) -> bool: ...

    def ensure_index(self, name: str) -> None: ...

    def bulk(
        self, index: str, operations: Sequence[tuple[str, str, Mapping[str, Any] | None]]
    ) -> None: ...

    def delete_by_query(self, index: str, query: Mapping[str, Any]) -> int: ...

    def search(
        self,
        index: str,
        *,
        query: Mapping[str, Any],
        filters: Sequence[Mapping[str, Any]],
        offset: int,
        limit: int,
    ) -> tuple[tuple[Mapping[str, Any], float], ...]: ...


class HttpOpenSearchClient:
    """Small OpenSearch REST client; TLS and credentials are startup-only."""

    def __init__(
        self,
        url: str,
        *,
        username: str,
        password: str,
        ca_path: str,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 30.0,
    ) -> None:
        if not url.strip():
            raise PlatformError("sparse_config_invalid", "OpenSearch url is required", {}, 422)
        parsed = httpx.URL(url)
        if parsed.scheme != "https" or not parsed.host:
            raise PlatformError(
                "sparse_config_invalid", "OpenSearch requires an HTTPS endpoint", {}, 422
            )
        if not username.strip() or not password.strip():
            raise PlatformError(
                "sparse_config_invalid", "OpenSearch credentials are required", {}, 422
            )
        ca_file = Path(ca_path)
        if not ca_file.is_file():
            raise PlatformError(
                "sparse_config_invalid", "OpenSearch TLS CA bundle is required", {}, 422
            )
        try:
            context = ssl.create_default_context(cafile=str(ca_file))
        except (OSError, ssl.SSLError) as exc:
            raise PlatformError(
                "sparse_config_invalid", "OpenSearch TLS CA bundle is invalid", {}, 422
            ) from exc
        self._base = url.rstrip("/") + "/"
        self._username = username
        self._password = password
        self._timeout = timeout
        self._transport = transport
        self._verify = context

    def _request(
        self,
        method: str,
        path: str,
        *,
        payload: Mapping[str, Any] | None = None,
        ndjson: str | None = None,
    ) -> Any:
        url = urljoin(self._base, path.lstrip("/"))
        headers = {"Accept": "application/json"}
        content: str | None = None
        if ndjson is not None:
            headers["Content-Type"] = "application/x-ndjson"
            content = ndjson
        try:
            with httpx.Client(
                timeout=self._timeout,
                transport=self._transport,
                verify=self._verify,
                auth=(self._username, self._password),
            ) as client:
                response = client.request(
                    method, url, headers=headers, json=payload, content=content
                )
        except ssl.SSLError as exc:
            raise PlatformError(
                "opensearch_tls_failed", "OpenSearch TLS verification failed", {}, 503
            ) from exc
        except httpx.HTTPError as exc:
            raise PlatformError(
                "opensearch_unavailable", "OpenSearch request failed", {}, 503
            ) from exc
        if response.status_code == 404:
            return None
        if response.status_code in {401, 403}:
            raise PlatformError(
                "opensearch_auth_failed", "OpenSearch authentication failed", {}, 503
            )
        if response.status_code >= 400:
            raise PlatformError("opensearch_unavailable", "OpenSearch request failed", {}, 503)
        if not response.content:
            return {}
        try:
            return response.json()
        except ValueError as exc:
            raise PlatformError(
                "opensearch_unavailable", "OpenSearch response is invalid", {}, 503
            ) from exc

    def health(self) -> Mapping[str, Any]:
        body = self._request("GET", "/_cluster/health")
        return dict(body or {})

    def version(self) -> Mapping[str, Any]:
        body = self._request("GET", "/")
        return dict(body or {})

    def authentication(self) -> Mapping[str, Any]:
        body = self._request("GET", "/_plugins/_security/authinfo")
        if not isinstance(body, Mapping) or not str(body.get("user_name", "")).strip():
            raise PlatformError(
                "opensearch_auth_failed", "OpenSearch authentication probe failed", {}, 503
            )
        return dict(body)

    def plugins(self) -> Mapping[str, Any]:
        body = self._request("GET", "/_nodes/_local?plugins=true")
        return dict(body or {})

    def jvm_stats(self) -> Mapping[str, Any]:
        body = self._request("GET", "/_nodes/_local/stats/jvm")
        return dict(body or {})

    def analyze(self, index: str, analyzer: str, text: str) -> Mapping[str, Any]:
        body = self._request(
            "POST",
            f"/{quote(index, safe='')}/_analyze",
            payload={"analyzer": analyzer, "text": text},
        )
        return dict(body or {})

    def has_index(self, name: str) -> bool:
        response = self._request("HEAD", f"/{quote(name, safe='')}")
        return response is not None

    def ensure_index(self, name: str) -> None:
        self._request(
            "PUT",
            f"/{quote(name, safe='')}",
            payload={
                "mappings": {
                    "dynamic": "false",
                    "properties": {
                        "generation_id": {"type": "keyword"},
                        "document_id": {"type": "keyword"},
                        "document_version_id": {"type": "keyword"},
                        "publication_id": {"type": "keyword"},
                        "space_id": {"type": "keyword"},
                        "chunk_id": {"type": "keyword"},
                        "attempt_id": {"type": "keyword"},
                        "status": {"type": "keyword"},
                        "content_hash": {"type": "keyword"},
                        "fencing_token": {"type": "long"},
                        "text": {
                            "type": "text",
                            "analyzer": _OPENSEARCH_ANALYZER,
                            "search_analyzer": _OPENSEARCH_ANALYZER,
                        },
                        "payload": {"type": "keyword", "index": False},
                    },
                }
            },
        )

    def bulk(
        self, index: str, operations: Sequence[tuple[str, str, Mapping[str, Any] | None]]
    ) -> None:
        lines: list[str] = []
        for action, identifier, document in operations:
            lines.append(json.dumps({action: {"_id": identifier}}, separators=(",", ":")))
            if document is not None:
                lines.append(json.dumps(document, ensure_ascii=True, separators=(",", ":")))
        body = self._request(
            "POST",
            f"/{quote(index, safe='')}/_bulk?refresh=true",
            ndjson="".join(f"{line}\n" for line in lines),
        )
        items = dict(body or {}).get("items") or ()
        failed = len(items) != len(operations) or any(
            int(item.get(action, {}).get("status", 500)) >= 400
            for item, (action, _identifier, _document) in zip(items, operations, strict=True)
        )
        if failed:
            raise PlatformError("opensearch_unavailable", "OpenSearch bulk write failed", {}, 503)

    def delete_by_query(self, index: str, query: Mapping[str, Any]) -> int:
        body = self._request(
            "POST",
            f"/{quote(index, safe='')}/_delete_by_query?refresh=true",
            payload={"query": query},
        )
        return int(dict(body or {}).get("deleted", 0))

    def search(
        self,
        index: str,
        *,
        query: Mapping[str, Any],
        filters: Sequence[Mapping[str, Any]],
        offset: int,
        limit: int,
    ) -> tuple[tuple[Mapping[str, Any], float], ...]:
        body = self._request(
            "POST",
            f"/{quote(index, safe='')}/_search",
            payload={
                "from": offset,
                "size": limit,
                "query": {"bool": {"filter": list(filters), "must": [query]}},
                "sort": [{"_score": "desc"}, {"chunk_id": "asc"}],
            },
        )
        hits: list[tuple[Mapping[str, Any], float]] = []
        for item in dict(body or {}).get("hits", {}).get("hits", []):
            source = item.get("_source")
            if isinstance(source, Mapping):
                try:
                    score = float(item.get("_score", 0.0))
                except (TypeError, ValueError):
                    score = 0.0
                hits.append((source, score))
        return tuple(hits)


def _document(
    *,
    status: str,
    attempt_id: str,
    chunk: IndexChunk,
    content_hash: str,
    fencing_token: int,
) -> dict[str, Any]:
    identifier = (
        f"{status}:{attempt_id}:{chunk.publication_id}:{chunk.chunk_id}"
        if status == "staged"
        else f"published:{chunk.generation_id}:{chunk.publication_id}:{chunk.chunk_id}"
    )
    return {
        "id": identifier,
        "generation_id": chunk.generation_id,
        "document_id": chunk.document_id,
        "document_version_id": chunk.document_version_id,
        "publication_id": chunk.publication_id,
        "space_id": chunk.space_id,
        "chunk_id": chunk.chunk_id,
        "attempt_id": attempt_id,
        "status": status,
        "content_hash": content_hash,
        "fencing_token": fencing_token,
        "text": chunk.sparse_text or chunk.text,
        "payload": json.dumps(chunk.to_mapping(), ensure_ascii=True, separators=(",", ":")),
    }


def _terms(name: str, values: Sequence[str]) -> dict[str, Any]:
    return {"terms": {name: sorted({str(value) for value in values})}}


def _chunk(document: Mapping[str, Any]) -> IndexChunk:
    try:
        payload = json.loads(str(document.get("payload", "{}")))
    except ValueError as exc:
        raise PlatformError(
            "opensearch_unavailable", "OpenSearch indexed chunk is invalid", {}, 503
        ) from exc
    return IndexChunk.from_mapping(payload)


def _result_from_docs(
    state: str,
    attempt_id: str,
    publication_id: str,
    documents: Sequence[Mapping[str, Any]],
) -> StageResult:
    chunks = tuple(_chunk(document) for document in documents)
    return StageResult(
        state,
        attempt_id,
        publication_id,
        str(documents[0].get("generation_id") or chunks[0].generation_id),
        tuple(f"{attempt_id}:{publication_id}:{chunk.chunk_id}" for chunk in chunks),
        str(documents[0].get("content_hash") or ""),
        int(documents[0].get("fencing_token") or 1),
    )


class OpenSearchSparseIndexProvider:
    """OpenSearch + IK implementation of the sparse provider contract."""

    provider_name = "opensearch"
    backend_kind = "sparse"
    engine_revision = OPENSEARCH_ENGINE_REVISION
    analyzer_revision = OPENSEARCH_ANALYZER_REVISION
    tokenizer_revision = OPENSEARCH_TOKENIZER_REVISION

    def __init__(
        self,
        client: OpenSearchClient,
        *,
        index_name: str = "ragqs_chunks",
        allow_create_index: bool = False,
        jvm_heap_min_bytes: int = 1024**3,
    ) -> None:
        self._client = client
        self._index = index_name
        self._allow_create = allow_create_index
        self._heap_min = jvm_heap_min_bytes
        self._index_ready = False

    def manifest_facts(self) -> dict[str, str]:
        return {
            "engine_revision": self.engine_revision,
            "analyzer_revision": self.analyzer_revision,
            "tokenizer_revision": self.tokenizer_revision,
        }

    def probe(self) -> None:
        health = self._client.health()
        if str(health.get("status", "red")).casefold() == "red":
            raise PlatformError(
                "opensearch_unavailable", "OpenSearch cluster is unhealthy", {}, 503
            )
        version = self._client.version()
        if not str(version.get("version", {}).get("number", "")):
            raise PlatformError(
                "opensearch_unavailable", "OpenSearch version is unavailable", {}, 503
            )
        self._client.authentication()
        info = self._client.plugins()
        nodes = dict(info.get("nodes", {}))
        plugins = {
            str(plugin.get("name", ""))
            for node in nodes.values()
            if isinstance(node, Mapping)
            for plugin in node.get("plugins", ())
            if isinstance(plugin, Mapping)
        }
        if not plugins & _IK_PLUGIN_NAMES:
            raise PlatformError(
                "opensearch_ik_missing", "OpenSearch IK analyzer plugin is unavailable", {}, 503
            )
        stats = self._client.jvm_stats()
        heaps = [
            int(node.get("jvm", {}).get("mem", {}).get("heap_max_in_bytes", 0))
            for node in dict(stats.get("nodes", {})).values()
            if isinstance(node, Mapping)
        ]
        if not heaps or min(heaps) < self._heap_min:
            raise PlatformError(
                "opensearch_jvm_heap_below_baseline",
                "OpenSearch JVM heap is below baseline",
                {"minimum_bytes": self._heap_min},
                503,
            )
        self.ensure_index()
        analysis = self._client.analyze(self._index, _OPENSEARCH_ANALYZER, "知识图谱 稀疏索引")
        if not analysis.get("tokens"):
            raise PlatformError(
                "opensearch_analyzer_failed", "OpenSearch Chinese analyzer probe failed", {}, 503
            )

    def ensure_index(self) -> None:
        if self._index_ready:
            return
        if self._client.has_index(self._index):
            self._index_ready = True
            return
        if not self._allow_create:
            raise PlatformError(
                "opensearch_index_missing", "OpenSearch index was not found", {}, 503
            )
        self._client.ensure_index(self._index)
        self._index_ready = True

    def _fetch(self, filters: Sequence[Mapping[str, Any]]) -> tuple[Mapping[str, Any], ...]:
        collected: list[Mapping[str, Any]] = []
        offset = 0
        while True:
            page = self._client.search(
                self._index,
                query={"match_all": {}},
                filters=filters,
                offset=offset,
                limit=_PAGE_SIZE,
            )
            collected.extend(document for document, _score in page)
            if len(page) < _PAGE_SIZE:
                return tuple(collected)
            offset += _PAGE_SIZE

    @_invalidates_index_cache
    def stage_chunks(
        self,
        attempt_id: str,
        publication_id: str,
        document_id: str,
        document_version_id: str,
        chunks: Sequence[IndexChunk | Mapping[str, Any]],
        *,
        fencing_token: int = 1,
        expected_generation_id: str | None = None,
        stage_resource_manifest: Sequence[Mapping[str, Any]] | None = None,
        content_hash: str | None = None,
        usage_context: object | None = None,
    ) -> StageResult:
        del usage_context
        prepared = validate_stage_chunks(
            attempt_id,
            publication_id,
            document_id,
            document_version_id,
            chunks,
            fencing_token=fencing_token,
            expected_generation_id=expected_generation_id,
            stage_resource_manifest=stage_resource_manifest,
            content_hash=content_hash,
        )
        self.ensure_index()
        existing = self._fetch(
            [
                _terms("attempt_id", (attempt_id,)),
                _terms("publication_id", (publication_id,)),
                _terms("status", ("staged",)),
            ]
        )
        if existing:
            result = _result_from_docs("staged", attempt_id, publication_id, existing)
            validate_stage_identity(
                result,
                fencing_token=fencing_token,
                expected_generation_id=expected_generation_id,
                stage_resource_manifest=stage_resource_manifest,
                content_hash=content_hash,
            )
            return result
        documents = [
            _document(
                status="staged",
                attempt_id=attempt_id,
                chunk=chunk,
                content_hash=prepared.content_hash,
                fencing_token=fencing_token,
            )
            for chunk in prepared.chunks
        ]
        self._client.bulk(
            self._index, [("index", document["id"], document) for document in documents]
        )
        return StageResult(
            "staged",
            attempt_id,
            publication_id,
            prepared.generation_id,
            prepared.resource_ids,
            prepared.content_hash,
            fencing_token,
        )

    @_invalidates_index_cache
    def publish_staged(
        self,
        attempt_id: str,
        publication_id: str,
        *,
        validator: Any | None = None,
        fencing_token: int | None = None,
        expected_generation_id: str | None = None,
        stage_resource_manifest: Sequence[Mapping[str, Any]] | None = None,
        content_hash: str | None = None,
    ) -> StageResult:
        self.ensure_index()
        staged = self._fetch(
            [
                _terms("attempt_id", (attempt_id,)),
                _terms("publication_id", (publication_id,)),
                _terms("status", ("staged",)),
            ]
        )
        if not staged:
            published = self._fetch(
                [
                    _terms("attempt_id", (attempt_id,)),
                    _terms("publication_id", (publication_id,)),
                    _terms("status", ("published",)),
                ]
            )
            if published:
                result = _result_from_docs("published", attempt_id, publication_id, published)
                validate_stage_identity(
                    result,
                    fencing_token=fencing_token,
                    expected_generation_id=expected_generation_id,
                    stage_resource_manifest=stage_resource_manifest,
                    content_hash=content_hash,
                )
                return result
            raise PlatformError("index_stage_not_found", "staged index was not found", {}, 404)
        result = _result_from_docs("staged", attempt_id, publication_id, staged)
        validate_stage_identity(
            result,
            fencing_token=fencing_token,
            expected_generation_id=expected_generation_id,
            stage_resource_manifest=stage_resource_manifest,
            content_hash=content_hash,
        )
        chunks = [_chunk(document) for document in staged]
        generation_ids = {chunk.generation_id for chunk in chunks}
        if len(generation_ids) != 1:
            raise PlatformError(
                "processing_receipt_conflict", "staged index content is invalid", {}, 409
            )
        if validator is not None and not validator(chunks):
            raise PlatformError(
                "index_release_blocked", "documents validation rejected publish", {}, 409
            )
        content_hash = result.content_hash
        token = result.fencing_token
        published = [
            _document(
                status="published",
                attempt_id=attempt_id,
                chunk=chunk,
                content_hash=content_hash,
                fencing_token=token,
            )
            for chunk in chunks
        ]
        operations = [("index", document["id"], document) for document in published]
        operations.extend(("delete", str(document.get("id", "")), None) for document in staged)
        self._client.bulk(self._index, operations)
        return StageResult(
            "published",
            attempt_id,
            publication_id,
            next(iter(generation_ids)),
            result.resource_ids,
            content_hash,
            token,
        )

    @_invalidates_index_cache
    def discard_staged(
        self,
        attempt_id: str,
        publication_id: str,
        *,
        fencing_token: int | None = None,
        expected_generation_id: str | None = None,
        stage_resource_manifest: Sequence[Mapping[str, Any]] | None = None,
        content_hash: str | None = None,
    ) -> StageResult:
        self.ensure_index()
        filters = [
            _terms("attempt_id", (attempt_id,)),
            _terms("publication_id", (publication_id,)),
            _terms("status", ("staged", "published")),
        ]
        staged = self._fetch(filters)
        if not staged:
            return StageResult("discarded", attempt_id, publication_id, "", (), "", 1)
        result = _result_from_docs(
            str(staged[0].get("status", "staged")), attempt_id, publication_id, staged
        )
        validate_stage_identity(
            result,
            fencing_token=fencing_token,
            expected_generation_id=expected_generation_id,
            stage_resource_manifest=stage_resource_manifest,
            content_hash=content_hash,
        )
        removed = self._client.delete_by_query(self._index, {"bool": {"filter": filters}})
        if removed != len(staged):
            raise PlatformError(
                "opensearch_unavailable", "OpenSearch staged discard was partial", {}, 503
            )
        return StageResult(
            "discarded",
            result.attempt_id,
            result.publication_id,
            result.generation_id,
            result.resource_ids,
            result.content_hash,
            result.fencing_token,
        )

    @_invalidates_index_cache
    def delete_document_version(
        self, document_id: str, document_version_id: str, *, generation_id: str | None = None
    ) -> int:
        self.ensure_index()
        filters = [
            _terms("document_id", (document_id,)),
            _terms("document_version_id", (document_version_id,)),
            _terms("status", ("published",)),
        ]
        if generation_id:
            filters.append(_terms("generation_id", (generation_id,)))
        return self._client.delete_by_query(self._index, {"bool": {"filter": filters}})

    @_invalidates_index_cache
    def delete_document(self, document_id: str, *, generation_id: str | None = None) -> int:
        self.ensure_index()
        filters = [_terms("document_id", (document_id,)), _terms("status", ("published",))]
        if generation_id:
            filters.append(_terms("generation_id", (generation_id,)))
        return self._client.delete_by_query(self._index, {"bool": {"filter": filters}})

    @_invalidates_index_cache
    def search(
        self,
        query: str,
        space_ids: Sequence[str],
        top_k: int,
        cursor: str | None,
        *,
        generation_id: str | None = None,
    ) -> ProviderSearchPage:
        if top_k < 1:
            raise PlatformError("validation_error", "top_k must be positive", {}, 422)
        if not space_ids:
            return ProviderSearchPage((), None)
        self.ensure_index()
        offset = _cursor_decode(cursor)
        filters = [
            _terms("space_id", tuple(str(item) for item in space_ids)),
            _terms("status", ("published",)),
        ]
        if generation_id:
            filters.append(_terms("generation_id", (generation_id,)))
        normalized_query = query.strip()
        if not normalized_query:
            text_query = {"match_all": {}}
        elif len(normalized_query) >= 2 and (
            (normalized_query[0], normalized_query[-1])
            in {('"', '"'), ("'", "'"), ("“", "”"), ("「", "」"), ("『", "』")}
        ):
            text_query = {
                "match_phrase": {
                    "text": {
                        "query": normalized_query[1:-1],
                        "analyzer": _OPENSEARCH_ANALYZER,
                    }
                }
            }
        else:
            text_query = {
                "match": {
                    "text": {
                        "query": normalized_query,
                        "operator": "and",
                        "analyzer": _OPENSEARCH_ANALYZER,
                    }
                }
            }
        hits = self._client.search(
            self._index,
            query=text_query,
            filters=filters,
            offset=offset,
            limit=top_k + 1,
        )
        page = hits[:top_k]
        items = tuple(
            {
                **_chunk(document).to_mapping(),
                "score": score,
                "publication_id": document.get("publication_id"),
            }
            for document, score in page
        )
        next_cursor = _cursor_encode(offset + len(page)) if len(hits) > top_k else None
        return ProviderSearchPage(items, next_cursor)


__all__ = [
    "HttpOpenSearchClient",
    "OPENSEARCH_ANALYZER_REVISION",
    "OPENSEARCH_ENGINE_REVISION",
    "OPENSEARCH_TOKENIZER_REVISION",
    "OpenSearchSparseIndexProvider",
]
