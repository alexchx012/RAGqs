from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import create_engine, select

from app.platform.database import (
    core_metadata,
    platform_observability_aggregate_table,
    platform_observability_sample_table,
)
from app.platform.observability import (
    ObservabilityReadRequest,
    ObservabilitySample,
    SqlAlchemyObservabilityMetrics,
)

NOW = datetime(2026, 8, 5, 12, 34, 56, tzinfo=UTC)


def sample(
    *,
    observed_at_utc: datetime = NOW - timedelta(minutes=1),
    route_template: str = "/v1/health",
    outcome_class: str = "success",
    status_family: str = "2xx",
    latency_ms: int = 100,
    sample_weight: float = 1.0,
) -> ObservabilitySample:
    return ObservabilitySample(
        observed_at_utc=observed_at_utc,
        route_template=route_template,
        method="GET",
        outcome_class=outcome_class,
        status_family=status_family,
        latency_ms=latency_ms,
        sample_weight=sample_weight,
    )


def test_sql_metrics_persist_weighted_aggregates_and_safe_dimensions() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    core_metadata.create_all(engine)
    metrics = SqlAlchemyObservabilityMetrics(
        engine,
        now=lambda: NOW,
        allowed_route_templates={"/v1/health"},
    )

    metrics.record(sample(latency_ms=100, sample_weight=10))
    metrics.record(
        sample(
            outcome_class="server_error",
            status_family="5xx",
            latency_ms=500,
            sample_weight=2,
        )
    )
    metrics.record(sample(route_template="/v1/users/123?token=secret", sample_weight=3))

    reloaded = SqlAlchemyObservabilityMetrics(
        engine,
        now=lambda: NOW,
        allowed_route_templates={"/v1/health"},
    )
    result = reloaded.read(ObservabilityReadRequest("retention-ops", "ops", "today"))

    assert result.api.sampled_request_weight == 15
    assert result.api.server_error_weight == 2
    assert result.api.error_rate == 2 / 15
    assert result.api.latency.p50_ms == 100
    assert result.api.latency.p95_ms == 500
    assert result.api.latency.p99_ms == 500

    with engine.connect() as connection:
        persisted = connection.execute(select(platform_observability_sample_table)).mappings().all()
        aggregates = (
            connection.execute(select(platform_observability_aggregate_table)).mappings().all()
        )
    assert len(persisted) == 3
    assert aggregates
    assert persisted[-1]["route_template"] == "other"
    assert "secret" not in repr(persisted)
    engine.dispose()


def test_sql_metrics_drop_samples_past_the_frozen_retention_policy() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    core_metadata.create_all(engine)
    metrics = SqlAlchemyObservabilityMetrics(engine, now=lambda: NOW, retention_days=31)

    metrics.record(sample(observed_at_utc=NOW - timedelta(days=32)))
    metrics.record(sample())

    with engine.connect() as connection:
        persisted = connection.execute(select(platform_observability_sample_table)).mappings().all()
    assert len(persisted) == 1
    assert persisted[0]["retention_days"] == 31
    engine.dispose()


def test_sql_metrics_prune_retention_in_maintenance_not_during_request_recording() -> None:
    current = [NOW]
    engine = create_engine("sqlite+pysqlite:///:memory:")
    core_metadata.create_all(engine)
    metrics = SqlAlchemyObservabilityMetrics(engine, now=lambda: current[0], retention_days=31)

    metrics.record(sample(observed_at_utc=current[0]))
    current[0] += timedelta(days=32)
    metrics.record(sample(observed_at_utc=current[0]))

    with engine.connect() as connection:
        assert connection.execute(select(platform_observability_sample_table)).all()
        assert len(connection.execute(select(platform_observability_sample_table)).all()) == 2

    metrics.prune()

    with engine.connect() as connection:
        assert len(connection.execute(select(platform_observability_sample_table)).all()) == 1
    engine.dispose()


def test_sql_metrics_clamp_future_observation_time_to_database_clock() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    core_metadata.create_all(engine)
    metrics = SqlAlchemyObservabilityMetrics(engine, now=lambda: NOW)

    metrics.record(sample(observed_at_utc=NOW + timedelta(days=365)))

    with engine.connect() as connection:
        persisted = connection.execute(select(platform_observability_sample_table)).mappings().one()
    observed_at = persisted["observed_at_utc"].replace(tzinfo=UTC)
    assert observed_at <= NOW
    engine.dispose()
