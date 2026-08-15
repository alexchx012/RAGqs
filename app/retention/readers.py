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


def _ratio(value: int, total: int) -> float:
    return round(value / total, 4) if total > 0 else 0.0


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
    total = sum(int(row["value"]) for row in rows)
    return {
        "key": key,
        "title": title,
        "kind": "distribution",
        "rows": [
            {
                "label": str(row["label"]),
                "value": int(row["value"]),
                "ratio": _ratio(int(row["value"]), total),
                **({"tone": str(row["tone"])} if row.get("tone") in {"normal", "warning"} else {}),
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
    total = sum(int(row["value"]) for row in rows)
    return {
        "key": key,
        "title": title,
        "kind": "user_rank",
        "rows": [
            {
                "label": str(row["label"]),
                "value": int(row["value"]),
                "ratio": _ratio(int(row["value"]), total),
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

    def dashboard(self, *, role: str, window: str) -> dict[str, Any]:
        with self._engine.connect() as connection:
            now = self._now(connection)
            start, end = facts.window_bounds(window, now)
            backlog = facts.pending_job_count(connection)
            terminal = facts.job_terminal_counts(connection, start=start, end=end)
            stale = facts.stale_running_job_count(connection, now=now)
            quality = facts.ingestion_quality_facts(connection, start=start, end=end)
            quota_pending = facts.quota_pending_count(connection)
            submissions_pending = facts.submission_pending_count(connection)
            usage = facts.provider_usage_trend(connection, start=start, end=end)
            packs: list[dict[str, Any]]
            if role == "ops":
                packs = self._ops_packs(
                    window=window,
                    backlog=backlog,
                    terminal=terminal,
                    stale=stale,
                    quality=quality,
                    quota_pending=quota_pending,
                    submissions_pending=submissions_pending,
                    usage=usage,
                )
            else:
                active_users = facts.active_user_count(connection)
                questions = facts.question_count(connection, start=start, end=end)
                thumbs = facts.feedback_up_count(connection, start=start, end=end)
                extra_quota = facts.quota_approved_facts(connection, start=start, end=end)
                departments = facts.department_user_rows(connection)
                spaces = facts.space_document_rows(connection)
                packs = self._admin_packs(
                    active_users=active_users,
                    questions=questions,
                    thumbs=thumbs,
                    extra_quota=extra_quota,
                    departments=departments,
                    spaces=spaces,
                    usage=usage,
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
        total_terminal = terminal["succeeded"] + terminal["failed"]
        failure_rate = round(terminal["failed"] / total_terminal, 4) if total_terminal > 0 else None
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
                        threshold={"value": 0.1, "direction": "above"},
                    ),
                    _stat(
                        "timeout_reclaims",
                        "超时回收次数",
                        value=stale,
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
                        value=None,
                        threshold={"value": 0.6, "direction": "below"},
                        link="ops.metrics",
                    ),
                    _stat("llm_usage_cost", "LLM 调用量与成本趋势", value=usage["calls"]),
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
        thumbs: int,
        extra_quota: Mapping[str, int],
        departments: list[Mapping[str, Any]],
        spaces: list[Mapping[str, Any]],
        usage: Mapping[str, Any],
    ) -> list[dict[str, Any]]:
        department_rows = [{"label": row["label"], "value": row["count"]} for row in departments]
        space_rows = [{"label": row["label"], "value": row["count"]} for row in spaces]
        return [
            {
                "key": "usage_overview",
                "title": "使用概览",
                "description": "活跃用户、提问趋势与部门使用分布",
                "cards": [
                    _stat("active_users", "活跃用户", value=active_users),
                    _stat("question_trend", "提问趋势", value=questions),
                    _distribution("department_usage", "部门使用分布", department_rows),
                ],
            },
            {
                "key": "asset_usage",
                "title": "资产使用",
                "description": "各空间文档与检索/引用频次",
                "cards": [
                    _distribution("space_usage", "各空间文档分布", space_rows),
                    _distribution("space_citation", "各空间检索/引用频次分布", []),
                ],
            },
            {
                "key": "cost_share",
                "title": "成本分摊",
                "description": "月度 LLM 成本与分摊",
                "cards": [
                    _stat("monthly_cost", "月度 LLM 总成本", value=usage["cost"]),
                    _distribution("department_share", "按部门分摊", []),
                    _user_rank("top_users", "成本前十用户", [], total_count=0),
                ],
            },
            {
                "key": "quality_quota",
                "title": "质量与配额",
                "description": "反馈趋势、配额消耗与追加额度",
                "cards": [
                    _stat("thumbs_trend", "👍 趋势", value=thumbs),
                    _distribution("quota_consumption", "配额消耗分布", []),
                    _stat("extra_quota", "追加额度次数", value=extra_quota["requests"]),
                ],
            },
        ]

    def operations(self, *, window: str) -> dict[str, Any]:
        with self._engine.connect() as connection:
            now = self._now(connection)
            start, end = facts.window_bounds(window, now)
            quality = facts.ingestion_quality_facts(connection, start=start, end=end)
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
                value=None,
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
        stale_count = sum(1 for item in items if item["stale"])
        return {"items": items, "stale_count": stale_count}

    @staticmethod
    def _wait_seconds(row: Mapping[str, Any], now: datetime) -> int:
        created = row.get("created_at")
        if isinstance(created, str):
            created = datetime.fromisoformat(created)
        return max(0, int((now - created).total_seconds())) if created else 0
