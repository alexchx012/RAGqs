from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from app.platform.observability import ObservabilitySample

PROVIDER_ANALYZER_PROBE_ROUTE = "index_provider_analyzer_probe"
CANDIDATE_FILTER_ROUTE = "index_candidate_filter"
CANDIDATE_REPLENISH_ROUTE = "index_candidate_replenish"
COMPONENT_PUBLISH_FAILURE_ROUTE = "index_component_publish_failure"
COMPONENT_ROLLBACK_FAILURE_ROUTE = "index_component_rollback_failure"
COMPONENT_GC_FAILURE_ROUTE = "index_component_gc_failure"

INDEX_INTERNAL_OBSERVABILITY_ROUTES = frozenset(
    {
        PROVIDER_ANALYZER_PROBE_ROUTE,
        CANDIDATE_FILTER_ROUTE,
        CANDIDATE_REPLENISH_ROUTE,
        COMPONENT_PUBLISH_FAILURE_ROUTE,
        COMPONENT_ROLLBACK_FAILURE_ROUTE,
        COMPONENT_GC_FAILURE_ROUTE,
    }
)


def record_index_observation(
    metrics: Any | None,
    route_template: str,
    *,
    success: bool,
    count: int = 1,
) -> None:
    if metrics is None or count < 1:
        return
    try:
        metrics.record(
            ObservabilitySample(
                observed_at_utc=datetime.now(UTC),
                route_template=route_template,
                method="POST",
                outcome_class="success" if success else "server_error",
                status_family="2xx" if success else "5xx",
                latency_ms=0,
                sample_weight=float(count),
            )
        )
    except Exception:
        return


__all__ = [
    "CANDIDATE_FILTER_ROUTE",
    "CANDIDATE_REPLENISH_ROUTE",
    "COMPONENT_GC_FAILURE_ROUTE",
    "COMPONENT_PUBLISH_FAILURE_ROUTE",
    "COMPONENT_ROLLBACK_FAILURE_ROUTE",
    "INDEX_INTERNAL_OBSERVABILITY_ROUTES",
    "PROVIDER_ANALYZER_PROBE_ROUTE",
    "record_index_observation",
]
