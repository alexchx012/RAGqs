"""Graph extraction plan and default deterministic executor.

The plan is derived before enqueueing (one primary call per public
publication). Staging writes and provider-call usage both flow through the
fence-bound session provided by the worker.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any, cast

from app.platform.errors import PlatformError

from .models import GraphRunRecord
from .ports import GraphExtractionSession
from .usage import GraphUsageRecorder


class DbGraphExtractionSession:
    """Fence-bound session: staging writes and usage calls for one attempt."""

    def __init__(
        self,
        *,
        run: GraphRunRecord,
        write_staging: Callable[[str, str, Mapping[str, Any]], None],
        heartbeat: Callable[[], bool],
        recorder: GraphUsageRecorder,
        now: Callable[[], datetime],
        deadline_seconds: int = 60,
    ) -> None:
        self._run = run
        self._write_staging = write_staging
        self._heartbeat = heartbeat
        self._recorder = recorder
        self._now = now
        self._deadline_seconds = deadline_seconds
        self.primary_calls = 0
        self.provider_calls = 0
        self.staging = _FencedStagingWriter(self._write_staging)

    def primary_call(
        self,
        *,
        operation: str,
        resource_id: str | None,
        request_fingerprint: str,
        send: Callable[[], Any],
    ) -> Any:
        self.provider_calls += 1
        result = self._recorder.record_call(
            resource_id=resource_id,
            request_fingerprint=request_fingerprint,
            send=send,
        )
        self.primary_calls += 1
        return result

    def heartbeat(self) -> None:
        if not self._heartbeat():
            raise PlatformError(
                "graph_build_lease_lost",
                "The graph run lease or fence no longer matches",
                {},
                409,
            )

    def deadline_expired(self) -> bool:
        return self._now() >= self._recorder.deadline_utc


class _FencedStagingWriter:
    def __init__(self, write: Callable[[str, str, Mapping[str, Any]], None]) -> None:
        self._write = write

    def stage(self, *, resource_kind: str, resource_id: str, payload: Mapping[str, Any]) -> None:
        self._write(resource_kind, resource_id, payload)


class DeterministicPublicGraphExtractor:
    """Default dev extractor: one primary call per publication, deterministic
    resources, bounded short retries (3 attempts per publication)."""

    MAX_ATTEMPTS = 3

    def estimate_primary_model_calls(self, snapshot: Any) -> int:
        publications = getattr(snapshot, "publications", None)
        if not isinstance(publications, (list, tuple)):
            raise PlatformError(
                "graph_build_estimate_unavailable",
                "The public graph plan cannot be estimated from the snapshot",
                {},
                503,
            )
        return len(publications)

    def extract(self, snapshot: Any, session: GraphExtractionSession) -> None:
        publications = tuple(getattr(snapshot, "publications", ()))
        for publication in publications:
            publication_mapping: Mapping[str, str] = (
                publication if isinstance(publication, Mapping) else {}
            )
            publication_id = str(publication_mapping.get("publication_id", ""))
            content_manifest_id = str(publication_mapping.get("content_manifest_id", ""))
            fingerprint = _fingerprint(publication_mapping)
            result = self._call_with_retries(
                session,
                resource_id=content_manifest_id or None,
                request_fingerprint=fingerprint,
                send=cast(
                    Callable[[], Any],
                    lambda pub=publication_mapping: _deterministic_graph(pub),
                ),
            )
            session.staging.stage(
                resource_kind="publication_graph",
                resource_id=publication_id or fingerprint,
                payload={"graph": result},
            )

    def _call_with_retries(
        self,
        session: GraphExtractionSession,
        *,
        resource_id: str | None,
        request_fingerprint: str,
        send: Callable[[], Any],
    ) -> Any:
        last_error: PlatformError | None = None
        for _ in range(self.MAX_ATTEMPTS):
            if session.deadline_expired():
                if last_error is not None:
                    raise last_error
                raise PlatformError(
                    "graph_provider_dispatch_failed",
                    "The graph build deadline expired before a provider call",
                    {},
                    503,
                )
            try:
                session.heartbeat()
                return session.primary_call(
                    operation="graph_extraction",
                    resource_id=resource_id,
                    request_fingerprint=request_fingerprint,
                    send=send,
                )
            except PlatformError as error:
                last_error = error
                if error.code == "graph_build_lease_lost" or session.deadline_expired():
                    break
        assert last_error is not None
        raise last_error


def _deterministic_graph(publication: Mapping[str, str]) -> dict[str, Any]:
    document_id = str(publication.get("document_id", ""))
    digest = hashlib.sha256(
        json.dumps(
            dict(publication), ensure_ascii=True, separators=(",", ":"), sort_keys=True
        ).encode("utf-8")
    ).hexdigest()
    return {
        "source": {
            "document_id": document_id,
            "content_manifest_id": str(publication.get("content_manifest_id", "")),
        },
        "entity_manifest_hash": digest[:16],
        "nodes": [
            {
                "canonical_key": f"{document_id}:topic",
                "entity_type": "topic",
                "display_name": document_id,
                "aliases": [],
                "chunk_locator": {
                    "content_manifest_id": str(publication.get("content_manifest_id", ""))
                },
                "extraction_model_revision": "public-graph-extraction-v1",
                "prompt_revision": "public-graph-v1",
                "confidence": 1.0,
            }
        ],
        "edges": [],
    }


def _fingerprint(publication: Mapping[str, str]) -> str:
    encoded = json.dumps(
        dict(publication), ensure_ascii=True, separators=(",", ":"), sort_keys=True
    )
    return hashlib.sha256(b"public-graph-call-v1\0" + encoded.encode("utf-8")).hexdigest()


__all__ = ["DbGraphExtractionSession", "DeterministicPublicGraphExtractor"]
