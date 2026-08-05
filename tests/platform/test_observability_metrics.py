from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.platform.observability import (
    HISTOGRAM_BOUNDARIES_MS,
    InMemoryObservabilityMetrics,
    ObservabilityMetricsError,
    ObservabilityReadRequest,
    ObservabilitySample,
    sample_success,
)

NOW = datetime(2026, 8, 5, 12, 34, 56, tzinfo=UTC)


def port(**kwargs) -> InMemoryObservabilityMetrics:
    return InMemoryObservabilityMetrics(now=lambda: NOW, **kwargs)


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


def test_weighted_error_rate_and_latency_percentiles_use_fixed_window() -> None:
    metrics = port()
    metrics.record(sample(latency_ms=100, sample_weight=10))
    metrics.record(
        sample(
            outcome_class="server_error",
            status_family="5xx",
            latency_ms=500,
            sample_weight=2,
        )
    )
    metrics.record(
        sample(
            outcome_class="validation_error",
            status_family="4xx",
            latency_ms=50,
            sample_weight=3,
        )
    )
    metrics.record(sample(observed_at_utc=NOW - timedelta(days=8), sample_weight=100))

    result = metrics.read(ObservabilityReadRequest("retention-ops", "ops", "7d"))

    assert result.data_state == "available"
    assert result.api.sampled_request_weight == 15
    assert result.api.server_error_weight == 2
    assert result.api.error_rate == pytest.approx(2 / 15)
    assert result.api.latency.p50_ms == 100
    assert result.api.latency.p95_ms == 500
    assert result.api.latency.p99_ms == 500
    assert result.start_at_utc == NOW - timedelta(days=7)
    assert result.end_at_utc == NOW


def test_empty_and_insufficient_samples_do_not_infer_zero() -> None:
    metrics = port(minimum_sample_weight=3)
    empty = metrics.read(ObservabilityReadRequest("retention-ops", "admin", "today"))

    assert empty.data_state == "empty"
    assert empty.api.error_rate is None
    assert empty.api.latency.p50_ms is None

    metrics.record(sample(sample_weight=1))
    insufficient = metrics.read(ObservabilityReadRequest("retention-ops", "admin", "today"))
    assert insufficient.data_state == "insufficient_sample"
    assert insufficient.api.error_rate is None


@pytest.mark.parametrize(
    "read_request",
    [
        ObservabilityReadRequest("client", "ops", "today"),
        ObservabilityReadRequest("retention-ops", "user", "today"),
        ObservabilityReadRequest("retention-ops", "ops", "90d"),
    ],
)
def test_metrics_read_has_caller_audience_and_window_gates(read_request) -> None:
    with pytest.raises(ObservabilityMetricsError) as raised:
        port().read(read_request)
    assert raised.value.status_code in {403, 422}


def test_route_dimensions_are_allowlisted_and_cardinality_is_bounded() -> None:
    metrics = port(allowed_route_templates={"/v1/health"}, max_route_templates=1)
    metrics.record(sample(route_template="/v1/users/123?token=secret"))

    assert metrics.samples[0].route_template == "other"
    assert "secret" not in repr(metrics.samples[0])


def test_sampling_is_deterministic_and_weighted() -> None:
    first = sample_success("request-hash", 0.25)
    second = sample_success("request-hash", 0.25)

    assert first == second
    assert first[1] == (4.0 if first[0] else 0.0)


def test_raw_sample_has_only_allowlisted_dimensions_and_fixed_histogram() -> None:
    assert HISTOGRAM_BOUNDARIES_MS == tuple(sorted(HISTOGRAM_BOUNDARIES_MS))
    assert all(isinstance(value, int) and value > 0 for value in HISTOGRAM_BOUNDARIES_MS)
    sample_fields = set(ObservabilitySample.__dataclass_fields__)
    assert sample_fields == {
        "observed_at_utc",
        "route_template",
        "method",
        "outcome_class",
        "status_family",
        "latency_ms",
        "sample_weight",
    }
