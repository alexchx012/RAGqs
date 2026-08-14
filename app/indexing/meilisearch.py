from __future__ import annotations

import base64
import json
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urljoin

import httpx

from app.platform.errors import PlatformError

from .models import IndexChunk, ProviderSearchPage
from .providers import StageResult, validate_stage_chunks, validate_stage_identity

_FILTERABLE = (
    "space_id",
    "generation_id",
    "status",
    "document_id",
    "document_version_id",
    "publication_id",
    "attempt_id",
)
_SEARCHABLE = ("jieba_tokens", "text")


def pretokens(text: str) -> str:
    """Meilisearch-only jieba field; not part of the generic sparse contract."""

    try:
        import jieba
    except ImportError as exc:  # pragma: no cover - dependency is declared
        raise PlatformError(
            "sparse_analyzer_unavailable", "jieba is required for Meilisearch", {}, 503
        ) from exc
    return " ".join(token for token in jieba.cut(text) if token.strip())


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


class MeilisearchClient(Protocol):
    def health(self) -> None: ...

    def authorized(self) -> None: ...

    def has_index(self, name: str) -> bool: ...

    def ensure_index(self, name: str) -> None: ...

    def add_documents(self, name: str, documents: Sequence[Mapping[str, Any]]) -> None: ...

    def delete_documents(self, name: str, document_ids: Sequence[str]) -> None: ...

    def get_documents(
        self, name: str, *, filters: str, limit: int = 1000
    ) -> tuple[Mapping[str, Any], ...]: ...

    def search(
        self,
        name: str,
        query: str,
        *,
        filters: str,
        limit: int,
        offset: int,
    ) -> tuple[tuple[Mapping[str, Any], float], ...]: ...


class HttpMeilisearchClient:
    def __init__(
        self,
        url: str,
        *,
        api_key: str,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 30.0,
    ) -> None:
        if not url.strip():
            raise PlatformError("sparse_config_invalid", "Meilisearch url is required", {}, 422)
        if not api_key.strip():
            raise PlatformError("sparse_config_invalid", "Meilisearch api key is required", {}, 422)
        self._base = url.rstrip("/") + "/"
        self._api_key = api_key
        self._timeout = timeout
        self._transport = transport

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

    def _request(self, method: str, path: str, payload: Mapping[str, Any] | None = None) -> Any:
        url = urljoin(self._base, path.lstrip("/"))
        try:
            with httpx.Client(timeout=self._timeout, transport=self._transport) as client:
                response = client.request(method, url, headers=self._headers(), json=payload)
        except httpx.HTTPError as exc:
            raise PlatformError(
                "meilisearch_unavailable", "Meilisearch request failed", {}, 503
            ) from exc
        if response.status_code == 404:
            return None
        if response.status_code >= 400:
            raise PlatformError("meilisearch_unavailable", "Meilisearch request failed", {}, 503)
        if not response.content:
            return {}
        try:
            return response.json()
        except ValueError as exc:
            raise PlatformError(
                "meilisearch_unavailable", "Meilisearch response is invalid", {}, 503
            ) from exc

    def _wait_task(self, task: Mapping[str, Any] | None) -> None:
        if not isinstance(task, Mapping) or "taskUid" not in task:
            return
        task_uid = task["taskUid"]
        deadline = time.monotonic() + self._timeout
        while time.monotonic() < deadline:
            body = self._request("GET", f"/tasks/{task_uid}")
            if isinstance(body, Mapping) and body.get("status") in {"succeeded", "failed"}:
                if body.get("status") == "failed":
                    raise PlatformError(
                        "meilisearch_unavailable", "Meilisearch task failed", {}, 503
                    )
                return
            time.sleep(0.05)
        raise PlatformError("meilisearch_unavailable", "Meilisearch task timed out", {}, 503)

    def health(self) -> None:
        url = urljoin(self._base, "health")
        try:
            with httpx.Client(timeout=self._timeout, transport=self._transport) as client:
                response = client.get(url)
        except httpx.HTTPError as exc:
            raise PlatformError(
                "meilisearch_unavailable", "Meilisearch is unreachable", {}, 503
            ) from exc
        if response.status_code >= 400:
            raise PlatformError("meilisearch_unavailable", "Meilisearch is unreachable", {}, 503)

    def authorized(self) -> None:
        body = self._request("GET", "/indexes")
        if body is None:
            raise PlatformError("meilisearch_unauthorized", "Meilisearch auth failed", {}, 503)

    def has_index(self, name: str) -> bool:
        return self._request("GET", f"/indexes/{name}") is not None

    def ensure_index(self, name: str) -> None:
        if not self.has_index(name):
            self._wait_task(self._request("POST", "/indexes", {"uid": name, "primaryKey": "id"}))
        self._wait_task(
            self._request(
                "PATCH",
                f"/indexes/{name}/settings",
                {
                    "filterableAttributes": list(_FILTERABLE),
                    "searchableAttributes": list(_SEARCHABLE),
                },
            )
        )

    def _sanitize_id(self, id_str: str) -> str:
        """Meilisearch document ID must be alphanumeric, hyphen, underscore, max 511 bytes."""
        return "".join(c for c in id_str if c.isalnum() or c in "-_")[:500]

    def add_documents(self, name: str, documents: Sequence[Mapping[str, Any]]) -> None:
        if not documents:
            return
        sanitized = [
            {**item, "id": self._sanitize_id(str(item.get("id") or item.get("chunk_id") or ""))}
            for item in documents
        ]
        self._wait_task(self._request("POST", f"/indexes/{name}/documents", sanitized))

    def delete_documents(self, name: str, document_ids: Sequence[str]) -> None:
        if not document_ids:
            return
        self._wait_task(
            self._request("POST", f"/indexes/{name}/documents/delete-batch", list(document_ids))
        )

    def get_documents(
        self, name: str, *, filters: str, limit: int = 1000
    ) -> tuple[Mapping[str, Any], ...]:
        body = self._request(
            "POST",
            f"/indexes/{name}/documents/fetch",
            {"filter": filters, "limit": limit},
        )
        if not isinstance(body, Mapping):
            return ()
        rows = body.get("results") or []
        return tuple(item for item in rows if isinstance(item, Mapping))

    def search(
        self,
        name: str,
        query: str,
        *,
        filters: str,
        limit: int,
        offset: int,
    ) -> tuple[tuple[Mapping[str, Any], float], ...]:
        body = self._request(
            "POST",
            f"/indexes/{name}/search",
            {
                "q": query,
                "filter": filters,
                "limit": limit,
                "offset": offset,
                "showRankingScore": True,
                "attributesToSearchOn": ["jieba_tokens"],
            },
        )
        if not isinstance(body, Mapping):
            return ()
        hits: list[tuple[Mapping[str, Any], float]] = []
        for item in body.get("hits") or []:
            if not isinstance(item, Mapping):
                continue
            score = item.get("_rankingScore", 0.0)
            try:
                numeric = float(score)
            except (TypeError, ValueError):
                numeric = 0.0
            hits.append((item, numeric))
        return tuple(hits)


