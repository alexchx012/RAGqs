"""Behavior tests for the protected chat maintenance CLI trigger layer.

The API process never runs a business scheduler: an external CronJob invokes
the once entry point with the maintenance key. These tests exercise that
trigger layer against a fake runtime: a missing key must reject before any
runtime resolution (no side effects), and a valid key must drive the worker
maintenance pass and run-once claims without closing a caller-provided
runtime. The reaper/worker logic itself is covered by the chat worker tests.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.chat.maintenance import run_chat_maintenance_once
from tests._support import make_settings


class _FakeChatWorker:
    def __init__(self) -> None:
        self.maintenance_runs = 0
        self.run_once_calls = 0

    def run_maintenance(self) -> dict[str, int]:
        self.maintenance_runs += 1
        return {"expired_lease_renewals": 1}

    def run_once(self) -> SimpleNamespace:
        self.run_once_calls += 1
        if self.run_once_calls == 1:
            return SimpleNamespace(executed="chat_generation_1")
        return SimpleNamespace(executed=None)


class _FakeRuntime:
    def __init__(self, worker: _FakeChatWorker) -> None:
        self._worker = worker
        self.resolved: list[str] = []
        self.closed = False

    def resolve(self, name: str) -> _FakeChatWorker:
        self.resolved.append(name)
        return self._worker

    def close(self) -> None:
        self.closed = True


def test_run_chat_maintenance_once_rejects_missing_key_without_side_effects() -> None:
    runtime = _FakeRuntime(_FakeChatWorker())
    with pytest.raises(ValueError, match="RAG_MAINTENANCE_KEY is required"):
        run_chat_maintenance_once(make_settings(), runtime=runtime)
    assert runtime.resolved == []
    assert runtime.closed is False


def test_run_chat_maintenance_once_runs_maintenance_and_claims_executions() -> None:
    worker = _FakeChatWorker()
    runtime = _FakeRuntime(worker)
    stats = run_chat_maintenance_once(
        make_settings(RAG_MAINTENANCE_KEY="chat-cli-test-key"), runtime=runtime
    )
    assert stats.maintenance == {"expired_lease_renewals": 1}
    assert stats.executions_claimed == 1
    assert worker.maintenance_runs == 1
    assert worker.run_once_calls == 2
    assert runtime.resolved == ["chat_generation_worker"]
    assert runtime.closed is False  # caller-provided runtimes are never closed
