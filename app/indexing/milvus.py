from __future__ import annotations

import base64
import functools
import json
import re
from collections.abc import Mapping, Sequence
from typing import Any, Protocol
from urllib.parse import urljoin

import httpx

from app.platform.errors import PlatformError

from .embedding import EmbeddingConfig, EmbeddingProvider
from .models import IndexChunk, ProviderSearchPage
from .providers import StageResult, validate_stage_chunks, validate_stage_identity

_MILVUS_METRIC = {"cosine": "COSINE", "l2": "L2", "ip": "IP"}
# Server-side query page size; stage/publish reads loop until a short page.
_QUERY_PAGE_SIZE = 16384


def _invalidates_collection_cache(method):
    """Reset the ensure_collection existence flag when a backend call fails.

    The flag only mirrors "the collection exists on the server"; any backend
    error may mean it was dropped externally, so the next call re-probes.
    """

    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        try:
            return method(self, *args, **kwargs)
        except PlatformError:
            self._collection_ready = False
            raise

    return wrapper


def _hit_score(item: Mapping[str, Any], *, metric: str, index: int) -> float:
    """Convert the Milvus search distance to a higher-is-better similarity.

    COSINE/IP return a similarity; values below 0 mean "dissimilar" and clamp
    to 0 so scores stay comparable with score_threshold. L2 returns a distance
    mapped through 1/(1+d).
    """

    raw = item.get("distance")
    if not isinstance(raw, (int, float)) or isinstance(raw, bool):
        return max(0.0, 1.0 - index * 0.01)
    distance = float(raw)
    if metric == "l2":
        return 1.0 / (1.0 + distance) if distance >= 0 else 0.0
    return max(0.0, distance)


_OUTPUT_FIELDS = (
    "id",
    "generation_id",
    "document_id",
    "document_version_id",
    "publication_id",
    "space_id",
    "chunk_id",
    "attempt_id",
    "status",
    "content_hash",
    "fencing_token",
    "payload",
)


def milvus_collection_name(prefix: str, revision: str, dimension: int) -> str:
    safe_prefix = re.sub(r"[^A-Za-z0-9_]", "_", prefix.strip()) or "ragqs"
    safe_revision = re.sub(r"[^A-Za-z0-9_]", "_", revision.strip()) or "model"
    name = f"{safe_prefix}_{safe_revision}_{int(dimension)}"
    if not re.match(r"^[A-Za-z_]", name):
        name = f"c_{name}"
    return name[:255]


def _cursor_encode(offset: int) -> str:
    return base64.urlsafe_b64encode(str(offset).encode("ascii")).decode("ascii")


