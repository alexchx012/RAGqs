from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import create_engine, event

from app.platform.database import core_metadata
from app.platform.observability import ObservabilitySample, SqlAlchemyObservabilityMetrics

NOW = datetime(2026, 8, 5, 12, 34, 56, tzinfo=UTC)


def _sample(**overrides) -> ObservabilitySample:
    values = {
        "observed_at_utc": NOW - timedelta(minutes=1),
        "route_template": "/v1/health",
        "method": "GET",
        "outcome_class": "success",
        "status_family": "2xx",
        "latency_ms": 100,
        "sample_weight": 1.0,
    }
    values.update(overrides)
    return ObservabilitySample(**values)


def test_sql_metrics_record_runs_no_cardinality_guard_selects() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    core_metadata.create_all(engine)
    metrics = SqlAlchemyObservabilityMetrics(
        engine,
        now=lambda: NOW,
        allowed_route_templates={"/v1/health"},
    )

    select_count = [0]

    @event.listens_for(engine, "before_cursor_execute")
    def count_selects(conn, cursor, statement, parameters, context, executemany):
        del conn, cursor, parameters, context, executemany
        if statement.lstrip().upper().startswith("SELECT"):
            select_count[0] += 1

    metrics.record(_sample())
    engine.dispose()

    assert select_count[0] == 0


def test_configure_route_templates_truncates_once_at_startup(caplog) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    core_metadata.create_all(engine)
    metrics = SqlAlchemyObservabilityMetrics(engine, now=lambda: NOW, max_route_templates=2)

    with caplog.at_level(logging.WARNING, logger="app.platform.observability"):
        metrics.configure_route_templates(["/v1/a", "/v1/b", "/v1/c"])

    assert metrics.allowed_route_templates == frozenset({"/v1/a", "/v1/b"})
    assert any("max_route_templates" in record.message for record in caplog.records)

    sanitized = metrics._sanitize(_sample(route_template="/v1/c"))
    engine.dispose()
    assert sanitized.route_template == "other"
