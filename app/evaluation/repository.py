"""SQLAlchemy repository for evaluation runs, suggestions and calibration windows."""

from __future__ import annotations

import secrets
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import and_, select, update
from sqlalchemy.engine import Connection, Engine

from app.platform.errors import PlatformError

from .models import (
    EvaluationPolicySnapshot,
    ShadowRunRecord,
    SuggestionRecord,
    WindowSnapshot,
)
from .schema import (
    calibration_window_command_table,
    calibration_window_suggestion_table,
    calibration_window_table,
    evaluation_active_default_table,
    evaluation_golden_item_table,
    evaluation_golden_set_version_table,
    evaluation_policy_table,
    evaluation_run_command_table,
    evaluation_sample_snapshot_item_table,
    evaluation_sample_snapshot_table,
    shadow_evaluation_result_table,
    shadow_evaluation_run_table,
)

RUN_STATES = ("queued", "running", "retry_wait", "succeeded", "failed", "cancelled")
NON_TERMINAL_STATES = ("queued", "running", "retry_wait")
TERMINAL_STATES = ("succeeded", "failed", "cancelled")
ACTIVE_CLAIM_STATES = ("queued", "retry_wait")


def _new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_urlsafe(15)}"


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _policy_from_row(row: Mapping[str, Any]) -> EvaluationPolicySnapshot:
    return EvaluationPolicySnapshot(
        policy_version=str(row["policy_version"]),
        faithfulness_min=float(row["faithfulness_min"]),
        refusal_rate_min=float(row["refusal_rate_min"]),
        hit_at_k_final_min=float(row["hit_at_k_final_min"]),
        mrr_min=float(row["mrr_min"]),
        p95_latency_max_ms=int(row["p95_latency_max_ms"]),
        cost_per_query_max=float(row["cost_per_query_max"]),
        min_real_queries=int(row["min_real_queries"]),
        shadow_max_examples=int(row["shadow_max_examples"]),
        shadow_max_candidate_configs=int(row["shadow_max_candidate_configs"]),
        calibration_open_score_gap=float(row["calibration_open_score_gap"]),
        cold_start_sample_rate=float(row["cold_start_sample_rate"]),
        sentinel_sample_rate=float(row["sentinel_sample_rate"]),
        pair_vote_ttl_seconds=int(row["pair_vote_ttl_seconds"]),
        close_grace_seconds=int(row["close_grace_seconds"]),
        max_attempts=int(row["max_attempts"]),
        run_deadline_seconds=int(row["run_deadline_seconds"]),
        lease_seconds=int(row["lease_seconds"]),
        heartbeat_seconds=int(row["heartbeat_seconds"]),
        concurrency=int(row["concurrency"]),
        judge_k=int(row["judge_k"]),
        created_at_utc=_utc(row["created_at_utc"]),  # type: ignore[arg-type]
    )


def _run_from_row(row: Mapping[str, Any]) -> ShadowRunRecord:
    return ShadowRunRecord(
        run_id=str(row["run_id"]),
        space_id=str(row["space_id"]),
        state=str(row["state"]),
        attempt=int(row["attempt"]),
        lease_owner=str(row["lease_owner"]) if row["lease_owner"] is not None else None,
        lease_expires_at=_utc(row["lease_expires_at_utc"]),
        heartbeat_at=_utc(row["heartbeat_at_utc"]),
        fencing_token=str(row["fencing_token"]) if row["fencing_token"] is not None else None,
        next_attempt_at=_utc(row["next_attempt_at_utc"]),
        failure_class=str(row["failure_class"]) if row["failure_class"] is not None else None,
        progress=dict(row["progress_json"] or {}),
        report_ref=str(row["report_ref"]) if row["report_ref"] is not None else None,
        policy_version=str(row["policy_version"]),
        comparator_key=str(row["comparator_key"]) if row["comparator_key"] is not None else None,
        candidate_config_versions=tuple(
            str(item) for item in (row["candidate_config_versions_json"] or [])
        ),
        index_generation_id=str(row["index_generation_id"]),
        index_revision=int(row["index_revision"]),
        frozen_snapshot=dict(row["frozen_snapshot_json"] or {}),
        created_at=_utc(row["created_at_utc"]),  # type: ignore[arg-type]
        started_at=_utc(row["started_at_utc"]),
        completed_at=_utc(row["completed_at_utc"]),
        version=int(row["version"]),
    )


