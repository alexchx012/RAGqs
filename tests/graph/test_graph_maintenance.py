"""Tests for the public graph build maintenance entry point."""

from __future__ import annotations

import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import SecretStr

import app.graph.maintenance as maintenance
from app.graph.maintenance import run_graph_maintenance_once
from app.graph.worker import GraphBuildWorkerStats


class _RecordingWorker:
    def __init__(self) -> None:
        self.calls = 0

    def run_once(self) -> GraphBuildWorkerStats:
        self.calls += 1
        if self.calls == 1:
            return GraphBuildWorkerStats(builds_processed=1, runs_requeued=2, runs_failed=1)
        return GraphBuildWorkerStats()


class _RecordingRuntime:
    def __init__(self, worker: _RecordingWorker) -> None:
        self._worker = worker
        self.closed = False

    def resolve(self, name: str) -> _RecordingWorker:
        assert name == "graph_build_worker"
        return self._worker

    def close(self) -> None:
        self.closed = True


def test_run_graph_maintenance_once_processes_until_worker_is_idle() -> None:
    worker = _RecordingWorker()
    runtime = _RecordingRuntime(worker)
    settings = SimpleNamespace(maintenance_key=SecretStr("maintenance-key"))

    stats = run_graph_maintenance_once(settings, runtime=runtime, max_builds=10)

    assert stats.builds_processed == 1
    assert stats.runs_requeued == 2
    assert stats.runs_failed == 1
    assert worker.calls == 2
    assert runtime.closed is False


def test_run_graph_maintenance_requires_a_nonblank_maintenance_key() -> None:
    settings = SimpleNamespace(maintenance_key=SecretStr("   "))

    with pytest.raises(ValueError, match="RAG_MAINTENANCE_KEY is required"):
        run_graph_maintenance_once(settings, runtime=_RecordingRuntime(_RecordingWorker()))


def test_run_graph_maintenance_closes_a_runtime_it_creates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = _RecordingWorker()
    runtime = _RecordingRuntime(worker)
    settings = SimpleNamespace(maintenance_key=SecretStr("maintenance-key"))
    monkeypatch.setattr(maintenance, "build_runtime", lambda _settings: runtime)

    run_graph_maintenance_once(settings, max_builds=1)

    assert worker.calls == 1
    assert runtime.closed is True


def test_graph_maintenance_cli_rejects_a_missing_maintenance_key(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        maintenance, "load_platform_settings", lambda: SimpleNamespace(maintenance_key=None)
    )

    with pytest.raises(SystemExit) as exit_error:
        maintenance.main([])

    assert exit_error.value.code == 2
    assert "RAG_MAINTENANCE_KEY is required" in capsys.readouterr().err


def test_graph_maintenance_console_script_is_registered() -> None:
    project = tomllib.loads(
        (Path(__file__).resolve().parents[2] / "pyproject.toml").read_text(encoding="utf-8")
    )

    assert project["project"]["scripts"]["ragqs-graph-maintenance"] == "app.graph.maintenance:main"
