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

from app.chat.maintenance import main, run_chat_maintenance_once
from app.platform.errors import PlatformError
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


def _patch_main(monkeypatch, settings, runtime, passes, sleeps):
    """Wire main() to fakes: load_platform_settings/build_runtime/passes/sleep recording.
    Returns the list of settings passed to build_runtime."""

    built: list[object] = []

    def fake_run_pass(run_settings, *, runtime=None):
        assert run_settings is settings
        passes.append(runtime)
        if len(passes) == 3:
            raise KeyboardInterrupt
        return SimpleNamespace(executions_claimed=1, maintenance={})

    monkeypatch.setattr("app.chat.maintenance.load_platform_settings", lambda: settings)
    monkeypatch.setattr("app.chat.maintenance.build_runtime", lambda s: built.append(s) or runtime)
    monkeypatch.setattr("app.chat.maintenance.run_chat_maintenance_once", fake_run_pass)
    monkeypatch.setattr("app.chat.maintenance.time.sleep", lambda seconds: sleeps.append(seconds))
    return built


def test_main_loop_reuses_runtime_and_stops_on_interrupt(monkeypatch) -> None:
    settings = make_settings(RAG_MAINTENANCE_KEY="chat-cli-test-key")
    runtime = _FakeRuntime(_FakeChatWorker())
    passes: list[object] = []
    sleeps: list[float] = []
    built = _patch_main(monkeypatch, settings, runtime, passes, sleeps)

    main(["--loop", "--interval", "3"])

    assert passes == [runtime, runtime, runtime]  # one runtime reused across passes
    assert built == [settings]  # runtime built exactly once, outside the loop
    assert sleeps == [3, 3]
    assert runtime.closed is True


def test_main_loop_survives_transient_platform_error(monkeypatch) -> None:
    settings = make_settings(RAG_MAINTENANCE_KEY="chat-cli-test-key")
    runtime = _FakeRuntime(_FakeChatWorker())
    passes: list[object] = []
    sleeps: list[float] = []
    failures = {"first": True}
    monkeypatch.setattr("app.chat.maintenance.load_platform_settings", lambda: settings)
    monkeypatch.setattr("app.chat.maintenance.build_runtime", lambda s: runtime)

    def flaky_pass(run_settings, *, runtime=None):
        passes.append(runtime)
        if failures["first"]:
            failures["first"] = False
            raise PlatformError("provider_unavailable", "storage blip", {}, 503)
        if len(passes) >= 3:
            raise KeyboardInterrupt
        return SimpleNamespace(executions_claimed=0, maintenance={})

    monkeypatch.setattr("app.chat.maintenance.run_chat_maintenance_once", flaky_pass)
    monkeypatch.setattr("app.chat.maintenance.time.sleep", lambda seconds: sleeps.append(seconds))

    main(["--loop"])

    assert len(passes) == 3  # the failed pass did not terminate the loop
    assert runtime.closed is True


def test_main_one_shot_keeps_single_pass_without_building_runtime(monkeypatch) -> None:
    settings = make_settings(RAG_MAINTENANCE_KEY="chat-cli-test-key")
    calls: list[object] = []
    built: list[object] = []
    monkeypatch.setattr("app.chat.maintenance.load_platform_settings", lambda: settings)
    monkeypatch.setattr("app.chat.maintenance.build_runtime", lambda s: built.append(s))
    monkeypatch.setattr(
        "app.chat.maintenance.run_chat_maintenance_once",
        lambda run_settings, *, runtime=None: calls.append(runtime)
        or SimpleNamespace(executions_claimed=0, maintenance={}),
    )

    main([])

    assert calls == [None]  # one-shot delegates runtime ownership to run_chat_maintenance_once
    assert built == []
