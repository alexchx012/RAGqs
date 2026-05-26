"""In-process runtime metrics for lightweight operations visibility."""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from threading import Lock
from typing import Any

DEFAULT_LATENCY_BUCKETS_MS = (100, 250, 500, 1000, 2500, 5000)


class RuntimeMetrics:
    """Thread-safe in-process metrics collector for one FastAPI process."""

    def __init__(self, latency_buckets_ms: Sequence[int | float] | None = None):
        buckets = latency_buckets_ms or DEFAULT_LATENCY_BUCKETS_MS
        self.latency_buckets_ms = tuple(sorted({float(bucket) for bucket in buckets}))
        self._lock = Lock()
        self._http_total = 0
        self._http_latency_total_ms = 0.0
        self._http_status_codes: Counter[str] = Counter()
        self._http_routes: dict[str, dict[str, float]] = defaultdict(
            lambda: {"count": 0, "latencyTotalMs": 0.0}
        )
        self._http_latency_buckets: Counter[str] = Counter()
        self._rag_total = 0
        self._rag_successes = 0
        self._rag_failures = 0
        self._rag_latency_total_ms = 0.0
        self._rag_latency_buckets: Counter[str] = Counter()
        self._rag_spaces: Counter[str] = Counter()
        self._token_usage = {"promptTokens": 0, "completionTokens": 0, "totalTokens": 0}

    def record_http_request(
        self,
        *,
        method: str,
        path: str,
        status_code: int,
        latency_ms: int | float,
    ) -> None:
        """Record one completed HTTP request."""

        normalized_latency = _rounded_latency(latency_ms)
        route_key = f"{method.upper()} {path}"
        with self._lock:
            self._http_total += 1
            self._http_latency_total_ms += normalized_latency
            self._http_status_codes[str(status_code)] += 1
            route = self._http_routes[route_key]
            route["count"] += 1
            route["latencyTotalMs"] += normalized_latency
            self._http_latency_buckets[self._bucket_label(normalized_latency)] += 1

    def record_rag_query(
        self,
        *,
        session_id: str,
        space_id: str,
        success: bool,
        latency_ms: int | float,
        token_usage: Mapping[str, Any] | None = None,
    ) -> None:
        """Record one RAG query execution."""

        del session_id
        normalized_latency = _rounded_latency(latency_ms)
        usage = normalize_token_usage(token_usage or {})
        normalized_space_id = str(space_id or "default")
        with self._lock:
            self._rag_total += 1
            if success:
                self._rag_successes += 1
            else:
                self._rag_failures += 1
            self._rag_latency_total_ms += normalized_latency
            self._rag_latency_buckets[self._bucket_label(normalized_latency)] += 1
            self._rag_spaces[normalized_space_id] += 1
            for key, value in usage.items():
                self._token_usage[key] += value

    def snapshot(self) -> dict[str, Any]:
        """Return a JSON-serializable metrics snapshot."""

        with self._lock:
            return {
                "http": {
                    "totalRequests": self._http_total,
                    "statusCodes": dict(self._http_status_codes),
                    "routes": {
                        route: {
                            "count": int(values["count"]),
                            "averageLatencyMs": _rounded_average(
                                values["latencyTotalMs"], values["count"]
                            ),
                        }
                        for route, values in self._http_routes.items()
                    },
                    "latencyBucketsMs": self._bucket_snapshot(self._http_latency_buckets),
                    "averageLatencyMs": _rounded_average(
                        self._http_latency_total_ms, self._http_total
                    ),
                },
                "rag": {
                    "totalQueries": self._rag_total,
                    "successes": self._rag_successes,
                    "failures": self._rag_failures,
                    "spaces": dict(self._rag_spaces),
                    "latencyBucketsMs": self._bucket_snapshot(self._rag_latency_buckets),
                    "averageLatencyMs": _rounded_average(
                        self._rag_latency_total_ms, self._rag_total
                    ),
                    "tokenUsage": dict(self._token_usage),
                },
            }

    def reset(self) -> None:
        """Clear collected metrics."""

        with self._lock:
            self.__init__(latency_buckets_ms=self.latency_buckets_ms)

    def _bucket_label(self, latency_ms: float) -> str:
        for bucket in self.latency_buckets_ms:
            if latency_ms <= bucket:
                return f"<={_format_bucket(bucket)}"
        return f">{_format_bucket(self.latency_buckets_ms[-1])}"

    def _bucket_snapshot(self, counter: Counter[str]) -> dict[str, int]:
        labels = [f"<={_format_bucket(bucket)}" for bucket in self.latency_buckets_ms]
        labels.append(f">{_format_bucket(self.latency_buckets_ms[-1])}")
        return {label: counter.get(label, 0) for label in labels}


def normalize_token_usage(token_usage: Mapping[str, Any]) -> dict[str, int]:
    """Normalize provider-specific token usage keys to one public shape."""

    prompt_tokens = _token_value(
        token_usage,
        "promptTokens",
        "prompt_tokens",
        "inputTokens",
        "input_tokens",
    )
    completion_tokens = _token_value(
        token_usage,
        "completionTokens",
        "completion_tokens",
        "outputTokens",
        "output_tokens",
    )
    total_tokens = _token_value(token_usage, "totalTokens", "total_tokens")
    if total_tokens == 0 and (prompt_tokens or completion_tokens):
        total_tokens = prompt_tokens + completion_tokens
    return {
        "promptTokens": prompt_tokens,
        "completionTokens": completion_tokens,
        "totalTokens": total_tokens,
    }


