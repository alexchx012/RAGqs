"""Immutable versioned ``retrieval_release_gate`` configuration (design §7.4.1).

与价格目录同模式：gate 版本由部署侧发布（无运行时写入口），insert-only、
发布后不得原地修改，新要求发新版本；开放版本（``effective_to_utc IS NULL``）
至多一个，由注册事务在关闭前任版本后串行保证。每项指标固定方向
（above/below）、绝对门槛、相对允许回退、最小样本数、聚合方法与严重度；
gate 级固定目标硬件 profile 与并发。发布验收 run 引用具体 gate 版本并按其判定。
"""

from __future__ import annotations

import secrets
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Engine, select, update

from app.platform.errors import PlatformError

from .schema import retrieval_release_gate_metric_table, retrieval_release_gate_table

# 延迟/错误/显存指标与质量指标是发布验收的完整指标集（suite 与 gate 共用）。
REQUIRED_LATENCY_METRICS: frozenset[str] = frozenset(
    {"p50_ms", "p95_ms", "p99_ms", "error_rate", "vram_mb"}
)
REQUIRED_QUALITY_METRICS: frozenset[str] = frozenset({"hit_at_k", "mrr", "ndcg", "refusal"})
GATE_REQUIRED_METRICS: frozenset[str] = REQUIRED_LATENCY_METRICS | REQUIRED_QUALITY_METRICS
# 指标语义决定合法方向：质量指标必须向 above（不低于门槛），延迟/错误/显存
# 指标必须向 below（不超过门槛）。方向是配置数据，但与指标语义矛盾的部署
# 会让闸门判定反向，注册时直接拒绝。
_METRIC_DIRECTIONS: dict[str, str] = {
    **{name: "below" for name in REQUIRED_LATENCY_METRICS},
    **{name: "above" for name in REQUIRED_QUALITY_METRICS},
}
GATE_AGGREGATIONS = frozenset({"mean", "max", "p50", "p95", "p99", "rate"})
GATE_SEVERITIES = frozenset({"blocking", "advisory"})


def _as_utc(value: datetime) -> datetime:
    # SQLite 丢时区信息，读出后统一补 UTC 再比较/返回。
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def _gate_id() -> str:
    return f"retrieval_release_gate_{secrets.token_urlsafe(12)}"