def probe_meilisearch_volume(data_path: str | None) -> None:
    if data_path is None or not str(data_path).strip():
        raise PlatformError(
            "meilisearch_volume_missing",
            "Meilisearch persistent volume path is required",
            {},
            503,
        )
    path = Path(data_path)
    if not path.exists() or not path.is_dir():
        raise PlatformError(
            "meilisearch_volume_missing",
            "Meilisearch persistent volume is not mounted",
            {"path": str(path)},
            503,
        )


class MeilisearchSparseIndexProvider:
    """Real Meilisearch BM25 backend. Scores stay inside this provider."""

    provider_name = "meilisearch"
    backend_kind = "sparse"

    def __init__(
        self,
        client: MeilisearchClient,
        *,
        index_name: str = "ragqs_chunks",
        data_path: str | None = None,
        allow_create_index: bool = False,
        tokenize=pretokens,
    ) -> None:
        self._client = client
        self._index = index_name
        self._data_path = data_path
        self._allow_create = allow_create_index
        self._tokenize = tokenize

    def probe(self) -> None:
        probe_meilisearch_volume(self._data_path)
        self._client.health()
        self._client.authorized()
        if not self._client.has_index(self._index) and not self._allow_create:
            raise PlatformError(
                "meilisearch_index_missing",
                "Meilisearch index was not found",
                {"index": self._index},
                503,
            )

    def ensure_index(self) -> None:
        if self._client.has_index(self._index):
            return
        if not self._allow_create:
            raise PlatformError(
                "meilisearch_index_missing",
                "Meilisearch index was not found",
                {"index": self._index},
                503,
            )
        self._client.ensure_index(self._index)

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
        self.ensure_index()
        existing = self._client.get_documents(
            self._index,
            filters=(
                f"attempt_id = {_quote(attempt_id)} AND publication_id = {_quote(publication_id)} "
                f"AND status = {_quote('staged')}"
            ),
        )
        if existing:
            first = existing[0]
            if str(first.get("content_hash") or "") != prepared.content_hash:
                raise PlatformError(
                    "idempotency_key_conflict",
                    "staged content conflicts with an existing attempt",
                    {},
                    409,
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
        documents = [
            _document(
                status="staged",
                attempt_id=attempt_id,
                chunk=chunk,
                content_hash=prepared.content_hash,
                fencing_token=fencing_token,
                tokens=self._tokenize(chunk.sparse_text or chunk.text),
            )
            for chunk in prepared.chunks
        ]
        self._client.add_documents(self._index, documents)
        return StageResult(
            "staged",
            attempt_id,
            publication_id,
            prepared.generation_id,
            prepared.resource_ids,
            prepared.content_hash,
            fencing_token,
        )

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
        staged = self._load(attempt_id, publication_id, "staged")
        if not staged:
            published = self._load(attempt_id, publication_id, "published")
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
        chunks = tuple(_chunk_from_doc(item) for item in staged)
        if validator is not None and not validator(chunks):
            raise PlatformError(
                "index_release_blocked", "documents validation rejected publish", {}, 409
            )
        published = [
            {**dict(item), "id": _published_id(item), "status": "published"} for item in staged
        ]
        self._client.add_documents(self._index, published)
        self._client.delete_documents(self._index, tuple(str(item["id"]) for item in staged))
        return StageResult(
            "published",
            result.attempt_id,
            result.publication_id,
            result.generation_id,
            result.resource_ids,
            result.content_hash,
            result.fencing_token,
        )

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
        staged = self._load(attempt_id, publication_id, "staged")
        published = self._load(attempt_id, publication_id, "published")
        docs = staged or published
        if docs:
            result = _result_from_docs(
                "staged" if staged else "published", attempt_id, publication_id, docs
            )
            validate_stage_identity(
                result,
                fencing_token=fencing_token,
                expected_generation_id=expected_generation_id,
                stage_resource_manifest=stage_resource_manifest,
                content_hash=content_hash,
            )
            self._client.delete_documents(self._index, tuple(str(item["id"]) for item in docs))
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

    def delete_document_version(
        self, document_id: str, document_version_id: str, *, generation_id: str | None = None
    ) -> int:
        self.ensure_index()
        filters = (
            f"document_id = {_quote(document_id)} AND "
            f"document_version_id = {_quote(document_version_id)}"
        )
        if generation_id:
            filters += f" AND generation_id = {_quote(generation_id)}"
        docs = self._client.get_documents(self._index, filters=filters)
        self._client.delete_documents(self._index, tuple(str(item["id"]) for item in docs))
        return len(docs)

    def delete_document(self, document_id: str, *, generation_id: str | None = None) -> int:
        self.ensure_index()
        filters = f"document_id = {_quote(document_id)}"
        if generation_id:
            filters += f" AND generation_id = {_quote(generation_id)}"
        docs = self._client.get_documents(self._index, filters=filters)
        self._client.delete_documents(self._index, tuple(str(item["id"]) for item in docs))
        return len(docs)

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
        spaces = " OR ".join(f"space_id = {_quote(str(item))}" for item in space_ids)
        filters = f"status = {_quote('published')} AND ({spaces})"
        if generation_id:
            filters += f" AND generation_id = {_quote(generation_id)}"
        hits = self._client.search(
            self._index,
            self._tokenize(query),
            filters=filters,
            limit=top_k + 1,
            offset=offset,
        )
        page = hits[:top_k]
        items = tuple(
            {
                **_chunk_from_doc(row).to_mapping(),
                "score": 0.0,
                "publication_id": row.get("publication_id"),
            }
            for row, _score in page
        )
        next_cursor = _cursor_encode(offset + len(page)) if len(hits) > top_k else None
        return ProviderSearchPage(items, next_cursor)

    def _load(
        self, attempt_id: str, publication_id: str, status: str
    ) -> tuple[Mapping[str, Any], ...]:
        return self._client.get_documents(
            self._index,
            filters=(
                f"attempt_id = {_quote(attempt_id)} AND publication_id = {_quote(publication_id)} "
                f"AND status = {_quote(status)}"
            ),
        )


def _document(
    *,
    status: str,
    attempt_id: str,
    chunk: IndexChunk,
    content_hash: str,
    fencing_token: int,
    tokens: str,
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
        "text": chunk.text,
        "jieba_tokens": tokens,
        "payload": json.dumps(chunk.to_mapping(), ensure_ascii=True, separators=(",", ":")),
    }


def _published_id(doc: Mapping[str, Any]) -> str:
    return f"published:{doc['generation_id']}:{doc['publication_id']}:{doc['chunk_id']}"


def _result_from_docs(
    state: str,
    attempt_id: str,
    publication_id: str,
    docs: Sequence[Mapping[str, Any]],
) -> StageResult:
    first = docs[0]
    chunks = tuple(_chunk_from_doc(item) for item in docs)
    return StageResult(
        state,
        attempt_id,
        publication_id,
        str(first.get("generation_id") or chunks[0].generation_id),
        tuple(f"{attempt_id}:{publication_id}:{chunk.chunk_id}" for chunk in chunks),
        str(first.get("content_hash") or ""),
        int(first.get("fencing_token") or 1),
    )


def _chunk_from_doc(doc: Mapping[str, Any]) -> IndexChunk:
    payload = doc.get("payload")
    if isinstance(payload, str) and payload:
        return IndexChunk.from_mapping(json.loads(payload))
    if isinstance(payload, Mapping):
        return IndexChunk.from_mapping(payload)
    return IndexChunk.from_mapping(doc)


__all__ = [
    "HttpMeilisearchClient",
    "MeilisearchClient",
    "MeilisearchSparseIndexProvider",
    "pretokens",
    "probe_meilisearch_volume",
]
