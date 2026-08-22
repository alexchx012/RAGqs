"""Read-only public-fact aggregations for retention read models and workers.

Everything here SELECTs owner tables and never mutates them. Aggregates that a
deployment does not provide stay absent (null/empty) — the callers never
fabricate values from absent facts.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import BigInteger, String, and_, case, cast, func, or_, select, true
from sqlalchemy.engine import Connection

from app.chat.schema import (
    chat_generation_table,
    chat_message_feedback_table,
    chat_message_table,
)
from app.documents.schema import (
    document_deletions_table,
    documents_table,
    ingestion_attempts_table,
    ingestion_jobs_table,
    knowledge_submissions_table,
)
from app.identity.schema import (
    identity_deletion_workflow_table,
    identity_department_table,
    identity_space_table,
    identity_user_table,
)
from app.indexing.schema import (
    index_generation_heads_table,
    index_generation_leases_table,
    index_generations_table,
    index_graph_components_table,
)
from app.usage.schema import quota_debit_table, quota_request_table, usage_event_table

_WINDOW_DAYS = {"today": 0, "7d": 7, "30d": 30}
_ACTIVE_JOB_STATES = ("pending", "running", "retry_wait")
_REPLAYABLE_JOB_STATES = ("failed", "cancelled", "dead_letter")
_OCR_QUALITY_BUCKETS = ("high_confidence", "medium_confidence", "low_confidence")


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def window_bounds(window: str, now: datetime) -> tuple[datetime, datetime]:
    if window not in _WINDOW_DAYS:
        raise ValueError("window must be today, 7d or 30d")
    end = now.astimezone(UTC)
    if window == "today":
        return (end.replace(hour=0, minute=0, second=0, microsecond=0), end)
    return (end - timedelta(days=_WINDOW_DAYS[window]), end)


def list_pending_document_deletions(
    connection: Connection, *, limit: int = 100
) -> list[Mapping[str, Any]]:
    rows = connection.execute(
        select(
            document_deletions_table.c.id,
            document_deletions_table.c.document_id,
            document_deletions_table.c.requested_at_utc,
        )
        .join(documents_table, documents_table.c.id == document_deletions_table.c.document_id)
        .where(
            document_deletions_table.c.status == "pending_delete",
            documents_table.c.lifecycle_status == "pending_delete",
        )
        .order_by(document_deletions_table.c.requested_at_utc)
        .limit(limit)
    ).mappings()
    return [dict(row) for row in rows]


def pending_job_count(connection: Connection) -> int:
    return int(
        connection.execute(
            select(func.count())
            .select_from(ingestion_jobs_table)
            .where(ingestion_jobs_table.c.state.in_(_ACTIVE_JOB_STATES))
        ).scalar_one()
    )


def job_terminal_counts(
    connection: Connection, *, start: datetime, end: datetime
) -> Mapping[str, int]:
    rows = connection.execute(
        select(ingestion_jobs_table.c.state, func.count())
        .where(
            ingestion_jobs_table.c.state.in_(("succeeded", "failed")),
            ingestion_jobs_table.c.updated_at_utc >= start,
            ingestion_jobs_table.c.updated_at_utc <= end,
        )
        .group_by(ingestion_jobs_table.c.state)
    ).all()
    counts = {str(state): int(count) for state, count in rows}
    return {
        "succeeded": counts.get("succeeded", 0),
        "failed": counts.get("failed", 0),
    }


def stale_running_job_count(connection: Connection, *, now: datetime) -> int:
    """Running jobs whose active attempt lease has already expired."""
    return int(
        connection.execute(
            select(func.count())
            .select_from(ingestion_jobs_table)
            .join(
                ingestion_attempts_table,
                ingestion_attempts_table.c.id == ingestion_jobs_table.c.active_attempt_id,
            )
            .where(
                ingestion_jobs_table.c.state == "running",
                ingestion_attempts_table.c.lease_expires_at_utc.is_not(None),
                ingestion_attempts_table.c.lease_expires_at_utc < now,
            )
        ).scalar_one()
    )


def stale_running_job_ids(connection: Connection, *, now: datetime) -> set[str]:
    ids = connection.execute(
        select(ingestion_jobs_table.c.id)
        .join(
            ingestion_attempts_table,
            ingestion_attempts_table.c.id == ingestion_jobs_table.c.active_attempt_id,
        )
        .where(
            ingestion_jobs_table.c.state == "running",
            ingestion_attempts_table.c.lease_expires_at_utc.is_not(None),
            ingestion_attempts_table.c.lease_expires_at_utc < now,
        )
    ).scalars()
    return {str(job_id) for job_id in ids}


def _quality_window_conditions(start: datetime, end: datetime) -> tuple[Any, ...]:
    return (
        ingestion_jobs_table.c.state == "succeeded",
        ingestion_jobs_table.c.updated_at_utc >= start,
        ingestion_jobs_table.c.updated_at_utc <= end,
    )


def _sqlite_quality_distribution(
    connection: Connection,
    *,
    summary_key: str,
    allowed_labels: tuple[str, ...] | None,
    start: datetime,
    end: datetime,
) -> list[dict[str, Any]]:
    path = f"$.{summary_key}"
    entries = func.json_each(
        ingestion_jobs_table.c.processing_summary_json,
        path,
    ).table_valued("key", "value", "type")
    total = func.sum(cast(entries.c.value, BigInteger))
    conditions: list[Any] = [
        *_quality_window_conditions(start, end),
        func.json_type(ingestion_jobs_table.c.processing_summary_json, path) == "object",
        entries.c.type.in_(("integer", "true", "false")),
    ]
    if allowed_labels is not None:
        conditions.append(entries.c.key.in_(allowed_labels))
    rows = connection.execute(
        select(entries.c.key.label("label"), total.label("count"))
        .select_from(ingestion_jobs_table.join(entries, true()))
        .where(*conditions)
        .group_by(entries.c.key)
        .having(total > 0)
        .order_by(entries.c.key)
    ).all()
    return [{"label": str(label), "count": int(count)} for label, count in rows]


def _postgres_quality_distribution(
    connection: Connection,
    *,
    summary_key: str,
    allowed_labels: tuple[str, ...] | None,
    start: datetime,
    end: datetime,
) -> list[dict[str, Any]]:
    summary = ingestion_jobs_table.c.processing_summary_json[summary_key]
    entries = func.json_each(summary).table_valued("key", "value")
    value_kind = func.json_typeof(entries.c.value)
    value_text = cast(entries.c.value, String)
    is_integer = and_(
        value_kind == "number",
        value_text.op("~")(r"^-?[0-9]+$"),
    )
    is_boolean = value_kind == "boolean"
    numeric_value = case(
        (value_text == "true", 1),
        (value_text == "false", 0),
        (is_integer, cast(value_text, BigInteger)),
        else_=None,
    )
    total = func.sum(numeric_value)
    conditions: list[Any] = [
        *_quality_window_conditions(start, end),
        func.json_typeof(summary) == "object",
        or_(is_boolean, is_integer),
    ]
    if allowed_labels is not None:
        conditions.append(entries.c.key.in_(allowed_labels))
    rows = connection.execute(
        select(entries.c.key.label("label"), total.label("count"))
        .select_from(ingestion_jobs_table.join(entries, true()))
        .where(*conditions)
        .group_by(entries.c.key)
        .having(total > 0)
        .order_by(entries.c.key)
    ).all()
    return [{"label": str(label), "count": int(count)} for label, count in rows]


def _quality_distribution(
    connection: Connection,
    *,
    summary_key: str,
    allowed_labels: tuple[str, ...] | None,
    start: datetime,
    end: datetime,
) -> list[dict[str, Any]]:
    if connection.dialect.name == "sqlite":
        return _sqlite_quality_distribution(
            connection,
            summary_key=summary_key,
            allowed_labels=allowed_labels,
            start=start,
            end=end,
        )
    if connection.dialect.name == "postgresql":
        return _postgres_quality_distribution(
            connection,
            summary_key=summary_key,
            allowed_labels=allowed_labels,
            start=start,
            end=end,
        )
    raise ValueError("ingestion quality aggregation requires SQLite or PostgreSQL")


def ingestion_quality_facts(
    connection: Connection, *, start: datetime, end: datetime
) -> Mapping[str, Any]:
    ocr_rows = _quality_distribution(
        connection,
        summary_key="ocr",
        allowed_labels=_OCR_QUALITY_BUCKETS,
        start=start,
        end=end,
    )
    tree_rows = _quality_distribution(
        connection,
        summary_key="tree",
        allowed_labels=None,
        start=start,
        end=end,
    )
    low_confidence, total = connection.execute(
        select(
            func.coalesce(
                func.sum(case((ingestion_jobs_table.c.ocr_low_confidence.is_(True), 1), else_=0)),
                0,
            ),
            func.count(),
        ).where(*_quality_window_conditions(start, end))
    ).one()
    low_confidence_docs = int(low_confidence)
    return {
        "ocr_rows": ocr_rows,
        "tree_rows": tree_rows,
        "low_confidence_docs": low_confidence_docs,
        "normal_docs": int(total) - low_confidence_docs,
    }


def quota_pending_count(connection: Connection) -> int:
    return int(
        connection.execute(
            select(func.count())
            .select_from(quota_request_table)
            .where(quota_request_table.c.status == "pending")
        ).scalar_one()
    )


def submission_pending_count(connection: Connection) -> int:
    return int(
        connection.execute(
            select(func.count())
            .select_from(knowledge_submissions_table)
            .where(knowledge_submissions_table.c.status == "pending")
        ).scalar_one()
    )


def quota_approved_facts(
    connection: Connection, *, start: datetime, end: datetime
) -> Mapping[str, Any]:
    rows = connection.execute(
        select(
            func.count(), func.coalesce(func.sum(quota_request_table.c.approved_pages), 0)
        ).where(
            quota_request_table.c.status == "approved",
            quota_request_table.c.reviewed_at_utc >= start,
            quota_request_table.c.reviewed_at_utc <= end,
        )
    ).one()
    return {"requests": int(rows[0]), "pages": int(rows[1])}


def provider_usage_trend(
    connection: Connection, *, start: datetime, end: datetime
) -> Mapping[str, Any]:
    calls = int(
        connection.execute(
            select(func.count())
            .select_from(usage_event_table)
            .where(
                usage_event_table.c.event_kind == "provider_usage",
                usage_event_table.c.completed_at_utc >= start,
                usage_event_table.c.completed_at_utc <= end,
            )
        ).scalar_one()
    )
    cost = connection.execute(
        select(func.sum(usage_event_table.c.estimated_cost_amount)).where(
            usage_event_table.c.event_kind == "provider_usage",
            usage_event_table.c.estimated_cost_amount.is_not(None),
            usage_event_table.c.completed_at_utc >= start,
            usage_event_table.c.completed_at_utc <= end,
        )
    ).scalar_one()
    cost_value = None
    if cost is not None:
        cost_value = float(cost)
    return {"calls": calls, "cost": cost_value}


def cache_hit_rate_facts(
    connection: Connection, *, start: datetime, end: datetime
) -> Mapping[str, Any]:
    rows = connection.execute(
        select(
            usage_event_table.c.prompt_cache_hit_tokens,
            usage_event_table.c.prompt_cache_miss_tokens,
            usage_event_table.c.completed_at_utc,
        )
        .where(
            usage_event_table.c.event_kind == "provider_usage",
            usage_event_table.c.completed_at_utc >= start,
            usage_event_table.c.completed_at_utc <= end,
        )
        .order_by(usage_event_table.c.completed_at_utc)
    ).mappings()
    buckets: list[list[int]] = [[0, 0] for _ in range(5)]
    total_hit = 0
    total_miss = 0
    window_seconds = max(1.0, (end - start).total_seconds())
    for row in rows:
        hit = row["prompt_cache_hit_tokens"]
        miss = row["prompt_cache_miss_tokens"]
        if not isinstance(hit, int) or isinstance(hit, bool) or hit < 0:
            hit = 0
        if not isinstance(miss, int) or isinstance(miss, bool) or miss < 0:
            miss = 0
        if hit + miss == 0:
            continue
        total_hit += hit
        total_miss += miss
        completed = row["completed_at_utc"]
        elapsed = (
            (_utc(completed) - start).total_seconds() if isinstance(completed, datetime) else 0
        )
        bucket = min(4, max(0, int(elapsed / window_seconds * 5)))
        buckets[bucket][0] += hit
        buckets[bucket][1] += miss
    total = total_hit + total_miss
    if total == 0:
        return {"value": None, "sparkline": []}
    sparkline = []
    for bucket in buckets:
        bucket_total = sum(bucket)
        if bucket_total > 0:
            sparkline.append(round(bucket[0] / bucket_total, 4))
    return {"value": round(total_hit / total, 4), "sparkline": sparkline}


def provider_usage_breakdown(
    connection: Connection, *, start: datetime, end: datetime
) -> Mapping[str, Any]:
    rows = connection.execute(
        select(
            usage_event_table.c.estimated_cost_amount,
            usage_event_table.c.ownership_json,
        ).where(
            usage_event_table.c.event_kind == "provider_usage",
            usage_event_table.c.completed_at_utc >= start,
            usage_event_table.c.completed_at_utc <= end,
            usage_event_table.c.estimated_cost_amount.is_not(None),
        )
    ).mappings()
    facts_rows = list(rows)
    user_ids: set[str] = set()
    department_ids: set[str] = set()
    for row in facts_rows:
        ownership = row["ownership_json"] if isinstance(row["ownership_json"], Mapping) else {}
        user_id = ownership.get("quota_subject_user_id") or ownership.get("actor_user_id")
        department_id = ownership.get("actor_department_id_snapshot")
        if isinstance(user_id, str) and user_id:
            user_ids.add(user_id)
        if isinstance(department_id, str) and department_id:
            department_ids.add(department_id)

    users: dict[str, Mapping[str, Any]] = {}
    if user_ids:
        user_rows = connection.execute(
            select(
                identity_user_table.c.id,
                identity_user_table.c.display_name,
                identity_user_table.c.username,
                identity_user_table.c.role,
                identity_user_table.c.department_id,
            ).where(identity_user_table.c.id.in_(user_ids))
        ).mappings()
        users = {str(row["id"]): row for row in user_rows}
        department_ids.update(
            str(row["department_id"])
            for row in users.values()
            if isinstance(row["department_id"], str) and row["department_id"]
        )

    departments: dict[str, str] = {}
    if department_ids:
        department_rows = connection.execute(
            select(identity_department_table.c.id, identity_department_table.c.name).where(
                identity_department_table.c.id.in_(department_ids)
            )
        ).all()
        departments = {str(row[0]): str(row[1]) for row in department_rows}

    role_labels = {"ops": "运维", "admin": "超管"}
    department_cost: dict[str, float] = {}
    user_cost: dict[str, float] = {}
    for row in facts_rows:
        cost = row["estimated_cost_amount"]
        if isinstance(cost, Decimal):
            amount = float(cost)
        elif isinstance(cost, (int, float)) and not isinstance(cost, bool):
            amount = float(cost)
        else:
            continue
        ownership = row["ownership_json"] if isinstance(row["ownership_json"], Mapping) else {}
        user_id = ownership.get("quota_subject_user_id") or ownership.get("actor_user_id")
        user = users.get(str(user_id)) if user_id is not None else None
        department_id = ownership.get("actor_department_id_snapshot")
        if not isinstance(department_id, str) or not department_id:
            department_id = user["department_id"] if user is not None else None
        if isinstance(department_id, str) and department_id in departments:
            department_label = departments[department_id]
        elif user is not None and user["role"] in role_labels:
            department_label = role_labels[str(user["role"])]
        else:
            department_label = "无部门"
        department_cost[department_label] = department_cost.get(department_label, 0.0) + amount
        if user is not None:
            user_label = str(user["display_name"] or user["username"])
        else:
            user_label = str(user_id) if user_id is not None else "未知用户"
        user_cost[user_label] = user_cost.get(user_label, 0.0) + amount

    def ordered(values: Mapping[str, float]) -> list[dict[str, Any]]:
        return [
            {"label": label, "value": round(value, 4)}
            for label, value in sorted(values.items(), key=lambda item: (-item[1], item[0]))
            if value != 0
        ]

    return {"department_rows": ordered(department_cost), "user_rows": ordered(user_cost)}


def feedback_ratio_facts(
    connection: Connection, *, start: datetime, end: datetime
) -> Mapping[str, Any]:
    up, total = connection.execute(
        select(
            func.coalesce(
                func.sum(case((chat_message_feedback_table.c.vote == "up", 1), else_=0)),
                0,
            ),
            func.count(),
        ).where(
            chat_message_feedback_table.c.created_at_utc >= start,
            chat_message_feedback_table.c.created_at_utc <= end,
        )
    ).one()
    if int(total) == 0:
        return {"value": None, "sparkline": []}
    value = round(int(up) / int(total), 4)
    return {"value": value, "sparkline": [value]}


def department_question_rows(
    connection: Connection, *, start: datetime, end: datetime
) -> list[Mapping[str, Any]]:
    rows = connection.execute(
        select(
            func.coalesce(identity_department_table.c.name, "未分配"),
            func.count(),
        )
        .select_from(
            chat_message_table.join(
                identity_user_table,
                identity_user_table.c.id == chat_message_table.c.owner_user_id,
            ).outerjoin(
                identity_department_table,
                identity_department_table.c.id == identity_user_table.c.department_id,
            )
        )
        .where(
            chat_message_table.c.role == "user",
            chat_message_table.c.created_at_utc >= start,
            chat_message_table.c.created_at_utc <= end,
        )
        .group_by(identity_department_table.c.name)
        .order_by(func.count().desc(), func.coalesce(identity_department_table.c.name, "未分配"))
    ).all()
    return [{"label": str(label), "count": int(count)} for label, count in rows]


def _space_count_rows(connection: Connection, counts: Mapping[str, int]) -> list[Mapping[str, Any]]:
    if not counts:
        return []
    names = dict(
        connection.execute(
            select(identity_space_table.c.id, identity_space_table.c.name).where(
                identity_space_table.c.id.in_(counts)
            )
        ).all()
    )
    labels = {str(space_id): str(name) for space_id, name in names}
    rows = [
        {"label": labels.get(space_id, space_id), "count": count}
        for space_id, count in counts.items()
    ]
    rows.sort(key=lambda row: (-int(row["count"]), str(row["label"])))
    return rows


def space_usage_rows(
    connection: Connection, *, start: datetime, end: datetime
) -> list[Mapping[str, Any]]:
    rows = connection.execute(
        select(chat_generation_table.c.request_scope_json).where(
            chat_generation_table.c.created_at_utc >= start,
            chat_generation_table.c.created_at_utc <= end,
        )
    ).all()
    counts: dict[str, int] = {}
    for (scope,) in rows:
        if not isinstance(scope, Mapping):
            continue
        space_ids = scope.get("space_ids")
        if not isinstance(space_ids, (list, tuple)):
            continue
        for space_id in space_ids:
            if isinstance(space_id, str) and space_id:
                counts[space_id] = counts.get(space_id, 0) + 1
    return _space_count_rows(connection, counts)


def space_citation_rows(
    connection: Connection, *, start: datetime, end: datetime
) -> list[Mapping[str, Any]]:
    rows = connection.execute(
        select(chat_message_table.c.citations_json).where(
            chat_message_table.c.role == "assistant",
            chat_message_table.c.created_at_utc >= start,
            chat_message_table.c.created_at_utc <= end,
        )
    ).all()
    counts: dict[str, int] = {}
    for (citations,) in rows:
        if not isinstance(citations, (list, tuple)):
            continue
        for citation in citations:
            if not isinstance(citation, Mapping):
                continue
            space_id = citation.get("space_id")
            if isinstance(space_id, str) and space_id:
                counts[space_id] = counts.get(space_id, 0) + 1
    return _space_count_rows(connection, counts)


def quota_consumption_rows(
    connection: Connection, *, start: datetime, end: datetime
) -> list[Mapping[str, Any]]:
    rows = connection.execute(
        select(
            quota_debit_table.c.quota_subject_user_id,
            quota_debit_table.c.page_delta,
        ).where(
            quota_debit_table.c.entry_kind == "debit",
            quota_debit_table.c.effective_at_utc >= start,
            quota_debit_table.c.effective_at_utc <= end,
        )
    ).all()
    subject_ids = {str(user_id) for user_id, _pages in rows if user_id is not None}
    users = (
        {
            str(row["id"]): row
            for row in connection.execute(
                select(
                    identity_user_table.c.id,
                    identity_user_table.c.department_id,
                    identity_user_table.c.role,
                ).where(identity_user_table.c.id.in_(subject_ids))
            ).mappings()
        }
        if subject_ids
        else {}
    )
    department_ids = {
        str(row["department_id"])
        for row in users.values()
        if isinstance(row["department_id"], str) and row["department_id"]
    }
    departments = (
        {
            str(row[0]): str(row[1])
            for row in connection.execute(
                select(identity_department_table.c.id, identity_department_table.c.name).where(
                    identity_department_table.c.id.in_(department_ids)
                )
            ).all()
        }
        if department_ids
        else {}
    )
    totals: dict[str, int] = {}
    for user_id, pages in rows:
        if not isinstance(pages, int) or isinstance(pages, bool) or pages <= 0:
            continue
        user = users.get(str(user_id)) if user_id is not None else None
        department_id = user["department_id"] if user is not None else None
        label = departments.get(str(department_id), "无部门")
        totals[label] = totals.get(label, 0) + pages
    return [
        {"label": label, "count": pages}
        for label, pages in sorted(totals.items(), key=lambda item: (-item[1], item[0]))
    ]


def active_user_count(connection: Connection) -> int:
    return int(
        connection.execute(
            select(func.count())
            .select_from(identity_user_table)
            .where(identity_user_table.c.lifecycle_status == "active")
        ).scalar_one()
    )


def question_count(connection: Connection, *, start: datetime, end: datetime) -> int:
    return int(
        connection.execute(
            select(func.count())
            .select_from(chat_message_table)
            .where(
                chat_message_table.c.role == "user",
                chat_message_table.c.created_at_utc >= start,
                chat_message_table.c.created_at_utc <= end,
            )
        ).scalar_one()
    )


def feedback_up_count(connection: Connection, *, start: datetime, end: datetime) -> int:
    return int(
        connection.execute(
            select(func.count())
            .select_from(chat_message_feedback_table)
            .where(
                chat_message_feedback_table.c.vote == "up",
                chat_message_feedback_table.c.created_at_utc >= start,
                chat_message_feedback_table.c.created_at_utc <= end,
            )
        ).scalar_one()
    )


def department_user_rows(connection: Connection) -> list[Mapping[str, Any]]:
    rows = connection.execute(
        select(identity_department_table.c.name, func.count())
        .select_from(identity_user_table)
        .join(
            identity_department_table,
            identity_department_table.c.id == identity_user_table.c.department_id,
        )
        .where(identity_user_table.c.lifecycle_status == "active")
        .group_by(identity_department_table.c.name)
        .order_by(func.count().desc())
    ).all()
    return [{"label": str(name), "count": int(count)} for name, count in rows]


def space_document_rows(connection: Connection) -> list[Mapping[str, Any]]:
    rows = connection.execute(
        select(identity_space_table.c.name, func.count())
        .select_from(documents_table)
        .join(identity_space_table, identity_space_table.c.id == documents_table.c.space_id)
        .where(documents_table.c.lifecycle_status == "active")
        .group_by(identity_space_table.c.name)
        .order_by(func.count().desc())
    ).all()
    return [{"label": str(name), "count": int(count)} for name, count in rows]


def gc_candidate_generations(
    connection: Connection,
    *,
    now: datetime,
    limit: int = 50,
    excluded_generation_ids: Collection[str] = (),
) -> list[Mapping[str, Any]]:
    head = (
        connection.execute(
            select(
                index_generation_heads_table.c.active_generation_id,
                index_generation_heads_table.c.rollback_candidate_id,
            ).where(index_generation_heads_table.c.id == "instance")
        )
        .mappings()
        .one_or_none()
    )
    active_id = str(head["active_generation_id"]) if head is not None else None
    rollback_id = str(head["rollback_candidate_id"]) if head is not None else None
    excluded = {value for value in (active_id, rollback_id) if value is not None}
    excluded.update(excluded_generation_ids)
    leased = (
        select(index_generation_leases_table.c.generation_id).where(
            index_generation_leases_table.c.expires_at_utc > now,
            index_generation_leases_table.c.released_at_utc.is_(None),
        )
    ).subquery()
    query = select(index_generations_table.c.id, index_generations_table.c.status).where(
        index_generations_table.c.status == "retired",
        or_(
            index_generations_table.c.rollback_until_utc.is_(None),
            index_generations_table.c.rollback_until_utc <= now,
        ),
        ~index_generations_table.c.id.in_(select(leased.c.generation_id)),
    )
    if excluded:
        query = query.where(index_generations_table.c.id.not_in(excluded))
    query = query.order_by(
        index_generations_table.c.retired_at_utc, index_generations_table.c.id
    ).limit(limit)
    rows = connection.execute(query).mappings()
    return [dict(row) for row in rows]


def gc_blocked_rollback_candidate(connection: Connection) -> Mapping[str, Any] | None:
    head = (
        connection.execute(
            select(
                index_generation_heads_table.c.active_generation_id,
                index_generation_heads_table.c.rollback_candidate_id,
            ).where(index_generation_heads_table.c.id == "instance")
        )
        .mappings()
        .one_or_none()
    )
    if head is None or head["rollback_candidate_id"] is None:
        return None
    return {
        "active_generation_id": str(head["active_generation_id"]),
        "rollback_candidate_id": str(head["rollback_candidate_id"]),
    }


def generation_status(connection: Connection, *, generation_id: str) -> str | None:
    return connection.execute(
        select(index_generations_table.c.status).where(
            index_generations_table.c.id == generation_id
        )
    ).scalar_one_or_none()


def graph_component_ids(connection: Connection, *, generation_id: str) -> list[str]:
    ids = connection.execute(
        select(index_graph_components_table.c.id).where(
            index_graph_components_table.c.generation_id == generation_id,
            index_graph_components_table.c.component_state != "disabled",
        )
    ).scalars()
    return [str(component_id) for component_id in ids]


def retired_workflow_rows(connection: Connection, *, limit: int = 100) -> list[Mapping[str, Any]]:
    rows = connection.execute(
        select(
            identity_deletion_workflow_table.c.user_id,
            identity_deletion_workflow_table.c.cleanup_operation_id,
            identity_deletion_workflow_table.c.retirement_receipt_id,
            identity_deletion_workflow_table.c.completed_at_utc,
        )
        .where(identity_deletion_workflow_table.c.retirement_receipt_id.is_not(None))
        .order_by(identity_deletion_workflow_table.c.completed_at_utc)
        .limit(limit)
    ).mappings()
    return [dict(row) for row in rows]