def _validate_metric_entries(metrics: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    if not isinstance(metrics, Sequence) or not metrics:
        raise PlatformError(
            "validation_error", "retrieval release gate metrics are required", {}, 422
        )
    seen: dict[str, dict[str, Any]] = {}
    for entry in metrics:
        if not isinstance(entry, Mapping):
            raise PlatformError(
                "validation_error", "retrieval release gate metric is invalid", {}, 422
            )
        name = str(entry.get("metric") or "")
        if name not in GATE_REQUIRED_METRICS:
            raise PlatformError(
                "validation_error",
                "retrieval release gate metric is outside the design-enumerated set",
                {"metric": name},
                422,
            )
        if name in seen:
            raise PlatformError(
                "validation_error",
                "retrieval release gate metric is duplicated",
                {"metric": name},
                422,
            )
        direction = str(entry.get("direction") or "")
        if direction != _METRIC_DIRECTIONS[name]:
            raise PlatformError(
                "validation_error",
                "retrieval release gate direction contradicts the metric semantics",
                {"metric": name, "direction": direction},
                422,
            )
        threshold = entry.get("absolute_threshold")
        if not isinstance(threshold, (int, float)) or not float(threshold) >= 0:
            raise PlatformError(
                "validation_error",
                "retrieval release gate absolute threshold is invalid",
                {"metric": name},
                422,
            )
        regression = entry.get("allowed_regression")
        if not isinstance(regression, (int, float)) or not 0.0 <= float(regression) < 1.0:
            raise PlatformError(
                "validation_error",
                "retrieval release gate allowed regression is invalid",
                {"metric": name},
                422,
            )
        min_samples = entry.get("min_samples")
        if not isinstance(min_samples, int) or isinstance(min_samples, bool) or min_samples < 1:
            raise PlatformError(
                "validation_error",
                "retrieval release gate minimum sample count is invalid",
                {"metric": name},
                422,
            )
        aggregation = str(entry.get("aggregation") or "")
        if aggregation not in GATE_AGGREGATIONS:
            raise PlatformError(
                "validation_error",
                "retrieval release gate aggregation is invalid",
                {"metric": name},
                422,
            )
        severity = str(entry.get("severity") or "")
        if severity not in GATE_SEVERITIES:
            raise PlatformError(
                "validation_error",
                "retrieval release gate severity is invalid",
                {"metric": name},
                422,
            )
        seen[name] = {
            "metric": name,
            "direction": direction,
            "absolute_threshold": float(threshold),
            "allowed_regression": float(regression),
            "min_samples": int(min_samples),
            "aggregation": aggregation,
            "severity": severity,
        }
    missing = sorted(GATE_REQUIRED_METRICS - seen.keys())
    if missing:
        raise PlatformError(
            "validation_error",
            "retrieval release gate is missing required metric gates",
            {"missing_metrics": missing},
            422,
        )
    return [seen[name] for name in sorted(seen)]


def load_gate_version(
    connection: Any, gate_version_id: str
) -> tuple[Mapping[str, Any], tuple[dict[str, Any], ...]]:
    """Read one gate version with its metric entries from an open connection."""

    row = (
        connection.execute(
            select(retrieval_release_gate_table).where(
                retrieval_release_gate_table.c.id == gate_version_id
            )
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        raise PlatformError(
            "validation_error", "retrieval release gate version is unavailable", {}, 422
        )
    metrics = tuple(
        {key: value for key, value in dict(metric).items() if key not in {"id", "gate_version_id"}}
        for metric in connection.execute(
            select(retrieval_release_gate_metric_table)
            .where(retrieval_release_gate_metric_table.c.gate_version_id == gate_version_id)
            .order_by(retrieval_release_gate_metric_table.c.metric)
        ).mappings()
    )
    return dict(row), metrics


class RetrievalReleaseGateService:
    """Deployment-side registration and lookup of immutable gate versions."""

    def __init__(self, engine: Engine, *, now: Callable[[], datetime] | None = None) -> None:
        self._engine = engine
        self._now = now or (lambda: datetime.now(UTC))

    def register(
        self,
        *,
        version: str,
        hardware_profile: Mapping[str, Any],
        concurrency: int,
        metrics: Sequence[Mapping[str, Any]],
        supersedes_version_id: str | None = None,
        effective_from: datetime | None = None,
    ) -> Mapping[str, Any]:
        if not isinstance(version, str) or not version.strip():
            raise PlatformError(
                "validation_error", "retrieval release gate version is required", {}, 422
            )
        if not isinstance(hardware_profile, Mapping) or not hardware_profile:
            raise PlatformError(
                "validation_error", "retrieval release gate hardware profile is required", {}, 422
            )
        if not isinstance(concurrency, int) or isinstance(concurrency, bool) or concurrency < 1:
            raise PlatformError(
                "validation_error", "retrieval release gate concurrency is invalid", {}, 422
            )
        entries = _validate_metric_entries(metrics)
        effective_from_utc = effective_from or self._now()
        if effective_from_utc.tzinfo is None:
            effective_from_utc = effective_from_utc.replace(tzinfo=UTC)
        with self._engine.begin() as connection:
            existing_version = connection.execute(
                select(retrieval_release_gate_table.c.id).where(
                    retrieval_release_gate_table.c.version == version
                )
            ).scalar_one_or_none()
            if existing_version is not None:
                raise PlatformError(
                    "retrieval_release_gate_conflict",
                    "retrieval release gate version already exists",
                    {},
                    409,
                )
            open_row = (
                connection.execute(
                    select(retrieval_release_gate_table).where(
                        retrieval_release_gate_table.c.effective_to_utc.is_(None)
                    )
                )
                .mappings()
                .one_or_none()
            )
            if open_row is None:
                if supersedes_version_id is not None:
                    raise PlatformError(
                        "validation_error",
                        "the first retrieval release gate version supersedes nothing",
                        {},
                        422,
                    )
            else:
                if supersedes_version_id != str(open_row["id"]):
                    raise PlatformError(
                        "retrieval_release_gate_conflict",
                        "a new gate version must supersede the current open version",
                        {"open_version_id": str(open_row["id"])},
                        409,
                    )
                if not effective_from_utc > _as_utc(open_row["effective_from_utc"]):
                    raise PlatformError(
                        "validation_error",
                        "the new gate version must take effect after its predecessor",
                        {},
                        422,
                    )
                closed = connection.execute(
                    update(retrieval_release_gate_table)
                    .where(
                        retrieval_release_gate_table.c.id == str(open_row["id"]),
                        retrieval_release_gate_table.c.effective_to_utc.is_(None),
                    )
                    .values(effective_to_utc=effective_from_utc)
                ).rowcount
                assert closed == 1
            gate_id = _gate_id()
            connection.execute(
                retrieval_release_gate_table.insert().values(
                    id=gate_id,
                    version=version,
                    hardware_profile_json=dict(hardware_profile),
                    concurrency=concurrency,
                    effective_from_utc=effective_from_utc,
                    effective_to_utc=None,
                    supersedes_version_id=supersedes_version_id,
                    created_at_utc=self._now(),
                )
            )
            for entry in entries:
                connection.execute(
                    retrieval_release_gate_metric_table.insert().values(
                        id=f"{gate_id}:{entry['metric']}",
                        gate_version_id=gate_id,
                        **entry,
                    )
                )
            # 不在事务内再开连接回读（SQLite 单连接池下会破坏当前事务）。
            return {
                "id": gate_id,
                "version": version,
                "hardware_profile": dict(hardware_profile),
                "concurrency": concurrency,
                "effective_from_utc": effective_from_utc,
                "effective_to_utc": None,
                "supersedes_version_id": supersedes_version_id,
                "metrics": tuple(entries),
            }

    def get(self, gate_version_id: str) -> Mapping[str, Any]:
        with self._engine.connect() as connection:
            row, metrics = load_gate_version(connection, gate_version_id)
        return {
            "id": str(row["id"]),
            "version": str(row["version"]),
            "hardware_profile": dict(row["hardware_profile_json"] or {}),
            "concurrency": int(row["concurrency"]),
            "effective_from_utc": _as_utc(row["effective_from_utc"]),
            "effective_to_utc": (
                None if row["effective_to_utc"] is None else _as_utc(row["effective_to_utc"])
            ),
            "supersedes_version_id": row["supersedes_version_id"],
            "created_at_utc": _as_utc(row["created_at_utc"]),
            "metrics": tuple(metrics),
        }

    def resolve(self, at: datetime | None = None) -> Mapping[str, Any] | None:
        """Select the gate version whose half-open interval covers ``at``."""

        moment = at or self._now()
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=UTC)
        with self._engine.connect() as connection:
            row = connection.execute(
                select(retrieval_release_gate_table.c.id)
                .where(
                    retrieval_release_gate_table.c.effective_from_utc <= moment,
                    retrieval_release_gate_table.c.effective_to_utc.is_(None)
                    | (retrieval_release_gate_table.c.effective_to_utc > moment),
                )
                .order_by(retrieval_release_gate_table.c.effective_from_utc.desc())
                .limit(1)
            ).scalar_one_or_none()
            if row is None:
                return None
            gate_id = str(row)
            detail, metrics = load_gate_version(connection, gate_id)
        return {
            "id": gate_id,
            "version": str(detail["version"]),
            "hardware_profile": dict(detail["hardware_profile_json"] or {}),
            "concurrency": int(detail["concurrency"]),
            "effective_from_utc": _as_utc(detail["effective_from_utc"]),
            "effective_to_utc": (
                None if detail["effective_to_utc"] is None else _as_utc(detail["effective_to_utc"])
            ),
            "supersedes_version_id": detail["supersedes_version_id"],
            "created_at_utc": _as_utc(detail["created_at_utc"]),
            "metrics": tuple(metrics),
        }
