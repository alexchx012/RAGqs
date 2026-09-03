"""Behavior tests for the protected documents maintenance CLI trigger layer.

The API process never runs a business scheduler: an external CronJob invokes
the once entry point with the maintenance key. These tests exercise that
trigger layer against a fake runtime: a missing key must reject before any
runtime resolution (no side effects), and a valid key must hand the limit to
the documents service and report the cleaned count. The cleanup query itself
is covered by the documents domain tests.
"""

from __future__ import annotations

import pytest

from app.documents.maintenance import run_documents_maintenance_once
from tests._support import make_settings


class _FakeDocumentsService:
    def __init__(self, cleaned: list[str]) -> None:
        self._cleaned = cleaned
        self.limits: list[int] = []

    def cleanup_scheduled_submissions(self, *, limit: int) -> list[str]:
        self.limits.append(limit)
        return self._cleaned[:limit]


class _FakeRuntime:
    def __init__(self, service: _FakeDocumentsService) -> None:
        self._service = service
        self.resolved: list[str] = []
        self.closed = False

    def resolve(self, name: str) -> _FakeDocumentsService:
        self.resolved.append(name)
        return self._service

    def close(self) -> None:
        self.closed = True


def test_run_documents_maintenance_once_rejects_missing_key_without_side_effects() -> None:
    service = _FakeDocumentsService(["submission_1"])
    runtime = _FakeRuntime(service)
    with pytest.raises(ValueError, match="RAG_MAINTENANCE_KEY is required"):
        run_documents_maintenance_once(make_settings(), runtime=runtime)
    assert runtime.resolved == []
    assert service.limits == []
    assert runtime.closed is False


def test_run_documents_maintenance_once_cleans_scheduled_submissions() -> None:
    service = _FakeDocumentsService(["submission_1", "submission_2"])
    runtime = _FakeRuntime(service)
    stats = run_documents_maintenance_once(
        make_settings(RAG_MAINTENANCE_KEY="documents-cli-test-key"),
        runtime=runtime,
        limit=5,
    )
    assert stats.cleaned == 2
    assert service.limits == [5]
    assert runtime.resolved == ["documents_service"]
    assert runtime.closed is False  # caller-provided runtimes are never closed
