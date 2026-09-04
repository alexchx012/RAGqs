"""quick 非 A/B 生成的 delta 流式透传契约（chat-generation-latency）。

- quick 且非 A/B：provider 段落经聚合 sink 落 `delta` 事件（candidate=0），
  seq 单调递增、先于 answer；answer 事件仍是完整权威全文。
- think/deep 与 A/B：不产生 delta（缓冲路径不变）。
- delta 事件经 /v1/generations/{id}/events 按 seq 重放透传。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select

import app.chat.worker as worker_module
from app.chat.models import AskRequest
from app.chat.schema import chat_generation_event_table

from .conftest import FakeChatProvider, build_test_env, provision_and_login, sse_frames


class _StreamingChatProvider(FakeChatProvider):
    """Mirrors the DashScope streaming path: chunk callbacks before the final answer."""

    def __init__(self) -> None:
        super().__init__()
        self.streamed: list[bool] = []

    def generate(self, request):  # type: ignore[no-untyped-def]
        self.streamed.append(request.on_delta is not None)
        if request.on_delta is not None:
            request.on_delta("answer ")
            request.on_delta("for ")
        return super().generate(request)


def _run_generation(env: dict, *, effort_level: str):
    token, _ = provision_and_login(env["identity"], "alice")
    headers = {"Authorization": f"Bearer {token}"}
    conversation_id = env["client"].post("/v1/conversations", json={}, headers=headers).json()["id"]
    principal = env["identity"].authenticate_access_token(token)
    result = (
        env["runtime"]
        .resolve("chat_generation_service")
        .ask(
            principal=principal,
            conversation_id=conversation_id,
            request=AskRequest(content="hello", effort_level=effort_level, scope=None),
            idempotency_key=f"ask-delta-{effort_level}",
        )
    )
    env["runtime"].resolve("chat_generation_worker").run_once()
    with env["engine"].connect() as connection:
        rows = (
            connection.execute(
                select(
                    chat_generation_event_table.c.event_seq,
                    chat_generation_event_table.c.event_type,
                    chat_generation_event_table.c.data_json,
                )
                .where(chat_generation_event_table.c.generation_id == result.generation_id)
                .order_by(chat_generation_event_table.c.event_seq)
            )
            .mappings()
            .all()
        )
    return result, [dict(row) for row in rows], headers


def _delta_rows(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [event for event in events if event["event_type"] == "delta"]


def test_quick_generation_streams_delta_events_before_authoritative_answer() -> None:
    provider = _StreamingChatProvider()
    env = build_test_env(provider=provider)
    result, events, headers = _run_generation(env, effort_level="quick")

    assert provider.streamed == [True]
    deltas = _delta_rows(events)
    assert len(deltas) >= 1
    assert all(event["data_json"]["candidate"] == 0 for event in deltas)
    joined = "".join(event["data_json"]["content"] for event in deltas)
    assert joined == "answer for "
    seqs = [event["event_seq"] for event in events]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)
    # delta 先于 answer；answer 仍一次性携带完整权威全文。
    types = [event["event_type"] for event in events]
    assert types[0] == "start" and types[-1] == "done"
    answer = next(event for event in events if event["event_type"] == "answer")
    assert deltas[-1]["event_seq"] < answer["event_seq"]
    assert answer["data_json"]["content"].startswith(joined)

    # delta 事件经 SSE 端点按 seq 重放（先于 answer 帧，id 单调）。
    response = env["client"].get(
        f"/v1/generations/{result.generation_id}/events",
        headers={**headers, "Accept": "text/event-stream"},
    )
    assert response.status_code == 200
    frames = [
        (event, int(event_id), json.loads(data))
        for event, event_id, data in sse_frames(response.text)
    ]
    delta_frames = [frame for frame in frames if frame[0] == "delta"]
    answer_frame = next(frame for frame in frames if frame[0] == "answer")
    assert delta_frames and delta_frames[-1][1] < answer_frame[1]
    assert "".join(frame[2]["content"] for frame in delta_frames) == joined


def test_think_generation_keeps_buffered_answer_path() -> None:
    provider = _StreamingChatProvider()
    env = build_test_env(provider=provider)
    _, events, _ = _run_generation(env, effort_level="think")

    assert provider.streamed == [False]
    assert _delta_rows(events) == []
    answer = next(event for event in events if event["event_type"] == "answer")
    assert answer["data_json"]["content"].startswith("answer for ")


def test_delta_sink_boundary_is_quick_non_ab_only() -> None:
    env = build_test_env()
    worker = env["runtime"].resolve("chat_generation_worker")

    def sink(*, effort: str, config_versions: tuple[str, str] | None) -> Any:
        return worker._open_delta_sink(
            generation={"id": "g1", "effective_effort_level": effort},
            execution_id="exec_1",
            fencing_token=1,
            control_version=1,
            candidate_config_versions=config_versions,
        )

    assert sink(effort="quick", config_versions=None) is not None
    assert sink(effort="think", config_versions=None) is None
    assert sink(effort="deep", config_versions=None) is None
    # A/B 双盲候选不提前透出正文。
    assert sink(effort="quick", config_versions=("v0", "v1")) is None


def test_delta_sink_aggregates_by_size_threshold_and_drains_on_flush() -> None:
    emissions: list[str] = []
    base = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)
    sink = worker_module._DeltaStreamSink(emit=emissions.append, clock=lambda: base)

    for _ in range(23):
        sink("x" * 10)  # 230 字符：低于 240 阈值，时钟不推进 → 全部缓冲
    assert emissions == []
    sink("x" * 10)  # 累计 240 字符 → 触发 size flush
    assert emissions == ["x" * 240]
    sink("tail")
    sink.flush()  # 残留在 generation 收尾时排空
    assert emissions == ["x" * 240, "tail"]


def test_delta_sink_flushes_by_time_threshold() -> None:
    emissions: list[str] = []
    base = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)
    state = {"ms": 0}

    def clock() -> datetime:
        state["ms"] += 100
        return base + timedelta(milliseconds=state["ms"])

    sink = worker_module._DeltaStreamSink(emit=emissions.append, clock=clock)
    sink("a")  # 100ms since init: < 400ms → 缓冲
    sink("b")  # 200ms
    sink("c")  # 300ms
    sink("d")  # 400ms ≥ 400ms → 时间 flush "abcd"
    assert emissions == ["abcd"]
    sink("e")
    sink.flush()
    assert emissions == ["abcd", "e"]