def _cursor_decode(cursor: str | None) -> int:
    if not cursor:
        return 0
    try:
        return int(base64.urlsafe_b64decode(cursor.encode("ascii")).decode("ascii"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise PlatformError("retrieval_degradation", "provider cursor is invalid", {}, 409) from exc


def _quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


class MilvusClient(Protocol):
    def health(self) -> None: ...

    def has_collection(self, name: str) -> bool: ...

    def describe_collection(self, name: str) -> Mapping[str, Any]: ...

    def create_collection(self, name: str, *, dimension: int, metric: str) -> None: ...

    def insert(self, name: str, rows: Sequence[Mapping[str, Any]]) -> None: ...

    def delete(self, name: str, expr: str) -> int: ...

    def query(
        self,
        name: str,
        expr: str,
        output_fields: Sequence[str],
        *,
        limit: int = _QUERY_PAGE_SIZE,
        offset: int = 0,
    ) -> tuple[Mapping[str, Any], ...]: ...

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
    ) -> tuple[tuple[Mapping[str, Any], float], ...]: ...


class HttpMilvusClient:
    """Milvus v1 HTTP API client. Never drops collections."""

    def __init__(
        self,
        uri: str,
        *,
        token: str | None = None,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 30.0,
        allow_create: bool = False,
    ) -> None:
        if not uri.strip():
            raise PlatformError("vector_config_invalid", "Milvus uri is required", {}, 422)
        self._base = uri.rstrip("/") + "/"
        self._token = token
        self._timeout = timeout
        self._transport = transport
        self._allow_create = allow_create

    def _post(self, path: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        url = urljoin(self._base, path.lstrip("/"))
        try:
            with httpx.Client(timeout=self._timeout, transport=self._transport) as client:
                response = client.post(url, headers=self._headers(), json=dict(payload))
        except httpx.HTTPError as exc:
            raise PlatformError("milvus_unavailable", "Milvus request failed", {}, 503) from exc
        if response.status_code >= 400:
            raise PlatformError("milvus_unavailable", "Milvus request failed", {}, 503)
        try:
            body = response.json()
        except ValueError as exc:
            raise PlatformError(
                "milvus_unavailable", "Milvus response is invalid", {}, 503
            ) from exc
        if not isinstance(body, dict):
            raise PlatformError("milvus_unavailable", "Milvus response is invalid", {}, 503)
        code = body.get("code", 0)
        if code not in {0, "0", None}:
            raise PlatformError(
                "milvus_unavailable",
                str(body.get("message") or "Milvus request failed"),
                {},
                503,
            )
        data = body.get("data", body)
        return data if isinstance(data, dict) else {"value": data}

    def _get(self, path: str) -> Mapping[str, Any]:
        url = urljoin(self._base, path.lstrip("/"))
        try:
            with httpx.Client(timeout=self._timeout, transport=self._transport) as client:
                response = client.get(url, headers=self._headers())
        except httpx.HTTPError as exc:
            raise PlatformError("milvus_unavailable", "Milvus request failed", {}, 503) from exc
        if response.status_code >= 400:
            raise PlatformError("milvus_unavailable", "Milvus request failed", {}, 503)
        try:
            body = response.json()
        except ValueError as exc:
            raise PlatformError(
                "milvus_unavailable", "Milvus response is invalid", {}, 503
            ) from exc
        if not isinstance(body, dict):
            raise PlatformError("milvus_unavailable", "Milvus response is invalid", {}, 503)
        return body

    def _v1_collection_list(self) -> Mapping[str, Any]:
        return self._get("/api/v1/collections")

    def _v1_collection_exists(self, name: str) -> bool:
        try:
            r = self._post("/api/v1/collection/existence", {"collection_name": name})
        except Exception:
            return False
        return bool(r.get("status") or r.get("has") or r.get("exists"))

    def _v1_create_collection(self, name: str, *, dimension: int, metric: str) -> None:
        self._post(
            "/api/v1/collection",
            {
                "collection_name": name,
                "schema": {
                    "autoId": False,
                    "enableDynamicField": False,
                    "fields": [
                        {
                            "fieldName": "id",
                            "dataType": "VarChar",
                            "isPrimary": True,
                            "elementTypeParams": {"max_length": "256"},
                        },
                        {
                            "fieldName": "vector",
                            "dataType": "FloatVector",
                            "elementTypeParams": {"dim": str(dimension)},
                        },
                        *_varchar_fields(),
                    ],
                },
                "index_params": [
                    {
                        "fieldName": "vector",
                        "indexName": "vector_idx",
                        "metricType": _MILVUS_METRIC[metric],
                        "indexType": "AUTOINDEX",
                    }
                ],
            },
        )

    def _v1_insert(self, name: str, rows: Sequence[Mapping[str, Any]]) -> None:
        self._post(
            "/api/v1/entities",
            {"collection_name": name, "operation": "insert", "data": [dict(row) for row in rows]},
        )

    def _v1_delete(self, name: str, expr: str) -> int:
        data = self._post(
            "/api/v1/entities",
            {"collection_name": name, "operation": "delete", "filter": expr},
        )
        deleted = data.get("deleted") or data.get("deleteCount") or 0
        try:
            return int(deleted)
        except (TypeError, ValueError):
            return 0

    def _v1_search(
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
        data = self._post(
            "/api/v1/entities",
            {
                "collection_name": name,
                "operation": "search",
                "data": [list(vector)],
                "limit": limit,
                "offset": offset,
                "filter": expr,
                "outputFields": list(output_fields),
                "search_params": {
                    "metricType": _MILVUS_METRIC[metric],
                    "params": {"nprobe": 16},
                },
            },
        )
        hits = data.get("data") or data.get("results") or []
        return tuple(
            (item, _hit_score(item, metric=metric, index=index)) for index, item in enumerate(hits)
        )

    def _v1_query(
        self,
        name: str,
        expr: str,
        output_fields: Sequence[str],
        *,
        limit: int = _QUERY_PAGE_SIZE,
        offset: int = 0,
    ) -> tuple[Mapping[str, Any], ...]:
        data = self._post(
            "/api/v1/entities",
            {
                "collection_name": name,
                "operation": "query",
                "filter": expr,
                "outputFields": list(output_fields),
                "limit": limit,
                "offset": offset,
            },
        )
        rows = data.get("data") or data.get("results") or []
        return tuple(item for item in rows if isinstance(item, Mapping))

    def health(self) -> None:
        try:
            self._get("/healthz")
            return
        except PlatformError:
            pass
        self._v1_collection_list()

    def has_collection(self, name: str) -> bool:
        return self._v1_collection_exists(name)

    def describe_collection(self, name: str) -> Mapping[str, Any]:
        return self._v1_collection_list()

    def create_collection(self, name: str, *, dimension: int, metric: str) -> None:
        if self._v1_collection_exists(name):
            return
        if not self._allow_create:
            raise PlatformError(
                "milvus_collection_missing",
                "Milvus collection was not found",
                {},
                503,
            )
        self._v1_create_collection(name, dimension=dimension, metric=metric)

    def insert(self, name: str, rows: Sequence[Mapping[str, Any]]) -> None:
        if not rows:
            return
        self._v1_insert(name, rows)

    def delete(self, name: str, expr: str) -> int:
        return self._v1_delete(name, expr)

    def query(
        self,
        name: str,
        expr: str,
        output_fields: Sequence[str],
        *,
        limit: int = _QUERY_PAGE_SIZE,
        offset: int = 0,
    ) -> tuple[Mapping[str, Any], ...]:
        return self._v1_query(name, expr, output_fields, limit=limit, offset=offset)

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
        return self._v1_search(
            name,
            vector,
            limit=limit,
            offset=offset,
            expr=expr,
            metric=metric,
            output_fields=output_fields,
        )


def _varchar_fields() -> list[dict[str, Any]]:
    names = (
        ("generation_id", "128"),
        ("document_id", "128"),
        ("document_version_id", "128"),
        ("publication_id", "128"),
        ("space_id", "128"),
        ("chunk_id", "128"),
        ("attempt_id", "128"),
        ("status", "32"),
        ("content_hash", "128"),
        ("fencing_token", "32"),
        ("payload", "65535"),
    )
    return [
        {
            "fieldName": name,
            "dataType": "VarChar",
            "elementTypeParams": {"max_length": max_length},
        }
        for name, max_length in names
    ]


class MilvusIndexWriter:
    """Dense IndexWriter + ANN search. Startup never drops or overwrites collections."""

    provider_name = "milvus"
    backend_kind = "dense"

    def __init__(
        self,
        client: MilvusClient,
        embedding: EmbeddingProvider,
        *,
        collection_prefix: str = "ragqs",
        allow_create_collection: bool = False,
    ) -> None:
        self._client = client
        self._embedding = embedding
        self._prefix = collection_prefix
        self._allow_create = allow_create_collection
        self._collection_ready = False
        self.collection_name = milvus_collection_name(
            collection_prefix, embedding.config.revision, embedding.config.dimension
        )

    @property
    def embedding_config(self) -> EmbeddingConfig:
        return self._embedding.config

    def probe(self) -> None:
        self._client.health()
        if not self._client.has_collection(self.collection_name):
            if self._allow_create:
                return
            raise PlatformError(
                "milvus_collection_missing",
                "Milvus collection was not found",
                {"collection": self.collection_name},
                503,
            )
        description = self._client.describe_collection(self.collection_name)
        dimension = _collection_dimension(description)
        metric = _collection_metric(description)
        if dimension is not None and int(dimension) != self._embedding.config.dimension:
            raise PlatformError(
                "milvus_dimension_mismatch",
                "Milvus collection dimension does not match the embedding profile",
                {"expected": self._embedding.config.dimension, "actual": dimension},
                503,
            )
        expected_metric = _MILVUS_METRIC[self._embedding.config.metric]
        if metric is not None and metric.upper() != expected_metric:
            raise PlatformError(
                "milvus_metric_mismatch",
                "Milvus collection metric does not match the embedding profile",
                {"expected": expected_metric, "actual": metric},
                503,
            )
        self._collection_ready = True

    def ensure_collection(self) -> None:
        if self._collection_ready:
            return
        if self._client.has_collection(self.collection_name):
            self._collection_ready = True
            return
        if not self._allow_create:
            raise PlatformError(
                "milvus_collection_missing",
                "Milvus collection was not found",
                {"collection": self.collection_name},
                503,
            )
        self._client.create_collection(
            self.collection_name,
            dimension=self._embedding.config.dimension,
            metric=self._embedding.config.metric,
        )
        self._collection_ready = True

    def _require_generation_config(
        self, expected_generation_id: str | None, chunks: Sequence[IndexChunk]
    ) -> None:
        del expected_generation_id
        for chunk in chunks:
            metadata = dict(chunk.metadata)
            if not metadata:
                continue
            if not self._embedding.config.matches(
                model=metadata.get("embedding_model"),
                revision=metadata.get("embedding_revision"),
                dimension=metadata.get("embedding_dimension"),
                metric=metadata.get("embedding_metric"),
            ):
                raise PlatformError(
                    "embedding_config_conflict",
                    "embedding configuration does not match the generation manifest",
                    {},
                    409,
                )

    @_invalidates_collection_cache
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
    ) -> StageResult:
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
        self.ensure_collection()
        existing = self._client.query(
            self.collection_name,
            f"attempt_id == {_quote(attempt_id)} && publication_id == {_quote(publication_id)} "
            f"&& status == {_quote('staged')}",
            ("content_hash", "generation_id", "fencing_token"),
        )
        if existing:
            first = existing[0]
            result = StageResult(
                "staged",
                attempt_id,
                publication_id,
                str(first.get("generation_id") or prepared.generation_id),
                prepared.resource_ids,
                str(first.get("content_hash") or prepared.content_hash),
                int(first.get("fencing_token") or fencing_token),
            )
            if result.content_hash != prepared.content_hash:
                raise PlatformError(
                    "idempotency_key_conflict",
                    "staged content conflicts with an existing attempt",
                    {},
                    409,
                )
            return result
        discarded = self._client.query(
            self.collection_name,
            f"attempt_id == {_quote(attempt_id)} && publication_id == {_quote(publication_id)} "
            f"&& status == {_quote('discarded')}",
            ("generation_id", "content_hash", "fencing_token"),
        )
        if discarded:
            first = discarded[0]
            return StageResult(
                "discarded",
                attempt_id,
                publication_id,
                str(first.get("generation_id") or prepared.generation_id),
                prepared.resource_ids,
                str(first.get("content_hash") or prepared.content_hash),
                int(first.get("fencing_token") or fencing_token),
            )
        self._require_generation_config(expected_generation_id, prepared.chunks)
        vectors = self._embedding.embed(tuple(item.embedding_text for item in prepared.chunks))
        rows = [
            _row(
                status="staged",
                attempt_id=attempt_id,
                chunk=chunk,
                vector=vector,
                content_hash=prepared.content_hash,
                fencing_token=fencing_token,
            )
            for chunk, vector in zip(prepared.chunks, vectors, strict=True)
        ]
        self._client.insert(self.collection_name, rows)
        return StageResult(
            "staged",
            attempt_id,
            publication_id,
            prepared.generation_id,
            prepared.resource_ids,
            prepared.content_hash,
            fencing_token,
        )

    @_invalidates_collection_cache
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
        self.ensure_collection()
        staged = self._load_rows(attempt_id, publication_id, "staged")
        if not staged:
            published = self._load_rows(attempt_id, publication_id, "published")
            if published:
                result = _result_from_rows("published", attempt_id, publication_id, published)
                validate_stage_identity(
                    result,
                    fencing_token=fencing_token,
                    expected_generation_id=expected_generation_id,
                    stage_resource_manifest=stage_resource_manifest,
                    content_hash=content_hash,
                )
                return result
            raise PlatformError("index_stage_not_found", "staged index was not found", {}, 404)
        result = _result_from_rows("staged", attempt_id, publication_id, staged)
        validate_stage_identity(
            result,
            fencing_token=fencing_token,
            expected_generation_id=expected_generation_id,
            stage_resource_manifest=stage_resource_manifest,
            content_hash=content_hash,
        )
        chunks = tuple(_chunk_from_row(row) for row in staged)
        if validator is not None and not validator(chunks):
            raise PlatformError(
                "index_release_blocked", "documents validation rejected publish", {}, 409
            )
        published_rows = [
            {**dict(row), "id": _published_id(row), "status": "published"} for row in staged
        ]
        self._client.insert(self.collection_name, published_rows)
        self._client.delete(
            self.collection_name,
            f"attempt_id == {_quote(attempt_id)} && publication_id == {_quote(publication_id)} "
            f"&& status == {_quote('staged')}",
        )
        return StageResult(
            "published",
            result.attempt_id,
            result.publication_id,
            result.generation_id,
            result.resource_ids,
            result.content_hash,
            result.fencing_token,
        )

    @_invalidates_collection_cache
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
        self.ensure_collection()
        staged = self._load_rows(attempt_id, publication_id, "staged")
        published = self._load_rows(attempt_id, publication_id, "published")
        rows = staged or published
        if rows:
            result = _result_from_rows(
                "staged" if staged else "published", attempt_id, publication_id, rows
            )
            validate_stage_identity(
                result,
                fencing_token=fencing_token,
                expected_generation_id=expected_generation_id,
                stage_resource_manifest=stage_resource_manifest,
                content_hash=content_hash,
            )
            self._client.delete(
                self.collection_name,
                f"attempt_id == {_quote(attempt_id)} && publication_id == {_quote(publication_id)} "
                f"&& status in [{_quote('staged')}, {_quote('published')}]",
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
        return StageResult("discarded", attempt_id, publication_id, "", (), "", 1)

    @_invalidates_collection_cache
    def delete_document_version(
        self, document_id: str, document_version_id: str, *, generation_id: str | None = None
    ) -> int:
        self.ensure_collection()
        expr = (
            f"document_id == {_quote(document_id)} && "
            f"document_version_id == {_quote(document_version_id)}"
        )
        if generation_id:
            expr += f" && generation_id == {_quote(generation_id)}"
        return self._client.delete(self.collection_name, expr)

    @_invalidates_collection_cache
    def delete_document(self, document_id: str, *, generation_id: str | None = None) -> int:
        self.ensure_collection()
        expr = f"document_id == {_quote(document_id)}"
        if generation_id:
            expr += f" && generation_id == {_quote(generation_id)}"
        return self._client.delete(self.collection_name, expr)

    @_invalidates_collection_cache
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
        self.ensure_collection()
        vector = self._embedding.embed((query,))[0]
        offset = _cursor_decode(cursor)
        spaces = ", ".join(_quote(str(item)) for item in space_ids)
        expr = f"status == {_quote('published')} && space_id in [{spaces}]"
        if generation_id:
            expr += f" && generation_id == {_quote(generation_id)}"
        page_size = top_k + 1
        hits = self._client.search(
            self.collection_name,
            vector,
            limit=page_size,
            offset=offset,
            expr=expr,
            metric=self._embedding.config.metric,
            output_fields=_OUTPUT_FIELDS,
        )
        items = tuple(_chunk_from_row(row) for row, _score in hits[:top_k])
        next_cursor = _cursor_encode(offset + len(items)) if len(hits) > top_k else None
        scored = tuple(
            {**chunk.to_mapping(), "score": score, "publication_id": chunk.publication_id}
            for chunk, (_row, score) in zip(items, hits[:top_k], strict=True)
        )
        return ProviderSearchPage(scored, next_cursor)

    def _load_rows(
        self, attempt_id: str, publication_id: str, status: str
    ) -> tuple[Mapping[str, Any], ...]:
        expr = (
            f"attempt_id == {_quote(attempt_id)} && publication_id == {_quote(publication_id)} "
            f"&& status == {_quote(status)}"
        )
        collected: list[Mapping[str, Any]] = []
        offset = 0
        while True:
            page = self._client.query(
                self.collection_name,
                expr,
                _OUTPUT_FIELDS,
                limit=_QUERY_PAGE_SIZE,
                offset=offset,
            )
            collected.extend(page)
            if len(page) < _QUERY_PAGE_SIZE:
                return tuple(collected)
            offset += _QUERY_PAGE_SIZE


def _row(
    *,
    status: str,
    attempt_id: str,
    chunk: IndexChunk,
    vector: Sequence[float],
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
        "vector": list(vector),
        "generation_id": chunk.generation_id,
        "document_id": chunk.document_id,
        "document_version_id": chunk.document_version_id,
        "publication_id": chunk.publication_id,
        "space_id": chunk.space_id,
        "chunk_id": chunk.chunk_id,
        "attempt_id": attempt_id,
        "status": status,
        "content_hash": content_hash,
        "fencing_token": str(fencing_token),
        "payload": json.dumps(chunk.to_mapping(), ensure_ascii=True, separators=(",", ":")),
    }


def _published_id(row: Mapping[str, Any]) -> str:
    return f"published:{row['generation_id']}:{row['publication_id']}:{row['chunk_id']}"


def _result_from_rows(
    state: str,
    attempt_id: str,
    publication_id: str,
    rows: Sequence[Mapping[str, Any]],
) -> StageResult:
    first = rows[0]
    chunks = tuple(_chunk_from_row(row) for row in rows)
    return StageResult(
        state,
        attempt_id,
        publication_id,
        str(first.get("generation_id") or chunks[0].generation_id),
        tuple(f"{attempt_id}:{publication_id}:{chunk.chunk_id}" for chunk in chunks),
        str(first.get("content_hash") or ""),
        int(first.get("fencing_token") or 1),
    )


def _chunk_from_row(row: Mapping[str, Any]) -> IndexChunk:
    payload = row.get("payload")
    if isinstance(payload, str) and payload:
        return IndexChunk.from_mapping(json.loads(payload))
    if isinstance(payload, Mapping):
        return IndexChunk.from_mapping(payload)
    return IndexChunk.from_mapping(
        {
            "chunk_id": row["chunk_id"],
            "generation_id": row["generation_id"],
            "publication_id": row["publication_id"],
            "document_id": row["document_id"],
            "document_version_id": row["document_version_id"],
            "space_id": row["space_id"],
            "text": row.get("text") or row["chunk_id"],
            "embedding_text": row.get("embedding_text") or row.get("text") or row["chunk_id"],
            "locator": {},
            "snippet": None,
            "media_kind": "text",
            "manifest_hash": row.get("manifest_hash") or "unknown",
        }
    )


def _collection_dimension(description: Mapping[str, Any]) -> int | None:
    fields = description.get("fields") or description.get("schema", {}).get("fields") or []
    for field in fields:
        if not isinstance(field, Mapping):
            continue
        name = str(field.get("fieldName") or field.get("name") or "")
        if name != "vector":
            continue
        params = field.get("elementTypeParams") or field.get("params") or {}
        dim = params.get("dim") or field.get("dim")
        if dim is not None:
            return int(dim)
    return None


def _collection_metric(description: Mapping[str, Any]) -> str | None:
    indexes = description.get("indexes") or description.get("index_descriptions") or []
    for item in indexes:
        if not isinstance(item, Mapping):
            continue
        metric = item.get("metricType") or item.get("metric_type")
        if metric:
            return str(metric)
    return description.get("metricType") or description.get("metric_type")


__all__ = [
    "HttpMilvusClient",
    "MilvusClient",
    "MilvusIndexWriter",
    "milvus_collection_name",
]