def _window_from_row(row: Mapping[str, Any]) -> WindowSnapshot:
    return WindowSnapshot(
        window_id=str(row["window_id"]),
        status=str(row["status"]),
        opened_at=_utc(row["opened_at_utc"]),
        closed_at=_utc(row["closed_at_utc"]),
        pairs_collected=int(row["pairs_collected"]),
        close_deadline_at=_utc(row["close_deadline_at_utc"]),
        window_kind=str(row["window_kind"]),
        policy_version=str(row["policy_version"]),
        sample_rate=float(row["sample_rate"]),
        opened_by=str(row["opened_by"]),
        closed_by=str(row["closed_by"]) if row["closed_by"] is not None else None,
    )


class SqlAlchemyEvaluationRepository:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    # ---------------------------------------------------------------- policy

    def ensure_policy(
        self,
        connection: Connection,
        *,
        policy: EvaluationPolicySnapshot,
    ) -> None:
        existing = connection.execute(
            select(evaluation_policy_table.c.policy_version).where(
                evaluation_policy_table.c.policy_version == policy.policy_version
            )
        ).scalar_one_or_none()
        if existing is not None:
            return
        connection.execute(
            evaluation_policy_table.insert().values(
                policy_version=policy.policy_version,
                faithfulness_min=policy.faithfulness_min,
                refusal_rate_min=policy.refusal_rate_min,
                hit_at_k_final_min=policy.hit_at_k_final_min,
                mrr_min=policy.mrr_min,
                p95_latency_max_ms=policy.p95_latency_max_ms,
                cost_per_query_max=policy.cost_per_query_max,
                min_real_queries=policy.min_real_queries,
                shadow_max_examples=policy.shadow_max_examples,
                shadow_max_candidate_configs=policy.shadow_max_candidate_configs,
                calibration_open_score_gap=policy.calibration_open_score_gap,
                cold_start_sample_rate=policy.cold_start_sample_rate,
                sentinel_sample_rate=policy.sentinel_sample_rate,
                pair_vote_ttl_seconds=policy.pair_vote_ttl_seconds,
                close_grace_seconds=policy.close_grace_seconds,
                max_attempts=policy.max_attempts,
                run_deadline_seconds=policy.run_deadline_seconds,
                lease_seconds=policy.lease_seconds,
                heartbeat_seconds=policy.heartbeat_seconds,
                concurrency=policy.concurrency,
                judge_k=policy.judge_k,
                created_at_utc=policy.created_at_utc,
            )
        )

    def get_policy(
        self, connection: Connection, *, policy_version: str
    ) -> EvaluationPolicySnapshot | None:
        row = (
            connection.execute(
                select(evaluation_policy_table).where(
                    evaluation_policy_table.c.policy_version == policy_version
                )
            )
            .mappings()
            .one_or_none()
        )
        return _policy_from_row(row) if row is not None else None

    def latest_policy(self, connection: Connection) -> EvaluationPolicySnapshot | None:
        row = (
            connection.execute(
                select(evaluation_policy_table).order_by(
                    evaluation_policy_table.c.created_at_utc.desc()
                )
            )
            .mappings()
            .first()
        )
        return _policy_from_row(row) if row is not None else None

    # ---------------------------------------------------------------- golden

    def latest_golden_set_version(self, connection: Connection, *, space_id: str) -> str | None:
        row = (
            connection.execute(
                select(evaluation_golden_set_version_table.c.version)
                .where(evaluation_golden_set_version_table.c.space_id == space_id)
                .order_by(
                    evaluation_golden_set_version_table.c.created_at_utc.desc(),
                    evaluation_golden_set_version_table.c.version.desc(),
                )
                .limit(1)
            )
            .mappings()
            .first()
        )
        return str(row["version"]) if row is not None else None

    def publish_golden_set_version(
        self,
        connection: Connection,
        *,
        space_id: str,
        version: str,
        items: tuple[Mapping[str, Any], ...],
        now: datetime,
    ) -> None:
        """Create one immutable, versioned golden set for a space (A13).

        New/revisioned sets always create a new version row; existing versions
        and previously computed results are never backfilled or rewritten.
        """
        connection.execute(
            evaluation_golden_set_version_table.insert().values(
                space_id=space_id,
                version=version,
                created_at_utc=now,
            )
        )
        for index, item in enumerate(items, start=1):
            connection.execute(
                evaluation_golden_item_table.insert().values(
                    item_id=f"golden_{space_id}_{version}_{index}",
                    space_id=space_id,
                    golden_version=version,
                    question_text=str(item["question_text"]),
                    question_hash=str(item["question_hash"]),
                    expected_sources_json=list(item.get("expected_sources", ())),
                    expects_refusal=item["expects_refusal"],
                    evidence_hash=str(item.get("evidence_hash", item["question_hash"])),
                    created_at_utc=now,
                )
            )

    def list_golden_items(
        self,
        connection: Connection,
        *,
        space_id: str,
        golden_version: str,
    ) -> list[Mapping[str, Any]]:
        rows = (
            connection.execute(
                select(evaluation_golden_item_table).where(
                    evaluation_golden_item_table.c.space_id == space_id,
                    evaluation_golden_item_table.c.golden_version == golden_version,
                )
            )
            .mappings()
            .all()
        )
        return [dict(row) for row in rows]

    def count_golden_items(
        self, connection: Connection, *, space_id: str, golden_version: str
    ) -> int:
        return len(
            connection.execute(
                select(evaluation_golden_item_table.c.item_id).where(
                    evaluation_golden_item_table.c.space_id == space_id,
                    evaluation_golden_item_table.c.golden_version == golden_version,
                )
            ).all()
        )

    # ------------------------------------------------------------------- run

    def insert_run(
        self,
        connection: Connection,
        *,
        run_id: str,
        space_id: str,
        policy_version: str,
        comparator_key: str | None,
        candidate_config_versions: tuple[str, ...],
        index_generation_id: str,
        index_revision: int,
        frozen_snapshot: Mapping[str, Any],
        snapshot_id: str,
        sample_items: tuple[Mapping[str, Any], ...],
        now: datetime,
        initiator_user_id: str,
        request_hash: str,
        idempotency_key: str,
    ) -> None:
        connection.execute(
            evaluation_sample_snapshot_table.insert().values(
                snapshot_id=snapshot_id,
                space_id=space_id,
                policy_version=policy_version,
                comparator_key=comparator_key,
                candidate_config_versions_json=list(candidate_config_versions),
                index_generation_id=index_generation_id,
                index_revision=index_revision,
                sample_count=len(sample_items),
                created_at_utc=now,
            )
        )
        for item in sample_items:
            connection.execute(
                evaluation_sample_snapshot_item_table.insert().values(
                    snapshot_id=snapshot_id,
                    item_id=str(item["item_id"]),
                    position=int(item["position"]),
                    question_text=str(item["question_text"]),
                    question_hash=str(item["question_hash"]),
                    evidence_hash=str(item["evidence_hash"]),
                    weak_signals_json=dict(item.get("weak_signals", {})),
                    source_ref=str(item.get("source_ref", "")),
                )
            )
        connection.execute(
            shadow_evaluation_run_table.insert().values(
                run_id=run_id,
                space_id=space_id,
                state="queued",
                attempt=1,
                lease_owner=None,
                lease_expires_at_utc=None,
                heartbeat_at_utc=None,
                fencing_token=None,
                next_attempt_at_utc=now,
                failure_class=None,
                progress_json={"total": len(sample_items), "completed": 0, "failed": 0},
                report_ref=None,
                policy_version=policy_version,
                comparator_key=comparator_key,
                candidate_config_versions_json=list(candidate_config_versions),
                index_generation_id=index_generation_id,
                index_revision=index_revision,
                frozen_snapshot_json=dict(frozen_snapshot),
                created_at_utc=now,
                started_at_utc=None,
                completed_at_utc=None,
                version=1,
            )
        )
        connection.execute(
            evaluation_run_command_table.insert().values(
                operator_user_id=initiator_user_id,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                run_id=run_id,
                response_json={"run_id": run_id, "status": "queued"},
                created_at_utc=now,
                completed_at_utc=now,
            )
        )

    def get_run(self, connection: Connection, *, run_id: str) -> ShadowRunRecord | None:
        row = (
            connection.execute(
                select(shadow_evaluation_run_table).where(
                    shadow_evaluation_run_table.c.run_id == run_id
                )
            )
            .mappings()
            .one_or_none()
        )
        return _run_from_row(row) if row is not None else None

    def find_run_command(
        self, connection: Connection, *, operator_user_id: str, idempotency_key: str
    ) -> Mapping[str, Any] | None:
        row = (
            connection.execute(
                select(evaluation_run_command_table).where(
                    evaluation_run_command_table.c.operator_user_id == operator_user_id,
                    evaluation_run_command_table.c.idempotency_key == idempotency_key,
                )
            )
            .mappings()
            .one_or_none()
        )
        return dict(row) if row is not None else None

    def has_active_run_for_space(self, connection: Connection, *, space_id: str) -> bool:
        return (
            connection.execute(
                select(shadow_evaluation_run_table.c.run_id)
                .where(
                    shadow_evaluation_run_table.c.space_id == space_id,
                    shadow_evaluation_run_table.c.state.in_(NON_TERMINAL_STATES),
                )
                .limit(1)
            ).scalar_one_or_none()
            is not None
        )

    def claim_next(
        self,
        connection: Connection,
        *,
        owner: str,
        lease_ttl_seconds: int,
        now: datetime,
    ) -> ShadowRunRecord | None:
        row = (
            connection.execute(
                select(shadow_evaluation_run_table)
                .where(
                    shadow_evaluation_run_table.c.state.in_(ACTIVE_CLAIM_STATES),
                    shadow_evaluation_run_table.c.next_attempt_at_utc <= now,
                )
                .order_by(shadow_evaluation_run_table.c.created_at_utc.asc())
                .limit(1)
                .with_for_update()
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            return None
        run = _run_from_row(row)
        fencing_token = _new_id("eval_fence")
        started_at = run.started_at if run.started_at is not None else now
        connection.execute(
            update(shadow_evaluation_run_table)
            .where(
                and_(
                    shadow_evaluation_run_table.c.run_id == run.run_id,
                    shadow_evaluation_run_table.c.version == run.version,
                )
            )
            .values(
                state="running",
                lease_owner=owner,
                lease_expires_at_utc=now + timedelta(seconds=lease_ttl_seconds),
                heartbeat_at_utc=now,
                fencing_token=fencing_token,
                next_attempt_at_utc=None,
                started_at_utc=started_at,
                version=run.version + 1,
            )
        )
        updated = self.get_run(connection, run_id=run.run_id)
        assert updated is not None
        return updated

    def fence_matches(
        self,
        connection: Connection,
        *,
        run_id: str,
        attempt: int,
        owner: str,
        fencing_token: str,
    ) -> bool:
        return (
            connection.execute(
                select(shadow_evaluation_run_table.c.run_id).where(
                    and_(
                        shadow_evaluation_run_table.c.run_id == run_id,
                        shadow_evaluation_run_table.c.state == "running",
                        shadow_evaluation_run_table.c.attempt == attempt,
                        shadow_evaluation_run_table.c.lease_owner == owner,
                        shadow_evaluation_run_table.c.fencing_token == fencing_token,
                    )
                )
            ).scalar_one_or_none()
            is not None
        )

    def heartbeat(
        self,
        connection: Connection,
        *,
        run_id: str,
        attempt: int,
        owner: str,
        fencing_token: str,
        now: datetime,
    ) -> bool:
        result = connection.execute(
            update(shadow_evaluation_run_table)
            .where(
                and_(
                    shadow_evaluation_run_table.c.run_id == run_id,
                    shadow_evaluation_run_table.c.state == "running",
                    shadow_evaluation_run_table.c.attempt == attempt,
                    shadow_evaluation_run_table.c.lease_owner == owner,
                    shadow_evaluation_run_table.c.fencing_token == fencing_token,
                )
            )
            .values(heartbeat_at_utc=now)
        )
        return result.rowcount == 1

    def write_progress(
        self,
        connection: Connection,
        *,
        run_id: str,
        attempt: int,
        owner: str,
        fencing_token: str,
        progress: Mapping[str, Any],
    ) -> bool:
        result = connection.execute(
            update(shadow_evaluation_run_table)
            .where(
                and_(
                    shadow_evaluation_run_table.c.run_id == run_id,
                    shadow_evaluation_run_table.c.state == "running",
                    shadow_evaluation_run_table.c.attempt == attempt,
                    shadow_evaluation_run_table.c.lease_owner == owner,
                    shadow_evaluation_run_table.c.fencing_token == fencing_token,
                )
            )
            .values(progress_json=dict(progress))
        )
        return result.rowcount == 1

    def insert_result(
        self,
        connection: Connection,
        *,
        run_id: str,
        sample_item_id: str,
        candidate_config_version: str,
        session_id: str,
        metrics_json: Mapping[str, Any],
        weak_signals_json: Mapping[str, Any],
        judged_at: datetime,
    ) -> None:
        connection.execute(
            shadow_evaluation_result_table.insert().values(
                run_id=run_id,
                sample_item_id=sample_item_id,
                candidate_config_version=candidate_config_version,
                session_id=session_id,
                metrics_json=dict(metrics_json),
                weak_signals_json=dict(weak_signals_json),
                judged_at_utc=judged_at,
            )
        )

    def transition_retry_wait(
        self,
        connection: Connection,
        *,
        run_id: str,
        attempt: int,
        owner: str,
        fencing_token: str,
        next_attempt_at: datetime,
        now: datetime,
        failure_class: str | None = None,
        increment_attempt: bool = False,
    ) -> bool:
        values: dict[str, Any] = {
            "state": "retry_wait",
            "lease_owner": None,
            "lease_expires_at_utc": None,
            "heartbeat_at_utc": None,
            "fencing_token": None,
            "next_attempt_at_utc": next_attempt_at,
            "failure_class": failure_class,
            "version": shadow_evaluation_run_table.c.version + 1,
        }
        if increment_attempt:
            values["attempt"] = shadow_evaluation_run_table.c.attempt + 1
        result = connection.execute(
            update(shadow_evaluation_run_table)
            .where(
                and_(
                    shadow_evaluation_run_table.c.run_id == run_id,
                    shadow_evaluation_run_table.c.state == "running",
                    shadow_evaluation_run_table.c.attempt == attempt,
                    shadow_evaluation_run_table.c.lease_owner == owner,
                    shadow_evaluation_run_table.c.fencing_token == fencing_token,
                )
            )
            .values(**values)
        )
        return result.rowcount == 1

    def transition_terminal(
        self,
        connection: Connection,
        *,
        run_id: str,
        attempt: int,
        owner: str,
        fencing_token: str,
        to_state: str,
        now: datetime,
        failure_class: str | None = None,
        progress: Mapping[str, Any] | None = None,
        report_ref: str | None = None,
    ) -> bool:
        if to_state not in TERMINAL_STATES:
            raise PlatformError("validation_error", "terminal state is invalid", {}, 422)
        values: dict[str, Any] = {
            "state": to_state,
            "lease_owner": None,
            "lease_expires_at_utc": None,
            "heartbeat_at_utc": None,
            "fencing_token": None,
            "next_attempt_at_utc": None,
            "completed_at_utc": now,
            "version": shadow_evaluation_run_table.c.version + 1,
        }
        if failure_class is not None:
            values["failure_class"] = failure_class
        if progress is not None:
            values["progress_json"] = dict(progress)
        if report_ref is not None:
            values["report_ref"] = report_ref
        result = connection.execute(
            update(shadow_evaluation_run_table)
            .where(
                and_(
                    shadow_evaluation_run_table.c.run_id == run_id,
                    shadow_evaluation_run_table.c.state.in_(NON_TERMINAL_STATES),
                    shadow_evaluation_run_table.c.attempt == attempt,
                    shadow_evaluation_run_table.c.lease_owner == owner,
                    shadow_evaluation_run_table.c.fencing_token == fencing_token,
                )
            )
            .values(**values)
        )
        return result.rowcount == 1

    def recover_expired(
        self,
        connection: Connection,
        *,
        run_id: str,
        attempt: int,
        next_attempt_at: datetime,
        now: datetime,
        max_attempts: int,
    ) -> bool:
        """Invalidate an expired running attempt and move to retry_wait (A12)."""
        run = self.get_run(connection, run_id=run_id)
        if run is None or run.state != "running" or run.attempt != attempt:
            return False
        if attempt >= max_attempts:
            return self._force_failed(
                connection, run_id=run_id, now=now, failure_class="lease_expired"
            )
        result = connection.execute(
            update(shadow_evaluation_run_table)
            .where(
                and_(
                    shadow_evaluation_run_table.c.run_id == run_id,
                    shadow_evaluation_run_table.c.state == "running",
                    shadow_evaluation_run_table.c.attempt == attempt,
                )
            )
            .values(
                state="retry_wait",
                attempt=attempt + 1,
                lease_owner=None,
                lease_expires_at_utc=None,
                heartbeat_at_utc=None,
                fencing_token=None,
                next_attempt_at_utc=next_attempt_at,
                failure_class="lease_expired",
                version=shadow_evaluation_run_table.c.version + 1,
            )
        )
        return result.rowcount == 1

    def _force_failed(
        self,
        connection: Connection,
        *,
        run_id: str,
        now: datetime,
        failure_class: str | None,
    ) -> bool:
        result = connection.execute(
            update(shadow_evaluation_run_table)
            .where(
                and_(
                    shadow_evaluation_run_table.c.run_id == run_id,
                    shadow_evaluation_run_table.c.state.in_(NON_TERMINAL_STATES),
                )
            )
            .values(
                state="failed",
                lease_owner=None,
                lease_expires_at_utc=None,
                heartbeat_at_utc=None,
                fencing_token=None,
                next_attempt_at_utc=None,
                failure_class=failure_class,
                completed_at_utc=now,
                version=shadow_evaluation_run_table.c.version + 1,
            )
        )
        return result.rowcount == 1

    def requeue_retry_wait(self, connection: Connection, *, now: datetime) -> int:
        result = connection.execute(
            update(shadow_evaluation_run_table)
            .where(
                and_(
                    shadow_evaluation_run_table.c.state == "retry_wait",
                    shadow_evaluation_run_table.c.next_attempt_at_utc <= now,
                )
            )
            .values(
                state="queued",
                next_attempt_at_utc=now,
                version=shadow_evaluation_run_table.c.version + 1,
            )
        )
        return result.rowcount

    def list_expired_running(self, connection: Connection, *, now: datetime) -> list[str]:
        rows = connection.execute(
            select(shadow_evaluation_run_table.c.run_id).where(
                and_(
                    shadow_evaluation_run_table.c.state == "running",
                    shadow_evaluation_run_table.c.lease_expires_at_utc <= now,
                )
            )
        ).all()
        return [str(row[0]) for row in rows]

    # -------------------------------------------------------------- results

    def list_results(self, connection: Connection, *, run_id: str) -> list[Mapping[str, Any]]:
        rows = (
            connection.execute(
                select(shadow_evaluation_result_table).where(
                    shadow_evaluation_result_table.c.run_id == run_id
                )
            )
            .mappings()
            .all()
        )
        return [dict(row) for row in rows]

    def latest_succeeded_run(
        self,
        connection: Connection,
        *,
        space_id: str,
        comparator_key: str,
    ) -> ShadowRunRecord | None:
        """The most recently completed successful run for a space and key (A26)."""
        row = (
            connection.execute(
                select(shadow_evaluation_run_table)
                .where(
                    shadow_evaluation_run_table.c.space_id == space_id,
                    shadow_evaluation_run_table.c.comparator_key == comparator_key,
                    shadow_evaluation_run_table.c.state == "succeeded",
                )
                .order_by(shadow_evaluation_run_table.c.completed_at_utc.desc())
                .limit(1)
            )
            .mappings()
            .one_or_none()
        )
        return _run_from_row(row) if row is not None else None

    # ------------------------------------------------------------ suggestion

    def create_suggestion(
        self,
        connection: Connection,
        *,
        suggestion_id: str,
        space_id: str,
        policy_version: str,
        comparator_key: str | None,
        rank_summary: Mapping[str, Any],
        now: datetime,
    ) -> SuggestionRecord:
        connection.execute(
            calibration_window_suggestion_table.insert().values(
                suggestion_id=suggestion_id,
                space_id=space_id,
                policy_version=policy_version,
                comparator_key=comparator_key,
                rank_summary_json=dict(rank_summary),
                status="not_actionable",
                version=1,
                created_at_utc=now,
                invalidated_at_utc=None,
                consumed_at_utc=None,
            )
        )
        record = self.get_suggestion(connection, suggestion_id=suggestion_id)
        assert record is not None
        return record

    def get_suggestion(
        self, connection: Connection, *, suggestion_id: str
    ) -> SuggestionRecord | None:
        row = (
            connection.execute(
                select(calibration_window_suggestion_table).where(
                    calibration_window_suggestion_table.c.suggestion_id == suggestion_id
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            return None
        return SuggestionRecord(
            suggestion_id=str(row["suggestion_id"]),
            space_id=str(row["space_id"]),
            policy_version=str(row["policy_version"]),
            comparator_key=(
                str(row["comparator_key"]) if row["comparator_key"] is not None else None
            ),
            rank_summary=dict(row["rank_summary_json"] or {}),
            status=str(row["status"]),
            version=int(row["version"]),
            created_at=_utc(row["created_at_utc"]),  # type: ignore[arg-type]
            invalidated_at=_utc(row["invalidated_at_utc"]),
            consumed_at=_utc(row["consumed_at_utc"]),
        )

    def transition_suggestion(
        self,
        connection: Connection,
        *,
        suggestion_id: str,
        from_status: str,
        to_status: str,
        now: datetime,
    ) -> int:
        """Atomically transition a suggestion and return its new version (A25)."""
        values: dict[str, Any] = {
            "status": to_status,
            "version": calibration_window_suggestion_table.c.version + 1,
        }
        if to_status == "consumed":
            values["consumed_at_utc"] = now
        elif to_status == "superseded":
            values["invalidated_at_utc"] = now
        result = connection.execute(
            update(calibration_window_suggestion_table)
            .where(
                and_(
                    calibration_window_suggestion_table.c.suggestion_id == suggestion_id,
                    calibration_window_suggestion_table.c.status == from_status,
                )
            )
            .values(**values)
        )
        if result.rowcount != 1:
            return 0
        return int(
            connection.execute(
                select(calibration_window_suggestion_table.c.version).where(
                    calibration_window_suggestion_table.c.suggestion_id == suggestion_id
                )
            ).scalar_one()
        )

    def latest_actionable_suggestion(
        self,
        connection: Connection,
        *,
        space_id: str,
        comparator_key: str,
    ) -> SuggestionRecord | None:
        row = (
            connection.execute(
                select(calibration_window_suggestion_table)
                .where(
                    calibration_window_suggestion_table.c.space_id == space_id,
                    calibration_window_suggestion_table.c.comparator_key == comparator_key,
                    calibration_window_suggestion_table.c.status == "actionable",
                )
                .order_by(calibration_window_suggestion_table.c.version.desc())
                .limit(1)
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            return None
        return SuggestionRecord(
            suggestion_id=str(row["suggestion_id"]),
            space_id=str(row["space_id"]),
            policy_version=str(row["policy_version"]),
            comparator_key=(
                str(row["comparator_key"]) if row["comparator_key"] is not None else None
            ),
            rank_summary=dict(row["rank_summary_json"] or {}),
            status=str(row["status"]),
            version=int(row["version"]),
            created_at=_utc(row["created_at_utc"]),  # type: ignore[arg-type]
            invalidated_at=_utc(row["invalidated_at_utc"]),
            consumed_at=_utc(row["consumed_at_utc"]),
        )

    def supersede_actionable_suggestions(
        self,
        connection: Connection,
        *,
        space_id: str,
        comparator_key: str,
        now: datetime,
    ) -> int:
        result = connection.execute(
            update(calibration_window_suggestion_table)
            .where(
                and_(
                    calibration_window_suggestion_table.c.space_id == space_id,
                    calibration_window_suggestion_table.c.comparator_key == comparator_key,
                    calibration_window_suggestion_table.c.status == "actionable",
                )
            )
            .values(
                status="superseded",
                version=calibration_window_suggestion_table.c.version + 1,
                invalidated_at_utc=now,
            )
        )
        return result.rowcount

    # ---------------------------------------------------------------- window

    def get_open_window(self, connection: Connection) -> WindowSnapshot | None:
        row = (
            connection.execute(
                select(calibration_window_table)
                .where(calibration_window_table.c.status == "open")
                .order_by(calibration_window_table.c.opened_at_utc.desc())
                .limit(1)
            )
            .mappings()
            .one_or_none()
        )
        return _window_from_row(row) if row is not None else None

    def get_closing_window(self, connection: Connection) -> WindowSnapshot | None:
        row = (
            connection.execute(
                select(calibration_window_table)
                .where(calibration_window_table.c.status == "closing")
                .order_by(calibration_window_table.c.opened_at_utc.desc())
                .limit(1)
            )
            .mappings()
            .one_or_none()
        )
        return _window_from_row(row) if row is not None else None

    def current_window(self, connection: Connection) -> WindowSnapshot | None:
        return self.get_open_window(connection) or self.get_closing_window(connection)

    def create_window(
        self,
        connection: Connection,
        *,
        window_id: str,
        status: str,
        window_kind: str,
        policy_version: str,
        sample_rate: float,
        opened_by: str,
        now: datetime,
        close_deadline_at: datetime | None = None,
    ) -> WindowSnapshot:
        connection.execute(
            calibration_window_table.insert().values(
                window_id=window_id,
                status=status,
                window_kind=window_kind,
                policy_version=policy_version,
                sample_rate=sample_rate,
                pairs_collected=0,
                opened_by=opened_by,
                opened_at_utc=now,
                closed_by=None,
                closed_at_utc=None,
                close_deadline_at_utc=close_deadline_at,
                version=1,
                created_at_utc=now,
                updated_at_utc=now,
            )
        )
        window = self.get_window_by_id(connection, window_id=window_id)
        assert window is not None
        return window

    def get_window_by_id(self, connection: Connection, *, window_id: str) -> WindowSnapshot | None:
        row = (
            connection.execute(
                select(calibration_window_table).where(
                    calibration_window_table.c.window_id == window_id
                )
            )
            .mappings()
            .one_or_none()
        )
        return _window_from_row(row) if row is not None else None

    def close_window(
        self,
        connection: Connection,
        *,
        window_id: str,
        closed_by: str,
        close_deadline_at: datetime,
        now: datetime,
    ) -> bool:
        result = connection.execute(
            update(calibration_window_table)
            .where(
                and_(
                    calibration_window_table.c.window_id == window_id,
                    calibration_window_table.c.status == "open",
                )
            )
            .values(
                status="closing",
                closed_by=closed_by,
                close_deadline_at_utc=close_deadline_at,
                version=calibration_window_table.c.version + 1,
                updated_at_utc=now,
            )
        )
        return result.rowcount == 1

    def finalize_window(
        self,
        connection: Connection,
        *,
        window_id: str,
        now: datetime,
    ) -> bool:
        result = connection.execute(
            update(calibration_window_table)
            .where(
                and_(
                    calibration_window_table.c.window_id == window_id,
                    calibration_window_table.c.status == "closing",
                )
            )
            .values(
                status="closed",
                closed_at_utc=now,
                version=calibration_window_table.c.version + 1,
                updated_at_utc=now,
            )
        )
        return result.rowcount == 1

    def increment_pairs_collected(self, connection: Connection, *, window_id: str) -> None:
        result = connection.execute(
            update(calibration_window_table)
            .where(calibration_window_table.c.window_id == window_id)
            .values(
                pairs_collected=calibration_window_table.c.pairs_collected + 1,
                version=calibration_window_table.c.version + 1,
                updated_at_utc=datetime.now(UTC),
            )
        )
        # 0 rows affected means the window no longer exists: silently ignore to
        # avoid miscounting a vote against a vanished window (A32).
        del result

    def find_window_command(
        self,
        connection: Connection,
        *,
        operator_user_id: str,
        idempotency_key: str,
    ) -> Mapping[str, Any] | None:
        row = (
            connection.execute(
                select(calibration_window_command_table).where(
                    calibration_window_command_table.c.operator_user_id == operator_user_id,
                    calibration_window_command_table.c.idempotency_key == idempotency_key,
                )
            )
            .mappings()
            .one_or_none()
        )
        return dict(row) if row is not None else None

    def insert_window_command(
        self,
        connection: Connection,
        *,
        operator_user_id: str,
        idempotency_key: str,
        action: str,
        request_hash: str,
        target_window_id: str | None,
        response_json: Mapping[str, Any],
        now: datetime,
    ) -> None:
        connection.execute(
            calibration_window_command_table.insert().values(
                operator_user_id=operator_user_id,
                idempotency_key=idempotency_key,
                action=action,
                request_hash=request_hash,
                target_window_id=target_window_id,
                response_json=dict(response_json),
                created_at_utc=now,
                completed_at_utc=now,
            )
        )

    # --------------------------------------------------------- active default

    def get_active_default(
        self, connection: Connection, *, space_id: str
    ) -> Mapping[str, Any] | None:
        row = (
            connection.execute(
                select(evaluation_active_default_table).where(
                    evaluation_active_default_table.c.space_id == space_id
                )
            )
            .mappings()
            .one_or_none()
        )
        return dict(row) if row is not None else None

    def set_active_default(
        self,
        connection: Connection,
        *,
        space_id: str,
        candidate_config_version: str,
        comparator_key: str | None,
        source_run_id: str | None,
        now: datetime,
    ) -> None:
        existing = connection.execute(
            select(evaluation_active_default_table.c.space_id).where(
                evaluation_active_default_table.c.space_id == space_id
            )
        ).scalar_one_or_none()
        if existing is not None:
            connection.execute(
                update(evaluation_active_default_table)
                .where(evaluation_active_default_table.c.space_id == space_id)
                .values(
                    candidate_config_version=candidate_config_version,
                    comparator_key=comparator_key,
                    source_run_id=source_run_id,
                    adopted_at_utc=now,
                )
            )
        else:
            connection.execute(
                evaluation_active_default_table.insert().values(
                    space_id=space_id,
                    candidate_config_version=candidate_config_version,
                    comparator_key=comparator_key,
                    adopted_at_utc=now,
                    source_run_id=source_run_id,
                )
            )


__all__ = [
    "NON_TERMINAL_STATES",
    "RUN_STATES",
    "TERMINAL_STATES",
    "SqlAlchemyEvaluationRepository",
]
