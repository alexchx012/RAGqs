"""Backup write gate middleware contract (Q7).

While the persisted gate is closing/closed, non-exempt business writes are
rejected with 503 backup_in_progress; reads, health/metrics/ops/auth stay
available. The in-process tracker counts admitted writes so the backup worker
can drain them before the gate closes.
"""

from __future__ import annotations

from datetime import UTC, datetime

from _helpers import build_engine, build_identity_service, make_settings
from fastapi.testclient import TestClient

from app.backup.schema import backup_metadata, backup_write_gate_table
from app.backup.write_gate import BackupWriteGateReader, InFlightWriteTracker
from app.platform.app_factory import create_platform_app
from app.platform.runtime import build_runtime

GATE_NOW = datetime(2026, 8, 25, 12, 0, 0, tzinfo=UTC)


def make_app():
    configured = make_settings()
    engine = build_engine()
    backup_metadata.create_all(engine)
    identity = build_identity_service(engine)
    gate_reader = BackupWriteGateReader(engine, cache_ttl_seconds=0)
    runtime = build_runtime(
        configured,
        adapters={
            "database_engine": engine,
            "identity_access": identity,
            "backup_write_gate_reader": gate_reader,
        },
    )
    app = create_platform_app(configured, runtime=runtime)
    return app, engine, runtime


def _set_gate(engine, state: str | None) -> None:
    with engine.begin() as connection:
        connection.execute(backup_write_gate_table.delete())
        if state is not None:
            connection.execute(
                backup_write_gate_table.insert().values(
                    id=1, state=state, backup_id="backup_x", updated_at_utc=GATE_NOW
                )
            )


def _business_write(client):
    return client.post("/v1/documents/doc_1/reindex", json={"expected_version": 1})


def test_open_gate_lets_writes_through() -> None:
    app, engine, runtime = make_app()
    _set_gate(engine, "open")
    with TestClient(app) as client:
        response = _business_write(client)
    # The gate passed the request through to the route, which rejects the
    # unauthenticated caller.
    assert response.status_code == 401
    assert runtime.resolve("backup_write_tracker").in_flight == 0


def test_missing_gate_row_is_open() -> None:
    app, _, _ = make_app()
    with TestClient(app) as client:
        response = _business_write(client)
    assert response.status_code == 401


def test_closing_and_closed_reject_business_writes() -> None:
    app, engine, runtime = make_app()
    tracker = runtime.resolve("backup_write_tracker")
    with TestClient(app) as client:
        for state in ("closing", "closed"):
            _set_gate(engine, state)
            response = _business_write(client)
            assert response.status_code == 503
            assert response.json()["error"]["code"] == "backup_in_progress"
            # Rejected (or admitted) writes never leak the in-flight count.
            assert tracker.in_flight == 0


def test_closed_gate_keeps_reads_and_exempt_prefixes_available() -> None:
    app, engine, _ = make_app()
    _set_gate(engine, "closed")
    with TestClient(app) as client:
        read = client.get("/v1/documents/doc_1/preview")
        health = client.get("/v1/health")
        login = client.post("/v1/auth/login", json={"username": "x", "password": "y"})
        ops_write = client.post("/v1/ops/backups")

    # Reads and exempt prefixes reach their routes (401/200), never 503.
    assert read.status_code == 401
    assert health.status_code == 200
    assert login.status_code == 401
    assert ops_write.status_code == 401
    for response in (read, login, ops_write):
        assert response.json()["error"]["code"] != "backup_in_progress"


def test_in_flight_tracker_counts_and_drains() -> None:
    tracker = InFlightWriteTracker()
    assert tracker.in_flight == 0
    tracker.inc()
    tracker.inc()
    assert tracker.in_flight == 2
    tracker.dec()
    assert tracker.in_flight == 1
    with tracker.track():
        assert tracker.in_flight == 2
    assert tracker.in_flight == 1
    tracker.dec()
    tracker.dec()  # floor at zero: a double-release must not go negative
    assert tracker.in_flight == 0


def test_write_gate_reader_fail_open_on_error() -> None:
    class _BrokenEngine:
        def connect(self):
            raise RuntimeError("database down")

    reader = BackupWriteGateReader(_BrokenEngine(), cache_ttl_seconds=0)
    assert reader.state() == "open"
    assert reader.writes_open() is True
