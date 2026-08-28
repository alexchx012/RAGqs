"""Server-driven dashboard, operations and ops-jobs read models.

Card structure is fixed by this module; only owners provide the facts, the
core observability port provides API telemetry, and absent aggregates render
as null/empty without fabrication.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from app.platform.errors import PlatformError
from app.platform.observability import (
    ObservabilityMetricsError,
    ObservabilityMetricsPort,
    ObservabilityReadRequest,
)

from . import facts


def _ratio(value: float, total: float) -> float:
    return round(value / total, 4) if total > 0 else 0.0


def _number(value: Any) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    if isinstance(value, int):
        return value
    return round(value, 4)


def _delta(
    current: float | int | None, previous: float | int | None, *, percent: bool = False
) -> Mapping[str, str] | None:
    """Machine-readable change against the previous equally long window.

    Returns None when either side is absent: an unknown previous value is an
    absent fact, and this module never fabricates a baseline for it.
    """
    if current is None or previous is None:
        return None
    if isinstance(current, bool) or isinstance(previous, bool):
        return None
    diff = round(float(current) - float(previous), 4)
    direction = "up" if diff > 0 else "down" if diff < 0 else "flat"
    if diff == 0:
        text_hint = "0"
    elif percent:
        text_hint = f"{diff * 100:+.1f}%"
    elif float(diff).is_integer():
        text_hint = f"{int(diff):+d}"
    else:
        text_hint = f"{diff:+g}"
    return {"direction": direction, "text_hint": text_hint}


def _delta_from(
    previous: Mapping[str, Any], key: str, value: float | int | None, *, percent: bool = False
) -> Mapping[str, str] | None:
    """Look the previous value up by card key; cards without history stay null."""
    if key not in previous:
        return None
    return _delta(value, previous[key], percent=percent)


def _failure_rate(terminal: Mapping[str, int]) -> float | None:
    total = terminal["succeeded"] + terminal["failed"]
    return round(terminal["failed"] / total, 4) if total > 0 else None


def _stat(
    key: str,
    title: str,
    *,
    value: float | int | None = None,
    delta: Mapping[str, str] | None = None,
    sparkline: list[float] | None = None,
    threshold: Mapping[str, Any] | None = None,
    link: str | None = None,
) -> dict[str, Any]:
    return {
        "key": key,
        "title": title,
        "kind": "stat",
        "value": value,
        "delta": dict(delta) if delta else None,
        "sparkline": list(sparkline or []),
        "threshold": dict(threshold) if threshold else None,
        "link": link,
    }


def _count(
    key: str,
    title: str,
    *,
    value: int | None = None,
    delta: Mapping[str, str] | None = None,
    threshold: Mapping[str, Any] | None = None,
    link: str | None = None,
) -> dict[str, Any]:
    return {
        "key": key,
        "title": title,
        "kind": "count",
        "value": value,
        "delta": dict(delta) if delta else None,
        "threshold": dict(threshold) if threshold else None,
        "link": link,
    }


def _distribution(
    key: str,
    title: str,
    rows: Sequence[Mapping[str, Any]],
    *,
    threshold: Mapping[str, Any] | None = None,
    link: str | None = None,
) -> dict[str, Any]:
    total = sum(float(_number(row["value"])) for row in rows)
    return {
        "key": key,
        "title": title,
        "kind": "distribution",
        "rows": [
            {
                "label": str(row["label"]),
                "value": _number(row["value"]),
                "ratio": _ratio(float(_number(row["value"])), total),
                "tone": str(row.get("tone", "normal")),
            }
            for row in rows
        ],
        "threshold": dict(threshold) if threshold else None,
        "link": link,
    }


def _user_rank(
    key: str,
    title: str,
    rows: Sequence[Mapping[str, Any]],
    *,
    total_count: int,
    threshold: Mapping[str, Any] | None = None,
    link: str | None = None,
) -> dict[str, Any]:
    total = sum(float(_number(row["value"])) for row in rows)
    return {
        "key": key,
        "title": title,
        "kind": "user_rank",
        "rows": [
            {
                "label": str(row["label"]),
                "value": _number(row["value"]),
                "ratio": _ratio(float(_number(row["value"])), total),
            }
            for row in rows
        ],
        "total_count": total_count,
        "threshold": dict(threshold) if threshold else None,
        "link": link,
    }


class DashboardReadModels:
    def __init__(
        self,
        *,
        engine: Any,
        now: Any,
        observability_metrics: ObservabilityMetricsPort | None = None,
    ) -> None:
        self._engine = engine
        self._now = now
        self._observability = observability_metrics

    def dashboard(self, *, role: str, window: str, expand: str | None = None) -> dict[str, Any]:
        with self._engine.connect() as connection:
            now = self._now(connection)
            start, end = facts.window_bounds(window, now)
            previous_start, previous_end = facts.previous_window_bounds(window, now)
            backlog = facts.pending_job_count(connection)
            terminal = facts.job_terminal_counts(connection, start=start, end=end)
            stale = facts.stale_running_job_count(connection, now=now)
            quality = facts.ingestion_quality_facts(connection, start=start, end=end)
            quota_pending = facts.quota_pending_count(connection)
            submissions_pending = facts.submission_pending_count(connection)
            usage = facts.provider_usage_trend(connection, start=start, end=end)
            cache_hit_rate = facts.cache_hit_rate_facts(connection, start=start, end=end)
            packs: list[dict[str, Any]]
            if role == "ops":
                previous_terminal = facts.job_terminal_counts(
                    connection, start=previous_start, end=previous_end
                )
                previous_usage = facts.provider_usage_trend(
                    connection, start=previous_start, end=previous_end
                )
                previous_cache_hit_rate = facts.cache_hit_rate_facts(
                    connection, start=previous_start, end=previous_end
                )
                # Cards whose fact source has no time dimension (current queue
                # depth, pending approvals, API telemetry) have no previous
                # window to compare against and keep a null delta.
                previous = {
                    "failure_rate": _failure_rate(previous_terminal),
                    "timeout_reclaims": facts.stale_running_job_count(connection, now=previous_end),
                    "cache_hit_rate": previous_cache_hit_rate["value"],
                    "llm_usage_cost": previous_usage["calls"],
                }
                packs = self._ops_packs(
                    window=window,
                    backlog=backlog,
                    terminal=terminal,
                    stale=stale,
                    quality=quality,
                    quota_pending=quota_pending,
                    submissions_pending=submissions_pending,
                    usage=usage,
                    cache_hit_rate=cache_hit_rate,
                    previous=previous,
                )
            else:
                active_users = facts.active_user_count(connection)
                questions = facts.question_count(connection, start=start, end=end)
                extra_quota = facts.quota_approved_facts(connection, start=start, end=end)
                department_usage = facts.department_question_rows(connection, start=start, end=end)
                retrieval_usage = facts.space_usage_rows(connection, start=start, end=end)
                citation_usage = facts.space_citation_rows(connection, start=start, end=end)
                usage_breakdown = facts.provider_usage_breakdown(connection, start=start, end=end)
                quota_consumption = facts.quota_consumption_rows(connection, start=start, end=end)
                feedback = facts.feedback_ratio_facts(connection, start=start, end=end)
                previous_usage = facts.provider_usage_trend(
                    connection, start=previous_start, end=previous_end
                )
                previous = {
                    # Active-user count reflects current lifecycle state only,
                    # so it has no previous-window counterpart to compare with.
                    "question_trend": facts.question_count(
                        connection, start=previous_start, end=previous_end
                    ),
                    "monthly_llm_cost": previous_usage["cost"],
                    "thumbs_up_ratio": facts.feedback_ratio_facts(
                        connection, start=previous_start, end=previous_end
                    )["value"],
                }
                packs = self._admin_packs(
                    active_users=active_users,
                    questions=questions,
                    extra_quota=extra_quota,
                    usage=usage,
                    department_usage=department_usage,
                    retrieval_usage=retrieval_usage,
                    citation_usage=citation_usage,
                    department_cost=usage_breakdown["department_rows"],
                    user_cost=usage_breakdown["user_rows"],
                    quota_consumption=quota_consumption,
                    feedback=feedback,
                    expand_user_rank=expand == "user_rank",
                    previous=previous,
                )
        return {"window": window, "packs": packs}

    def _ops_packs(
        self,
        *,
        window: str,
        backlog: int,
        terminal: Mapping[str, int],
        stale: int,
        quality: Mapping[str, Any],
        quota_pending: int,
        submissions_pending: int,
        usage: Mapping[str, Any],
        cache_hit_rate: Mapping[str, Any],
        previous: Mapping[str, Any],
    ) -> list[dict[str, Any]]:
        api_error_rate = None
        api_latency_p95 = None
        if self._observability is not None:
            try:
                read = self._observability.read(
                    ObservabilityReadRequest(caller="retention-ops", audience="ops", window=window)
                )
                if read.data_state == "available":
                    api_error_rate = read.api.error_rate
                    api_latency_p95 = read.api.latency.p95_ms
            except ObservabilityMetricsError:
                api_error_rate = None
                api_latency_p95 = None
        failure_rate = _failure_rate(terminal)
        ocr_rows = [
            (
                {"label": row["label"], "value": row["count"], "tone": "warning"}
                if row["label"] == "low_confidence"
                else {"label": row["label"], "value": row["count"]}
            )
            for row in quality["ocr_rows"]
        ]
        low_confidence_rows = [
            {
                "label": "正常文档",
                "value": quality["normal_docs"],
                "tone": "normal",
            },
            {
                "label": "低置信文档",
                "value": quality["low_confidence_docs"],
                "tone": "warning",
            },
        ]
        tree_rows = [{"label": row["label"], "value": row["count"]} for row in quality["tree_rows"]]
        return [
            {
                "key": "tasks_health",
                "title": "任务与健康",
                "cards": [
                    _stat(
                        "ingestion_backlog",
                        "入库队列积压",
                        value=backlog,
                        threshold={"value": 20, "direction": "above"},
                        link="ops.jobs",
                    ),
                    _stat(
                        "failure_rate",
                        "失败率",
                        value=failure_rate,
                        delta=_delta_from(previous, "failure_rate", failure_rate, percent=True),
                        threshold={"value": 0.1, "direction": "above"},
                    ),
                    _stat(
                        "timeout_reclaims",
                        "超时回收次数",
                        value=stale,
                        delta=_delta_from(previous, "timeout_reclaims", stale),
                        threshold={"value": 0, "direction": "above"},
                    ),
                    _stat(
                        "api_error_rate",
                        "API 错误率",
                        value=api_error_rate,
                        threshold={"value": 0.05, "direction": "above"},
                    ),
                    _stat(
                        "api_latency",
                        "API 延迟",
                        value=api_latency_p95,
                        threshold={"value": 800, "direction": "above"},
                        link="ops.metrics",
                    ),
                ],
            },
            {
                "key": "cost_sentinel",
                "title": "成本哨兵",
                "cards": [
                    _stat(
                        "cache_hit_rate",
                        "缓存命中率",
                        value=cache_hit_rate["value"],
                        delta=_delta_from(
                            previous, "cache_hit_rate", cache_hit_rate["value"], percent=True
                        ),
                        sparkline=cache_hit_rate["sparkline"],
                        threshold={"value": 0.6, "direction": "below"},
                        link="ops.metrics",
                    ),
                    _stat(
                        "llm_usage_cost",
                        "LLM 调用量与成本趋势",
                        value=usage["calls"],
                        delta=_delta_from(previous, "llm_usage_cost", usage["calls"]),
                    ),
                ],
            },
            {
                "key": "ingestion_quality",
                "title": "入库质量",
                "cards": [
                    _distribution("ocr_confidence_dist", "OCR 置信度分布", ocr_rows),
                    _distribution(
                        "low_confidence_doc_ratio", "低置信文档占比", low_confidence_rows
                    ),
                    _distribution("graph_basic_split", "建树/basic 分流比例", tree_rows),
                ],
            },
            {
                "key": "todo",
                "title": "待办",
                "cards": [
                    _count(
                        "quota_pending",
                        "配额申请待处理数",
                        value=quota_pending,
                        link="ops.approvals.quota",
                    ),
                    _count(
                        "submission_pending",
                        "投稿审核待处理数",
                        value=submissions_pending,
                        link="ops.approvals.submissions",
                    ),
                ],
            },
        ]

    def _admin_packs(
        self,
        *,
        active_users: int,
        questions: int,
        extra_quota: Mapping[str, int],
        usage: Mapping[str, Any],
        department_usage: list[Mapping[str, Any]],
        retrieval_usage: list[Mapping[str, Any]],
        citation_usage: list[Mapping[str, Any]],
        department_cost: list[Mapping[str, Any]],
        user_cost: list[Mapping[str, Any]],
        quota_consumption: list[Mapping[str, Any]],
        feedback: Mapping[str, Any],
        expand_user_rank: bool,
        previous: Mapping[str, Any],
    ) -> list[dict[str, Any]]:
        department_rows = [
            {"label": row["label"], "value": row["count"]} for row in department_usage
        ]
        retrieval_rows = [{"label": row["label"], "value": row["count"]} for row in retrieval_usage]
        citation_rows = [{"label": row["label"], "value": row["count"]} for row in citation_usage]
        department_cost_rows = list(department_cost)
        user_cost_rows = list(user_cost)
        rank_rows = user_cost_rows[: (50 if expand_user_rank else 10)]
        quota_consumption_rows = [
            {"label": row["label"], "value": row["count"]} for row in quota_consumption
        ]
        grant_rows = [
            {"label": "追加次数", "value": extra_quota["requests"], "tone": "normal"},
            {"label": "追加页数", "value": extra_quota["pages"], "tone": "normal"},
        ]
        for row in grant_rows:
            row["ratio"] = 1.0
        return [
            {
                "key": "usage_overview",
                "title": "使用概览",
                "description": "活跃用户、提问趋势与部门使用分布",
                "cards": [
                    _stat("active_users", "活跃用户数", value=active_users),
                    _stat(
                        "question_trend",
                        "提问量趋势",
                        value=questions,
                        delta=_delta_from(previous, "question_trend", questions),
                    ),
                    _distribution("department_usage", "部门使用分布", department_rows),
                ],
            },
            {
                "key": "asset_usage",
                "title": "知识资产使用率",
                "description": "各空间文档与检索/引用频次",
                "cards": [
                    _distribution("retrieval_freq", "各空间被检索频次分布", retrieval_rows),
                    _distribution("citation_freq", "各空间被引用频次分布", citation_rows),
                ],
            },
            {
                "key": "cost_share",
                "title": "成本分摊",
                "description": "月度 LLM 成本与分摊",
                "cards": [
                    _stat(
                        "monthly_llm_cost",
                        "月度 LLM 总成本",
                        value=usage["cost"],
                        delta=_delta_from(previous, "monthly_llm_cost", usage["cost"]),
                    ),
                    _distribution("department_cost", "按部门分摊", department_cost_rows),
                    _user_rank(
                        "user_cost_rank",
                        "按用户分摊",
                        rank_rows,
                        total_count=len(user_cost_rows),
                    ),
                ],
            },
            {
                "key": "quality_quota",
                "title": "质量与配额",
                "description": "反馈趋势、配额消耗与追加额度",
                "cards": [
                    _stat(
                        "thumbs_up_ratio",
                        "点赞比例趋势",
                        value=feedback["value"],
                        delta=_delta_from(
                            previous, "thumbs_up_ratio", feedback["value"], percent=True
                        ),
                        sparkline=feedback["sparkline"],
                    ),
                    _distribution("quota_consumption", "配额消耗分布", quota_consumption_rows),
                    {
                        "key": "quota_grants",
                        "title": "追加额度发放",
                        "kind": "distribution",
                        "rows": grant_rows,
                        "threshold": None,
                        "link": None,
                    },
                ],
            },
        ]

    def operations(self, *, window: str) -> dict[str, Any]:
        with self._engine.connect() as connection:
            now = self._now(connection)
            start, end = facts.window_bounds(window, now)
            quality = facts.ingestion_quality_facts(connection, start=start, end=end)
            cache_hit_rate = facts.cache_hit_rate_facts(connection, start=start, end=end)
        ocr_rows = [
            (
                {"label": row["label"], "value": row["count"], "tone": "warning"}
                if row["label"] == "low_confidence"
                else {"label": row["label"], "value": row["count"]}
            )
            for row in quality["ocr_rows"]
        ]
        tree_rows = [{"label": row["label"], "value": row["count"]} for row in quality["tree_rows"]]
        cards = [
            _stat(
                "cache_hit_rate",
                "缓存命中率",
                value=cache_hit_rate["value"],
                sparkline=cache_hit_rate["sparkline"],
                threshold={"value": 0.6, "direction": "below"},
            ),
            _distribution("ocr_confidence_dist", "OCR 置信度分布", ocr_rows),
            _distribution("graph_basic_split", "建树/basic 分流比例", tree_rows),
        ]
        return {"window": window, "cards": cards}


class OpsJobsReadModel:
    def __init__(self, *, engine: Any, now: Any, documents_service: Any) -> None:
        self._engine = engine
        self._now = now
        self._documents = documents_service

    def jobs(self, *, principal: Any, view: str, limit: int = 200) -> dict[str, Any]:
        if view not in {"all", "active", "replayable", "stale"}:
            raise PlatformError("validation_error", "view is invalid", {"field": "view"}, 422)
        with self._engine.connect() as connection:
            now = self._now(connection)
            stale_ids = facts.stale_running_job_ids(connection, now=now)
        listed = self._documents.list_jobs(principal=principal, limit=limit)
        is_admin = getattr(principal, "role", None) == "admin"
        items: list[dict[str, Any]] = []
        for row in listed["items"]:
            job_id = str(row["job_id"])
            state = str(row["state"])
            item = {
                "job_id": job_id,
                "task_type": "ingestion",
                "document_name": row["name"],
                "state": state,
                "stale": job_id in stale_ids,
                "allowed_actions": [] if is_admin else list(row["allowed_actions"]),
                "enqueued_at": row["created_at"],
                "wait_seconds": self._wait_seconds(row, now),
            }
            items.append(item)
        if view == "active":
            items = [
                item for item in items if item["state"] in {"pending", "running", "retry_wait"}
            ]
        elif view == "replayable":
            items = [
                item for item in items if item["state"] in {"failed", "cancelled", "dead_letter"}
            ]
        elif view == "stale":
            items = [item for item in items if item["stale"]]
        # §10.1: the badge count is global, taken before the view filter narrows the items.
        return {"items": items, "stale_count": len(stale_ids)}

    @staticmethod
    def _wait_seconds(row: Mapping[str, Any], now: datetime) -> int:
        created = row.get("created_at")
        if isinstance(created, str):
            created = datetime.fromisoformat(created)
        return max(0, int((now - created).total_seconds())) if created else 0
