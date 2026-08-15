"""Read-only public-fact aggregations for retention read models and workers.

Everything here SELECTs owner tables and never mutates them. Aggregates that a
deployment does not provide stay absent (null/empty) — the callers never
fabricate values from absent facts.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import BigInteger, String, and_, case, cast, func, or_, select, true
from sqlalchemy.engine import Connection

from app.chat.schema import chat_message_feedback_table, chat_message_table
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
from app.usage.schema import quota_request_table, usage_event_table

_WINDOW_DAYS = {"today": 0, "7d": 7, "30d": 30}
_ACTIVE_JOB_STATES = ("pending", "running", "retry_wait")
_REPLAYABLE_JOB_STATES = ("failed", "cancelled", "dead_letter")
_OCR_QUALITY_BUCKETS = ("high_confidence", "medium_confidence", "low_confidence")


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
        select(func.coalesce(func.sum(usage_event_table.c.estimated_cost_amount), 0)).where(
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
