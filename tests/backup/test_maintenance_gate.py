"""Maintenance gate reader behavior (Q-scope review A36).

The reader serves `reads_closed` from a short in-process cache so the API does
not query the gate table on every request, and fails open when the database is
unreachable. Mirrors the BackupWriteGateReader tests in
tests/backup/test_backup_write_gate.py.
"""

from __future__ import annotations

from types import SimpleNamespace

import app.backup.gate as gate_module
from app.backup.gate import MaintenanceGateReader


class _FakeGateConnection:
    def __init__(self, reads_closed: bool) -> None:
        self._reads_closed = reads_closed

    def execute(self, _statement):
        return SimpleNamespace(scalar_one_or_none=lambda: self._reads_closed)

    def __enter__(self):
        return self

    def __exit__(self, *_exc_info) -> bool:
        return False


class _FakeGateEngine:
    def __init__(self, reads_closed: bool) -> None:
        self.connect_calls = 0
        self.reads_closed = reads_closed

    def connect(self):
        self.connect_calls += 1
        return _FakeGateConnection(self.reads_closed)


class _FakeClock:
    def __init__(self, start: float) -> None:
        self.now = start

    def monotonic(self) -> float:
        return self.now


def test_reads_closed_serves_cache_within_ttl_and_refreshes_after(monkeypatch) -> None:
    engine = _FakeGateEngine(reads_closed=True)
    reader = MaintenanceGateReader(engine, cache_ttl_seconds=1.0)
    clock = _FakeClock(start=100.0)
    monkeypatch.setattr(gate_module, "time", clock)

    assert reader.reads_closed() is True
    assert engine.connect_calls == 1

    # Inside the 1 second cache window the cached answer is served and the
    # gate table is not queried again, even though the persisted state moved.
    clock.now = 100.9
    engine.reads_closed = False
    assert reader.reads_closed() is True
    assert engine.connect_calls == 1

    # Once the TTL lapses the reader queries the database again.
    clock.now = 101.0
    assert reader.reads_closed() is False
    assert engine.connect_calls == 2


def test_reads_closed_fails_open_when_database_is_down() -> None:
    class _BrokenEngine:
        def connect(self):
            raise RuntimeError("database down")

    reader = MaintenanceGateReader(_BrokenEngine(), cache_ttl_seconds=0)
    assert reader.reads_closed() is False
