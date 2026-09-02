"""Remote LLM public-graph extraction transport (``GraphExtractorPort``).

Replaces the deterministic development implementation when the deployment
configures an extraction endpoint (``RAG_GRAPH_EXTRACTION_PROVIDER=llm``). The
graph domain pipeline is reused unchanged: the fence-bound session owns usage
metering (``session.primary_call`` → ``GraphUsageRecorder``), bounded short
retries/backoff/circuit-breaking come from the unified egress kernel
(``model_http_post``), and any send or schema failure terminalizes the run
through the existing ``graph_provider_call_failed`` failure class.

Wire contract: one OpenAI-compatible chat-completions call per publication. The
frozen source snapshot carries only immutable manifest identifiers, so the
prompt hands the publication manifest to the deployment's extraction endpoint
(expected to hold document access); the model returns strict JSON with the
entity/relation facts. The extractor stamps provenance and extraction identity
(model/prompt revision) itself — never trusting the model for them.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from datetime import datetime
from hashlib import sha256
from typing import Any, cast

from app.platform.errors import PlatformError
from app.platform.model_http import (
    ModelHttpError,
    build_model_http_client,
    model_http_post,
)
from app.platform.provider import CircuitOpen

from .ports import GraphExtractionSession

GRAPH_EXTRACTION_OPERATION = "graph_extraction"
GRAPH_EXTRACTION_DEFAULT_TIMEOUT_SECONDS = 60.0

LLM_GRAPH_SYSTEM_INSTRUCTION = (
    "You extract the public knowledge graph for one published document. Return "
    "STRICT JSON only, no markdown fences, matching exactly: "
    '{"nodes": [{"canonical_key": str, "entity_type": str, "display_name": str, '
    '"aliases": [str], "chunk_locator": {"content_manifest_id": str}, '
    '"confidence": number}], "edges": [{"source_key": str, "target_key": str, '
    '"relation_type": str, "directed": bool, "properties": {}, '
    '"chunk_locator": {"content_manifest_id": str}}]}. Edge endpoints must match '
    "node canonical keys. Use the document manifest identifiers verbatim."
)

_NODE_TEXT_FIELDS = ("canonical_key", "entity_type", "display_name")
_EDGE_TEXT_FIELDS = ("source_key", "target_key", "relation_type")


class LlmPublicGraphExtractor:
    """LLM extraction transport with session-owned metering and staging."""

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        prompt_version: str,
        api_key: str = "",
        timeout_seconds: float = GRAPH_EXTRACTION_DEFAULT_TIMEOUT_SECONDS,
        transport: Any = None,
        now: Callable[[], datetime] | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        self._model = model
        self._prompt_version = prompt_version
        self._timeout_seconds = float(timeout_seconds)
        self._now = now
        self._sleep = sleep
        self._base_url = base_url.rstrip("/")
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if api_key.strip():
            headers["Authorization"] = f"Bearer {api_key.strip()}"
        self._client = build_model_http_client(
            base_url=base_url,
            headers=headers,
            timeout=float(timeout_seconds),
            transport=transport,
        )

    def close(self) -> None:
        self._client.close()

    def dispose(self) -> None:
        self.close()

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
            # Deadline/lease gate before the provider call: a lost lease or an
            # expired budget must never produce an outbound side effect.
            if session.deadline_expired():
                raise PlatformError(
                    "graph_provider_dispatch_failed",
                    "The graph build deadline expired before a provider call",
                    {},
                    503,
                )
            session.heartbeat()
            graph = session.primary_call(
                operation=GRAPH_EXTRACTION_OPERATION,
                resource_id=content_manifest_id or None,
                request_fingerprint=_request_fingerprint(publication_mapping),
                send=cast(
                    "Callable[[], Any]",
                    lambda pub=publication_mapping: self._extract_publication(pub),
                ),
            )
            session.staging.stage(
                resource_kind="publication_graph",
                resource_id=publication_id or _request_fingerprint(publication_mapping),
                payload={"graph": graph},
            )

    def _extract_publication(self, publication: Mapping[str, str]) -> dict[str, Any]:
        try:
            egress = model_http_post(
                provider="openai_compatible",
                operation=GRAPH_EXTRACTION_OPERATION,
                url=f"{self._base_url}/chat/completions",
                payload={
                    "model": self._model,
                    "temperature": 0,
                    "messages": [
                        {"role": "system", "content": LLM_GRAPH_SYSTEM_INSTRUCTION},
                        {"role": "user", "content": json.dumps(dict(publication))},
                    ],
                },
                timeout_seconds=self._timeout_seconds,
                client=self._client,
                asynchronous=True,
                now=self._now,
                sleep=self._sleep,
            )
        except CircuitOpen as exc:
            raise RuntimeError("graph extraction provider is unavailable") from exc
        except ModelHttpError as exc:
            raise RuntimeError(f"graph extraction provider failed: {exc.error_class}") from exc
        content = _chat_content(egress.body)
        try:
            parsed = json.loads(_strip_json_fence(content))
        except ValueError as exc:
            raise RuntimeError("graph extraction response was not JSON") from exc
        if not isinstance(parsed, dict):
            raise RuntimeError("graph extraction response was not a JSON object")
        return _assemble_graph(
            parsed,
            publication,
            model_revision=self._model,
            prompt_revision=self._prompt_version,
        )


def _chat_content(body: Any) -> str:
    content: Any = None
    if isinstance(body, Mapping):
        choices = body.get("choices")
        if isinstance(choices, list) and choices:
            first = choices[0]
            message = first.get("message") if isinstance(first, Mapping) else None
            if isinstance(message, Mapping):
                content = message.get("content")
    if not isinstance(content, str) or not content.strip():
        raise RuntimeError("graph extraction response content was malformed")
    return content


def _strip_json_fence(content: str) -> str:
    text = content.strip()
    if text.startswith("```"):
        first_newline = text.find("\n")
        if first_newline != -1:
            text = text[first_newline + 1 :]
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3]
    return text.strip()


def _assemble_graph(
    parsed: Mapping[str, Any],
    publication: Mapping[str, str],
    *,
    model_revision: str,
    prompt_revision: str,
) -> dict[str, Any]:
    raw_nodes = parsed.get("nodes")
    raw_edges = parsed.get("edges")
    if not isinstance(raw_nodes, list) or not isinstance(raw_edges, list):
        raise RuntimeError("graph extraction response nodes/edges must be arrays")
    nodes: list[dict[str, Any]] = []
    node_keys: set[str] = set()
    for raw_node in raw_nodes:
        if not isinstance(raw_node, Mapping):
            raise RuntimeError("graph extraction node is invalid")
        for field in _NODE_TEXT_FIELDS:
            value = raw_node.get(field)
            if not isinstance(value, str) or not value.strip():
                raise RuntimeError(f"graph extraction node {field} is required")
        canonical_key = str(raw_node["canonical_key"]).strip()
        if canonical_key in node_keys:
            raise RuntimeError("graph extraction node canonical_key is duplicated")
        node_keys.add(canonical_key)
        aliases = raw_node.get("aliases", [])
        if not isinstance(aliases, list) or any(
            not isinstance(alias, str) or not alias.strip() for alias in aliases
        ):
            raise RuntimeError("graph extraction node aliases are invalid")
        locator = raw_node.get("chunk_locator")
        if not isinstance(locator, Mapping) or not locator:
            raise RuntimeError("graph extraction node chunk_locator is required")
        confidence = raw_node.get("confidence", 1.0)
        if not isinstance(confidence, (int, float)) or isinstance(confidence, bool):
            raise RuntimeError("graph extraction node confidence is invalid")
        nodes.append(
            {
                "canonical_key": canonical_key,
                "entity_type": str(raw_node["entity_type"]).strip(),
                "display_name": str(raw_node["display_name"]).strip(),
                "aliases": [str(alias).strip() for alias in aliases],
                "chunk_locator": dict(locator),
                "extraction_model_revision": model_revision,
                "prompt_revision": prompt_revision,
                "confidence": float(confidence),
            }
        )
    edges: list[dict[str, Any]] = []
    for raw_edge in raw_edges:
        if not isinstance(raw_edge, Mapping):
            raise RuntimeError("graph extraction edge is invalid")
        for field in _EDGE_TEXT_FIELDS:
            value = raw_edge.get(field)
            if not isinstance(value, str) or not value.strip():
                raise RuntimeError(f"graph extraction edge {field} is required")
        source_key = str(raw_edge["source_key"]).strip()
        target_key = str(raw_edge["target_key"]).strip()
        if source_key not in node_keys or target_key not in node_keys:
            raise RuntimeError("graph extraction edge endpoint is not a staged node")
        if not isinstance(raw_edge.get("directed"), bool):
            raise RuntimeError("graph extraction edge directed must be boolean")
        properties = raw_edge.get("properties", {})
        if not isinstance(properties, Mapping):
            raise RuntimeError("graph extraction edge properties are invalid")
        locator = raw_edge.get("chunk_locator")
        if not isinstance(locator, Mapping) or not locator:
            raise RuntimeError("graph extraction edge chunk_locator is required")
        edges.append(
            {
                "source_key": source_key,
                "target_key": target_key,
                "relation_type": str(raw_edge["relation_type"]).strip(),
                "directed": bool(raw_edge["directed"]),
                "properties": dict(properties),
                "chunk_locator": dict(locator),
                "extraction_model_revision": model_revision,
                "prompt_revision": prompt_revision,
            }
        )
    # Provenance is stamped by the extractor from the frozen publication, never
    # taken from the model output (store.activate validates it strictly).
    return {
        "source": {
            "document_id": str(publication.get("document_id", "")),
            "content_manifest_id": str(publication.get("content_manifest_id", "")),
        },
        "nodes": nodes,
        "edges": edges,
    }


def _request_fingerprint(publication: Mapping[str, str]) -> str:
    encoded = json.dumps(
        dict(publication), ensure_ascii=True, separators=(",", ":"), sort_keys=True
    )
    return f"public-graph-llm:{sha256(encoded.encode('utf-8')).hexdigest()}"


__all__ = [
    "GRAPH_EXTRACTION_DEFAULT_TIMEOUT_SECONDS",
    "GRAPH_EXTRACTION_OPERATION",
    "LlmPublicGraphExtractor",
]
