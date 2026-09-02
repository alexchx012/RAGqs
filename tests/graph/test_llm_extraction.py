"""Remote LLM public-graph extraction transport tests (mock transport).

抽取计量经真实 session/recorder 管道、provenance 与抽取身份由 transport 盖章、
坏模型输出经既有失败终态管道（graph_provider_call_failed）、deadline/lease 门控。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

from app.graph.extraction import DbGraphExtractionSession
from app.graph.llm_extraction import LlmPublicGraphExtractor
from app.graph.usage import GraphUsageRecorder
from app.platform.errors import PlatformError


def _publication(number: int) -> dict[str, str]:
    return {
        "document_id": f"doc_{number}",
        "document_version_id": f"ver_{number}",
        "publication_id": f"pub_{number}",
        "content_manifest_id": f"manifest_{number}",
        "content_manifest_hash": f"hash_{number}",
    }


def _model_graph(number: int) -> dict[str, Any]:
    return {
        "nodes": [
            {
                "canonical_key": f"doc_{number}:topic",
                "entity_type": "topic",
                "display_name": f"Document {number}",
                "aliases": [f"D{number}"],
                "chunk_locator": {"content_manifest_id": f"manifest_{number}"},
                "confidence": 0.9,
            }
        ],
        "edges": [],
    }


def _chat_response(content: str) -> httpx.Response:
    return httpx.Response(
        200,
        json={"choices": [{"message": {"role": "assistant", "content": content}}]},
    )


@dataclass
class _Snapshot:
    publications: tuple[dict[str, str], ...]


class _RecordingUsage:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def prepare_provider_call(self, **kwargs: Any) -> str:
        call_id = f"pcall_{len(self.events)}"
        self.events.append({"phase": "prepared", "call_id": call_id, **kwargs})
        return call_id

    def mark_dispatching(self, provider_call_id: str, **kwargs: Any) -> bool:
        self.events.append({"phase": "dispatching", "call_id": provider_call_id})
        return True

    def complete_provider_call(self, **kwargs: Any) -> str:
        self.events.append({"phase": "completed", **kwargs})
        return str(kwargs["provider_call_id"])

    def mark_not_sent(self, provider_call_id: str) -> None:
        self.events.append({"phase": "not_sent", "call_id": provider_call_id})

    def mark_unknown(self, provider_call_id: str) -> None:
        self.events.append({"phase": "unknown", "call_id": provider_call_id})


def _session(
    usage: _RecordingUsage,
    *,
    deadline_expired: bool = False,
) -> DbGraphExtractionSession:
    now = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)

    def heartbeat() -> bool:
        usage.events.append({"phase": "heartbeat"})
        return True

    def write_staging(kind: str, resource_id: str, payload: Any) -> None:
        usage.events.append(
            {
                "phase": "staged",
                "resource_kind": kind,
                "resource_id": resource_id,
                "payload": dict(payload),
            }
        )

    recorder = GraphUsageRecorder(
        submission=usage,
        provider="openai_compatible",
        model="public-graph-extraction-v1",
        operation="graph_extraction",
        execution_id="gb_test",
        attempt_id="gb_test:1",
        generation_id="graph_generation_gb_test",
        actor_user_id="ops_1",
        deadline_utc=now + timedelta(seconds=120),
        started_at=lambda: now,
    )
    return DbGraphExtractionSession(
        run=None,  # type: ignore[arg-type]
        write_staging=write_staging,
        heartbeat=heartbeat,
        recorder=recorder,
        now=lambda: (now + timedelta(seconds=121) if deadline_expired else now),
        deadline_seconds=120,
    )


def _extractor(handler) -> LlmPublicGraphExtractor:
    return LlmPublicGraphExtractor(
        base_url="https://extraction.example.test/v1",
        model="public-graph-extraction-v1",
        prompt_version="public-graph-v2",
        api_key="extract-key",
        transport=httpx.MockTransport(handler),
        sleep=lambda _seconds: None,
    )


def test_extract_stamps_provenance_and_identity_and_meters_each_call() -> None:
    usage = _RecordingUsage()
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        body = json.loads(request.content.decode("utf-8"))
        publication = json.loads(body["messages"][1]["content"])
        return _chat_response(
            json.dumps(_model_graph(int(publication["document_id"].split("_")[1])))
        )

    extractor = _extractor(handler)
    snapshot = _Snapshot((_publication(1), _publication(2)))
    assert extractor.estimate_primary_model_calls(snapshot) == 2
    extractor.extract(snapshot, _session(usage))
    extractor.close()

    assert len(requests) == 2
    assert str(requests[0].url).endswith("/chat/completions")
    assert requests[0].headers["authorization"] == "Bearer extract-key"
    body = json.loads(requests[0].content.decode("utf-8"))
    assert body["model"] == "public-graph-extraction-v1"
    assert "STRICT JSON" in body["messages"][0]["content"]

    prepared = [event for event in usage.events if event["phase"] == "prepared"]
    assert len(prepared) == 2
    assert {event["resource_id"] for event in prepared} == {"manifest_1", "manifest_2"}
    assert all(event["operation"] == "graph_extraction" for event in prepared)
    assert all(event["call_id"] for event in prepared)
    completed = [event for event in usage.events if event["phase"] == "completed"]
    assert [event["result"] for event in completed] == ["succeeded", "succeeded"]

    staged = [event for event in usage.events if event["phase"] == "staged"]
    assert [event["resource_id"] for event in staged] == ["pub_1", "pub_2"]
    graph = staged[0]["payload"]["graph"]
    # Provenance and extraction identity are stamped by the extractor, never
    # taken from the model output.
    assert graph["source"] == {"document_id": "doc_1", "content_manifest_id": "manifest_1"}
    node = graph["nodes"][0]
    assert node["canonical_key"] == "doc_1:topic"
    assert node["extraction_model_revision"] == "public-graph-extraction-v1"
    assert node["prompt_revision"] == "public-graph-v2"
    assert node["confidence"] == 0.9
    assert graph["edges"] == []


def test_non_json_model_output_fails_through_the_terminal_pipeline() -> None:
    usage = _RecordingUsage()

    def handler(request: httpx.Request) -> httpx.Response:
        return _chat_response("definitely not json")

    extractor = _extractor(handler)
    session = _session(usage)
    with pytest.raises(PlatformError) as error:
        extractor.extract(_Snapshot((_publication(1),)), session)
    assert error.value.code == "graph_provider_call_failed"
    extractor.close()
    # The provider call was metered, accounted failed, and nothing was staged.
    completed = [event for event in usage.events if event["phase"] == "completed"]
    assert [event["result"] for event in completed] == ["failed"]
    assert not [event for event in usage.events if event["phase"] == "staged"]


@pytest.mark.parametrize(
    "graph",
    (
        {"nodes": "not-a-list", "edges": []},
        {
            "nodes": [
                {
                    "canonical_key": "k",
                    "entity_type": "topic",
                    "display_name": "K",
                    "aliases": [],
                    "chunk_locator": {"chunk": "1"},
                },
                {
                    "canonical_key": "k",
                    "entity_type": "topic",
                    "display_name": "K",
                    "aliases": [],
                    "chunk_locator": {"chunk": "1"},
                },
            ],
            "edges": [],
        },
        {
            "nodes": [
                {
                    "canonical_key": "k",
                    "entity_type": "topic",
                    "display_name": "K",
                    "aliases": [],
                    "chunk_locator": {"chunk": "1"},
                }
            ],
            "edges": [
                {
                    "source_key": "k",
                    "target_key": "missing",
                    "relation_type": "references",
                    "directed": True,
                    "properties": {},
                    "chunk_locator": {"chunk": "1"},
                }
            ],
        },
        {
            "nodes": [
                {
                    "canonical_key": "k",
                    "entity_type": "topic",
                    "aliases": [],
                    "chunk_locator": {"chunk": "1"},
                }
            ],
            "edges": [],
        },
    ),
)
def test_schema_violations_fail_the_run_not_the_integrity(graph: dict[str, Any]) -> None:
    usage = _RecordingUsage()

    def handler(request: httpx.Request) -> httpx.Response:
        return _chat_response(json.dumps(graph))

    extractor = _extractor(handler)
    with pytest.raises(PlatformError) as error:
        extractor.extract(_Snapshot((_publication(1),)), _session(usage))
    assert error.value.code == "graph_provider_call_failed"
    extractor.close()
    assert not [event for event in usage.events if event["phase"] == "staged"]


def test_expired_budget_never_sends() -> None:
    usage = _RecordingUsage()

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no provider send expected after deadline")

    extractor = _extractor(handler)
    with pytest.raises(PlatformError) as error:
        extractor.extract(_Snapshot((_publication(1),)), _session(usage, deadline_expired=True))
    assert error.value.code == "graph_provider_dispatch_failed"
    extractor.close()
    assert usage.events == []


def test_transport_failure_fails_through_the_terminal_pipeline() -> None:
    usage = _RecordingUsage()
    extractor = _extractor(lambda request: httpx.Response(401, text="denied"))
    with pytest.raises(PlatformError) as error:
        extractor.extract(_Snapshot((_publication(1),)), _session(usage))
    assert error.value.code == "graph_provider_call_failed"
    extractor.close()


def test_estimate_rejects_unknown_snapshot_shape() -> None:
    extractor = _extractor(lambda request: httpx.Response(200, json={}))
    with pytest.raises(PlatformError) as error:
        extractor.estimate_primary_model_calls(object())
    assert error.value.code == "graph_build_estimate_unavailable"
    extractor.close()
