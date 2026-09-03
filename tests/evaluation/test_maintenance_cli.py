"""Behavior tests for the protected evaluation maintenance CLI trigger layer.

The API process never runs a business scheduler: an external CronJob invokes
the once entry point with the maintenance key. These tests exercise that
trigger layer against a fake runtime: a missing key must reject before any
runtime resolution (no side effects), and a valid key must claim shadow runs
until the worker reports none processed and then run one calibration-close
pass. The shadow/calibration workers themselves are covered by the evaluation
worker tests.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.evaluation.maintenance import run_evaluation_maintenance_once
from tests._support import make_settings


class _FakeShadowWorker:
    def __init__(self, outcomes: list[SimpleNamespace]) -> None:
        self._outcomes = list(outcomes)
        self.run_once_calls = 0

    def run_once(self) -> SimpleNamespace:
        self.run_once_calls += 1
        if self._outcomes:
            return self._outcomes.pop(0)
        return SimpleNamespace(runs_processed=0, runs_requeued=0, runs_failed=0)


class _FakeCloseWorker:
    def __init__(self) -> None:
        self.run_once_calls = 0

    def run_once(self) -> int:
        self.run_once_calls += 1
        return 1


class _FakeRuntime:
    def __init__(self, shadow_worker: _FakeShadowWorker, close_worker: _FakeCloseWorker) -> None:
        self._workers = {
            "evaluation_worker": shadow_worker,
            "calibration_close_worker": close_worker,
        }
        self.resolved: list[str] = []
        self.closed = False

    def resolve(self, name: str):
        self.resolved.append(name)
        return self._workers[name]

    def close(self) -> None:
        self.closed = True


def test_run_evaluation_maintenance_once_rejects_missing_key_without_side_effects() -> None:
    runtime = _FakeRuntime(_FakeShadowWorker([]), _FakeCloseWorker())
    with pytest.raises(ValueError, match="RAG_MAINTENANCE_KEY is required"):
        run_evaluation_maintenance_once(make_settings(), runtime=runtime)
    assert runtime.resolved == []
    assert runtime.closed is False


def test_run_evaluation_maintenance_once_processes_runs_and_closes_windows() -> None:
    shadow = _FakeShadowWorker(
        [
            SimpleNamespace(runs_processed=2, runs_requeued=1, runs_failed=0),
            SimpleNamespace(runs_processed=0, runs_requeued=0, runs_failed=0),
        ]
    )
    close = _FakeCloseWorker()
    runtime = _FakeRuntime(shadow, close)
    stats = run_evaluation_maintenance_once(
        make_settings(RAG_MAINTENANCE_KEY="evaluation-cli-test-key"), runtime=runtime
    )
    assert stats.runs_processed == 2
    assert stats.runs_requeued == 1
    assert stats.runs_failed == 0
    assert stats.windows_closed == 1
    assert shadow.run_once_calls == 2
    assert close.run_once_calls == 1
    assert runtime.resolved == ["evaluation_worker", "calibration_close_worker"]
    assert runtime.closed is False  # caller-provided runtimes are never closed