def render_prometheus_metrics(snapshot: Mapping[str, Any]) -> str:
    """Render a runtime metrics snapshot as Prometheus text exposition."""

    http = _mapping(snapshot.get("http"))
    rag = _mapping(snapshot.get("rag"))
    lines: list[str] = []

    _append_metric(
        lines,
        "ragqs_http_requests_total",
        "Total HTTP requests handled by this process.",
        "counter",
        _number(http.get("totalRequests")),
    )
    _append_labeled_metrics(
        lines,
        "ragqs_http_status_codes_total",
        "HTTP requests by status code.",
        "counter",
        _mapping(http.get("statusCodes")),
        "status_code",
    )
    _append_route_metrics(lines, _mapping(http.get("routes")))
    _append_labeled_metrics(
        lines,
        "ragqs_http_latency_bucket_count",
        "HTTP request latency bucket counts in milliseconds.",
        "gauge",
        _mapping(http.get("latencyBucketsMs")),
        "bucket",
    )
    _append_metric(
        lines,
        "ragqs_http_latency_average_ms",
        "Average HTTP request latency in milliseconds.",
        "gauge",
        _number(http.get("averageLatencyMs")),
    )

    _append_metric(
        lines,
        "ragqs_rag_queries_total",
        "Total RAG queries handled by this process.",
        "counter",
        _number(rag.get("totalQueries")),
    )
    _append_metric(
        lines,
        "ragqs_rag_query_success_total",
        "Successful RAG queries handled by this process.",
        "counter",
        _number(rag.get("successes")),
    )
    _append_metric(
        lines,
        "ragqs_rag_query_failure_total",
        "Failed RAG queries handled by this process.",
        "counter",
        _number(rag.get("failures")),
    )
    _append_labeled_metrics(
        lines,
        "ragqs_rag_space_queries_total",
        "RAG queries by knowledge space.",
        "counter",
        _mapping(rag.get("spaces")),
        "space_id",
    )
    _append_labeled_metrics(
        lines,
        "ragqs_rag_latency_bucket_count",
        "RAG query latency bucket counts in milliseconds.",
        "gauge",
        _mapping(rag.get("latencyBucketsMs")),
        "bucket",
    )
    _append_metric(
        lines,
        "ragqs_rag_latency_average_ms",
        "Average RAG query latency in milliseconds.",
        "gauge",
        _number(rag.get("averageLatencyMs")),
    )
    _append_token_usage_metrics(lines, _mapping(rag.get("tokenUsage")))

    return "\n".join(lines) + "\n"


def _token_value(token_usage: Mapping[str, Any], *keys: str) -> int:
    for key in keys:
        value = token_usage.get(key)
        if value is not None:
            try:
                return max(0, int(value))
            except (TypeError, ValueError):
                return 0
    return 0


def _append_route_metrics(lines: list[str], routes: Mapping[str, Any]) -> None:
    lines.extend(
        [
            "# HELP ragqs_http_route_requests_total HTTP requests by route.",
            "# TYPE ragqs_http_route_requests_total counter",
        ]
    )
    for route, values in _sorted_metric_items(routes):
        route_values = _mapping(values)
        lines.append(
            f'ragqs_http_route_requests_total{{route="{_escape_label_value(route)}"}} '
            f'{_number(route_values.get("count"))}'
        )

    lines.extend(
        [
            "# HELP ragqs_http_route_latency_average_ms Average HTTP route latency in milliseconds.",
            "# TYPE ragqs_http_route_latency_average_ms gauge",
        ]
    )
    for route, values in _sorted_metric_items(routes):
        route_values = _mapping(values)
        lines.append(
            f'ragqs_http_route_latency_average_ms{{route="{_escape_label_value(route)}"}} '
            f'{_number(route_values.get("averageLatencyMs"))}'
        )


def _append_token_usage_metrics(lines: list[str], token_usage: Mapping[str, Any]) -> None:
    token_types = {
        "prompt": token_usage.get("promptTokens", 0),
        "completion": token_usage.get("completionTokens", 0),
        "total": token_usage.get("totalTokens", 0),
    }
    _append_labeled_metrics(
        lines,
        "ragqs_rag_token_usage_total",
        "Total RAG token usage reported by providers.",
        "counter",
        token_types,
        "type",
    )


def _append_metric(
    lines: list[str],
    name: str,
    help_text: str,
    metric_type: str,
    value: int | float,
) -> None:
    lines.extend([f"# HELP {name} {help_text}", f"# TYPE {name} {metric_type}", f"{name} {value}"])


def _append_labeled_metrics(
    lines: list[str],
    name: str,
    help_text: str,
    metric_type: str,
    values: Mapping[str, Any],
    label_name: str,
) -> None:
    lines.extend([f"# HELP {name} {help_text}", f"# TYPE {name} {metric_type}"])
    for label_value, value in _sorted_metric_items(values):
        lines.append(
            f'{name}{{{label_name}="{_escape_label_value(label_value)}"}} {_number(value)}'
        )


def _sorted_metric_items(values: Mapping[str, Any]) -> list[tuple[Any, Any]]:
    return sorted(values.items(), key=lambda item: str(item[0]))


def _mapping(value: Any) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    return {}


def _number(value: Any) -> int | float:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int | float):
        return value
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0
    if number.is_integer():
        return int(number)
    return number


def _escape_label_value(value: Any) -> str:
    return str(value).replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _rounded_latency(value: int | float) -> float:
    return round(float(value), 3)


def _rounded_average(total: float, count: int | float) -> float:
    if not count:
        return 0.0
    return round(total / count, 3)


def _format_bucket(bucket: float) -> str:
    if bucket.is_integer():
        return str(int(bucket))
    return str(bucket)


runtime_metrics = RuntimeMetrics()
