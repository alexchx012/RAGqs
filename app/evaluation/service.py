"""Evaluation & calibration domain services."""

from __future__ import annotations

import hashlib
import json
import math
import secrets
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.engine import Connection, Engine

from app.identity.schema import identity_space_table
from app.platform.errors import PlatformError

from .judge import JudgePreflight, JudgeProviderPort
from .models import (
    EvaluationPolicySnapshot,
    LeaderboardEntry,
    RunReadModel,
)
from .policy import (
    build_comparator_key,
    default_policy_snapshot,
    policy_view,
    threshold_eligibility,
    validate_policy,
    weighted_score,
)
from .ports import (
    CalibrationOutboxPort,
    CandidateConfigSourcePort,
    ChatFactsPort,
    IndexGenerationSourcePort,
    RetrievalReplayPort,
    SpaceVisibilityPort,
)
from .repository import SqlAlchemyEvaluationRepository
from .schema import (
    calibration_window_suggestion_table,
    evaluation_active_default_table,
    shadow_evaluation_run_table,
)


def _new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_urlsafe(15)}"


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _request_hash(kind: str, payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        {"kind": kind, **{str(key): payload[key] for key in sorted(payload)}},
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(b"evaluation-request-v1\0" + encoded.encode("utf-8")).hexdigest()


class EvaluationService:
    """Shadow-evaluation run orchestration (handler only enqueues, never runs)."""

    def __init__(
        self,
        engine: Engine,
        repository: SqlAlchemyEvaluationRepository,
        *,
        judge: JudgeProviderPort,
        chat_facts: ChatFactsPort,
        candidate_configs: CandidateConfigSourcePort,
        index_generation: IndexGenerationSourcePort,
        retrieval: RetrievalReplayPort | None = None,
        space_visibility: SpaceVisibilityPort | None = None,
        now: Any = None,
    ) -> None:
        self._engine = engine
        self._repository = repository
        self._judge = judge
        self._chat_facts = chat_facts
        self._candidate_configs = candidate_configs
        self._index_generation = index_generation
        self._retrieval = retrieval
        self._space_visibility = space_visibility
        self._now = now or (lambda connection: datetime.now(UTC))
        self._preflight = JudgePreflight(judge)
        self._calibration_outbox: CalibrationOutboxPort | None = None

    def _now_utc(self, connection: Connection) -> datetime:
        value = self._now(connection)
        if not isinstance(value, datetime):
            value = datetime.now(UTC)
        return _utc(value)

    @staticmethod
    def _require_ops(principal: Any) -> None:
        if getattr(principal, "role", None) != "ops":
            raise PlatformError(
                "evaluation_run_forbidden",
                "The evaluation:run capability is required",
                {},
                403,
            )

    @staticmethod
    def _space_exists(connection: Connection, space_id: str) -> bool:
        return (
            connection.execute(
                select(identity_space_table.c.id).where(identity_space_table.c.id == space_id)
            ).scalar_one_or_none()
            is not None
        )

    def create_shadow_run(
        self,
        actor: Any,
        space_id: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        self._require_ops(actor)
        user_id = str(getattr(actor, "user_id", ""))
        request_hash = _request_hash("shadow_run", {"space_id": space_id})
        with self._engine.begin() as connection:
            replay = self._repository.find_run_command(
                connection,
                operator_user_id=user_id,
                idempotency_key=idempotency_key,
            )
            if replay is not None:
                if str(replay["request_hash"]) != request_hash:
                    raise PlatformError(
                        "idempotency_key_conflict",
                        "The idempotency key was already used with a different request",
                        {},
                        409,
                    )
                return dict(replay["response_json"])
            if not self._space_exists(connection, space_id):
                raise PlatformError(
                    "evaluation_space_unavailable",
                    "The space is unavailable for evaluation",
                    {},
                    404,
                )
            policy = self._repository.latest_policy(connection)
            if policy is None:
                raise PlatformError(
                    "evaluation_not_eligible",
                    "No evaluation policy is available",
                    {},
                    409,
                )
            if self._repository.has_active_run_for_space(connection, space_id=space_id):
                raise PlatformError(
                    "shadow_evaluation_in_progress",
                    "A shadow evaluation is already in progress for this space",
                    {},
                    409,
                )
            samples = self._chat_facts.collect_samples(
                connection,
                space_id=space_id,
                limit=policy.shadow_max_examples,
            )
            if len(samples) < policy.min_real_queries:
                raise PlatformError(
                    "evaluation_not_eligible",
                    "The space has fewer real questions than the policy minimum",
                    {},
                    409,
                )
            self._preflight.verify_run()
            candidate_config_versions = self._candidate_configs.candidate_config_versions(
                space_id=space_id
            )
            if not candidate_config_versions:
                raise PlatformError(
                    "evaluation_not_eligible",
                    "No candidate configurations are available",
                    {},
                    409,
                )
            candidate_config_versions = candidate_config_versions[
                : policy.shadow_max_candidate_configs
            ]
            index_generation_id, index_revision = self._index_generation.active_generation()
            golden_version = self._repository.latest_golden_set_version(
                connection, space_id=space_id
            )
            comparator_key = build_comparator_key(
                golden_set_version=golden_version,
                judge_provider=getattr(self._judge, "provider", "bailian"),
                judge_model=getattr(self._judge, "model", "qwen3.7-plus"),
                judge_mode=getattr(self._judge, "mode", "non_thinking"),
                judge_capability=getattr(self._judge, "capability", "qwen3.7-plus"),
                judge_release=getattr(self._judge, "release", "stable"),
                judge_prompt_version=getattr(self._judge, "prompt_version", "v1"),
                judge_k=policy.judge_k,
            )
            run_id = _new_id("eval_run")
            snapshot_id = _new_id("eval_snapshot")
            now = self._now_utc(connection)
            frozen_snapshot = {
                "snapshot_id": snapshot_id,
                "space_id": space_id,
                "policy_version": policy.policy_version,
                "candidate_config_versions": list(candidate_config_versions),
                "index_generation_id": index_generation_id,
                "index_revision": index_revision,
                "golden_set_version": golden_version,
                "comparator_key": comparator_key,
                "judge": {
                    "provider": getattr(self._judge, "provider", "bailian"),
                    "model": getattr(self._judge, "model", "qwen3.7-plus"),
                    "mode": getattr(self._judge, "mode", "non_thinking"),
                    "capability": getattr(self._judge, "capability", "qwen3.7-plus"),
                    "release": getattr(self._judge, "release", "stable"),
                    "prompt_version": getattr(self._judge, "prompt_version", "v1"),
                    "k": policy.judge_k,
                },
                "limits": {
                    "examples": policy.shadow_max_examples,
                    "candidate_configs": policy.shadow_max_candidate_configs,
                    "concurrency": policy.concurrency,
                    "deadline_seconds": policy.run_deadline_seconds,
                    "lease_seconds": policy.lease_seconds,
                    "heartbeat_seconds": policy.heartbeat_seconds,
                    "max_attempts": policy.max_attempts,
                },
                "initiator_user_id": user_id,
                "request_hash": request_hash,
                "session_prefix": f"shadow:{run_id}",
            }
            self._repository.insert_run(
                connection,
                run_id=run_id,
                space_id=space_id,
                policy_version=policy.policy_version,
                comparator_key=comparator_key,
                candidate_config_versions=candidate_config_versions,
                index_generation_id=index_generation_id,
                index_revision=index_revision,
                frozen_snapshot=frozen_snapshot,
                snapshot_id=snapshot_id,
                sample_items=samples,
                now=now,
                initiator_user_id=user_id,
                request_hash=request_hash,
                idempotency_key=idempotency_key,
            )
            return {"run_id": run_id, "status": "queued"}

    def get_shadow_run(self, run_id: str, actor: Any) -> RunReadModel:
        if getattr(actor, "role", None) not in {"ops", "admin"}:
            raise PlatformError(
                "evaluation_run_forbidden",
                "The evaluation:run read capability is required",
                {},
                403,
            )
        with self._engine.connect() as connection:
            record = self._repository.get_run(connection, run_id=run_id)
            if record is None:
                raise PlatformError(
                    "shadow_evaluation_not_found",
                    "The shadow evaluation run was not found",
                    {},
                    404,
                )
        return RunReadModel(
            run_id=record.run_id,
            state=record.state,
            attempt=record.attempt,
            progress=record.progress,
            failure_class=record.failure_class,
            report_ref=record.report_ref,
            policy_version=record.policy_version,
            comparator_key=record.comparator_key,
            candidate_config_versions=record.candidate_config_versions,
            index_generation_id=record.index_generation_id,
            index_revision=record.index_revision,
            created_at=record.created_at,
            started_at=record.started_at,
            completed_at=record.completed_at,
        )

    # -------------------------------------------------------------- leaderboard

    def leaderboard(self, actor: Any) -> dict[str, Any]:
        if getattr(actor, "role", None) not in {"ops", "admin"}:
            raise PlatformError(
                "evaluation_leaderboard_forbidden",
                "Leaderboard access requires ops or admin",
                {},
                403,
            )
        with self._engine.connect() as connection:
            policy = self._repository.latest_policy(connection)
            if policy is None:
                policy = default_policy_snapshot(now=self._now_utc(connection))
            validate_policy(policy)
            entries = self._build_entries(connection, actor=actor)
            shadow_entries = self._build_shadow_entries(connection, policy, actor=actor)
            return {
                "entries": [entry.to_json() for entry in entries],
                "shadow_entries": [entry.to_json() for entry in shadow_entries],
                "policy": policy_view(policy),
            }

    def _visible_space_ids(self, actor: Any) -> frozenset[str] | None:
        if self._space_visibility is None:
            return None
        return self._space_visibility.visible_space_ids(actor)

    def _build_entries(
        self,
        connection: Connection,
        *,
        actor: Any,
    ) -> list[LeaderboardEntry]:
        rows = connection.execute(select(evaluation_active_default_table)).mappings().all()
        visible = self._visible_space_ids(actor)
        entries: list[LeaderboardEntry] = []
        for row in rows:
            space_id = str(row["space_id"])
            if visible is not None and space_id not in visible:
                # Aggregation only happens over spaces the principal may
                # retrieve from (A24).
                continue
            entries.append(
                LeaderboardEntry(
                    rank=0,
                    name=str(row["candidate_config_version"]),
                    score=0.0,
                    metrics={},
                    eligible=True,
                    is_active=True,
                )
            )
        return [
            LeaderboardEntry(
                rank=index,
                name=item.name,
                score=item.score,
                metrics=item.metrics,
                eligible=item.eligible,
                is_active=item.is_active,
            )
            for index, item in enumerate(entries, start=1)
        ]

    def _build_shadow_entries(
        self,
        connection: Connection,
        policy: EvaluationPolicySnapshot,
        *,
        actor: Any,
    ) -> list[LeaderboardEntry]:
        run_rows = (
            connection.execute(
                select(shadow_evaluation_run_table)
                .where(shadow_evaluation_run_table.c.state == "succeeded")
                .order_by(shadow_evaluation_run_table.c.completed_at_utc.desc())
            )
            .mappings()
            .all()
        )
        visible = self._visible_space_ids(actor)
        entries: list[LeaderboardEntry] = []
        for run_row in run_rows:
            space_id = str(run_row["space_id"])
            if visible is not None and space_id not in visible:
                continue
            results = self._repository.list_results(connection, run_id=str(run_row["run_id"]))
            by_config: dict[str, list[Mapping[str, Any]]] = {}
            for result in results:
                by_config.setdefault(str(result["candidate_config_version"]), []).append(result)
            aggregated: list[LeaderboardEntry] = []
            for config, config_results in by_config.items():
                metrics = self._aggregate_result_metrics(config_results)
                eligible = threshold_eligibility(metrics, policy)
                aggregated.append(
                    LeaderboardEntry(
                        rank=0,
                        name=config,
                        score=weighted_score(metrics) if eligible else 0.0,
                        metrics=metrics,
                        eligible=eligible,
                        is_active=False,
                    )
                )
            aggregated.sort(key=lambda item: item.score, reverse=True)
            for rank, item in enumerate(aggregated, start=1):
                entries.append(
                    LeaderboardEntry(
                        rank=rank,
                        name=item.name,
                        score=item.score,
                        metrics=item.metrics,
                        eligible=item.eligible,
                        is_active=False,
                    )
                )
        return entries

    @staticmethod
    def _aggregate_result_metrics(results: list[Mapping[str, Any]]) -> dict[str, float]:
        keys = (
            "faithfulness",
            "answer_relevancy",
            "refusal_rate",
            "hit_at_k_final",
            "mrr",
            "p95_latency_ms",
            "cost_per_query",
        )
        aggregates: dict[str, float] = {}
        for key in keys:
            numbers = [
                float(result["metrics_json"].get(key))
                for result in results
                if result["metrics_json"] and result["metrics_json"].get(key) is not None
            ]
            if key == "p95_latency_ms" and numbers:
                rank = math.ceil(len(numbers) * 0.95)
                aggregates[key] = sorted(numbers)[rank - 1]
            else:
                aggregates[key] = (sum(numbers) / len(numbers)) if numbers else 0.0
        return aggregates

    # -------------------------------------------------------------- suggestion

    def compute_suggestion(self, run_id: str) -> None:
        with self._engine.begin() as connection:
            run = self._repository.get_run(connection, run_id=run_id)
            if run is None or run.state != "succeeded":
                return
            latest = self._repository.latest_succeeded_run(
                connection,
                space_id=run.space_id,
                comparator_key=run.comparator_key or "",
            )
            if latest is None or latest.run_id != run.run_id:
                # Only the latest complete success with the same comparator key
                # may drive a suggestion (A26).
                return
            policy = self._repository.get_policy(connection, policy_version=run.policy_version)
            if policy is None:
                return
            validate_policy(policy)
            results = self._repository.list_results(connection, run_id=run.run_id)
            distinct_samples = len({str(result["sample_item_id"]) for result in results})
            if distinct_samples < policy.min_real_queries:
                return
            by_config: dict[str, list[Mapping[str, Any]]] = {}
            for result in results:
                by_config.setdefault(str(result["candidate_config_version"]), []).append(result)
            scored: list[tuple[str, float, bool]] = []
            for config, config_results in by_config.items():
                metrics = self._aggregate_result_metrics(config_results)
                eligible = threshold_eligibility(metrics, policy)
                scored.append((config, weighted_score(metrics), eligible))
            if len(scored) < 2:
                return
            scored.sort(key=lambda item: item[1], reverse=True)
            first = scored[0]
            second = scored[1]
            if not first[2] or not second[2]:
                return
            if abs(first[1] - second[1]) > policy.calibration_open_score_gap:
                return
            now = self._now_utc(connection)
            actionable = self._repository.latest_actionable_suggestion(
                connection,
                space_id=run.space_id,
                comparator_key=run.comparator_key or "",
            )
            if actionable is not None:
                if actionable.rank_summary.get("source_run_id") == run.run_id:
                    # Duplicate computation must not publish (A27).
                    return
                self._repository.supersede_actionable_suggestions(
                    connection,
                    space_id=run.space_id,
                    comparator_key=run.comparator_key or "",
                    now=now,
                )
            suggestion_id = _new_id("suggestion")
            self._repository.create_suggestion(
                connection,
                suggestion_id=suggestion_id,
                space_id=run.space_id,
                policy_version=run.policy_version,
                comparator_key=run.comparator_key,
                rank_summary={
                    "source_run_id": run.run_id,
                    "rankings": [
                        {"name": name, "score": score, "eligible": eligible}
                        for name, score, eligible in scored
                    ],
                },
                now=now,
            )
            version = self._repository.transition_suggestion(
                connection,
                suggestion_id=suggestion_id,
                from_status="not_actionable",
                to_status="actionable",
                now=now,
            )
            if version > 1 and self._calibration_outbox is not None:
                self._calibration_outbox.publish_suggested(
                    suggestion_id=suggestion_id,
                    transition_version=version,
                    occurred_at=now,
                    connection=connection,
                )

    def attach_outbox(self, outbox: CalibrationOutboxPort) -> None:
        self._calibration_outbox = outbox

    # ---------------------------------------------------------------- golden

    def _maybe_auto_shadow_run(self, space_id: str, golden_version: str) -> None:
        """Attempt to auto-trigger a shadow evaluation after golden set adoption.

        Uses the existing create_shadow_run path with a system actor and a
        deterministic idempotency key. Non-eligibility (not enough samples,
        active run, missing policy/config) is silently skipped; unexpected
        errors propagate.
        """

        class _SystemActor:
            user_id = "system:evaluation"
            role = "ops"
            auth_session_id = "system"
            username = "system"
            department_id = None

        _AUTO_ELIGIBLE = frozenset(
            {
                "evaluation_not_eligible",
                "shadow_evaluation_in_progress",
                "evaluation_space_unavailable",
            }
        )
        try:
            self.create_shadow_run(
                _SystemActor(),
                space_id=space_id,
                idempotency_key=f"golden-auto:{space_id}:{golden_version}",
            )
        except PlatformError as exc:
            if exc.code not in _AUTO_ELIGIBLE:
                raise

    def publish_golden_set(
        self,
        *,
        space_id: str,
        version: str,
        items: tuple[Mapping[str, Any], ...],
    ) -> str:
        """Publish one immutable, versioned golden set for a space (A13).

        Deployment-side entry point (no HTTP API): new/revisioned sets always
        create a new version row and never backfill or rewrite existing
        results. Each item carries ``question_text``, ``expected_sources``,
        an explicit boolean ``expects_refusal`` and optional ``evidence_hash``;
        the question hash uses the same canonical algorithm as the chat-facts
        snapshot so worker golden matching is exact.
        """
        if not version.strip():
            raise PlatformError("validation_error", "golden set version is required", {}, 422)
        normalized: list[dict[str, Any]] = []
        for item in items:
            question = str(item.get("question_text", "")).strip()
            if not question:
                raise PlatformError(
                    "validation_error", "golden item question_text is required", {}, 422
                )
            if not isinstance(item.get("expects_refusal"), bool):
                raise PlatformError(
                    "validation_error",
                    "golden item expects_refusal must be an explicit boolean",
                    {},
                    422,
                )
            expected_sources = list(item.get("expected_sources", ()))
            normalized.append(
                {
                    "question_text": question,
                    "question_hash": hashlib.sha256(question.encode("utf-8")).hexdigest(),
                    "expected_sources": expected_sources,
                    "expects_refusal": item["expects_refusal"],
                    "evidence_hash": str(item.get("evidence_hash", ""))
                    or hashlib.sha256(question.encode("utf-8")).hexdigest(),
                }
            )
        with self._engine.begin() as connection:
            self._repository.publish_golden_set_version(
                connection,
                space_id=space_id,
                version=version,
                items=tuple(normalized),
                now=self._now_utc(connection),
            )
        self._maybe_auto_shadow_run(space_id, version)
        return version

    # ---------------------------------------------------------------- window

    def open_window(
        self,
        actor: Any,
        action: str,
        window_kind: str | None,
        idempotency_key: str,
    ) -> tuple[dict[str, Any], int]:
        self._require_ops(actor)
        if action != "open" or window_kind is None:
            raise PlatformError("validation_error", "window_kind is required for open", {}, 422)
        if window_kind not in {"cold_start", "sentinel", "manual"}:
            raise PlatformError("validation_error", "window_kind is invalid", {}, 422)
        user_id = str(getattr(actor, "user_id", ""))
        request_hash = _request_hash("window", {"action": action, "window_kind": window_kind})
        with self._engine.begin() as connection:
            replay = self._repository.find_window_command(
                connection, operator_user_id=user_id, idempotency_key=idempotency_key
            )
            if replay is not None:
                if str(replay["request_hash"]) != request_hash:
                    raise PlatformError(
                        "idempotency_key_conflict",
                        "The idempotency key was already used with a different request",
                        {},
                        409,
                    )
                return dict(replay["response_json"]), 201 if replay["action"] == "open" else 200
            existing = self._repository.get_open_window(connection)
            if existing is not None:
                raise PlatformError(
                    "calibration_window_already_open",
                    "A calibration window is already open",
                    {},
                    409,
                )
            if self._repository.get_closing_window(connection) is not None:
                raise PlatformError(
                    "calibration_window_closing",
                    "A calibration window is closing",
                    {},
                    409,
                )
            policy = self._repository.latest_policy(connection)
            if policy is None:
                policy = default_policy_snapshot(now=self._now_utc(connection))
            validate_policy(policy)
            suggestion_space_id = None
            if window_kind in {"cold_start", "sentinel"}:
                suggestion_space_id = self._actionable_space_id(connection)
                if suggestion_space_id is None:
                    raise PlatformError(
                        "calibration_window_not_eligible",
                        "No actionable suggestion is available",
                        {},
                        409,
                    )
            sample_rate = (
                policy.cold_start_sample_rate
                if window_kind == "cold_start"
                else policy.sentinel_sample_rate
            )
            now = self._now_utc(connection)
            window_id = _new_id("window")
            window = self._repository.create_window(
                connection,
                window_id=window_id,
                status="open",
                window_kind=window_kind,
                policy_version=policy.policy_version,
                sample_rate=sample_rate,
                opened_by=user_id,
                now=now,
            )
            response = window.to_json()
            self._repository.insert_window_command(
                connection,
                operator_user_id=user_id,
                idempotency_key=idempotency_key,
                action="open",
                request_hash=request_hash,
                target_window_id=window_id,
                response_json=response,
                now=now,
            )
            return response, 201

    @staticmethod
    def _actionable_space_id(connection: Connection) -> str | None:
        row = (
            connection.execute(
                select(calibration_window_suggestion_table.c.space_id)
                .where(calibration_window_suggestion_table.c.status == "actionable")
                .limit(1)
            )
            .mappings()
            .first()
        )
        return str(row["space_id"]) if row is not None else None

    def close_window(
        self,
        actor: Any,
        idempotency_key: str,
    ) -> tuple[dict[str, Any], int]:
        self._require_ops(actor)
        user_id = str(getattr(actor, "user_id", ""))
        request_hash = _request_hash("window", {"action": "close"})
        with self._engine.begin() as connection:
            replay = self._repository.find_window_command(
                connection, operator_user_id=user_id, idempotency_key=idempotency_key
            )
            if replay is not None:
                if str(replay["request_hash"]) != request_hash:
                    raise PlatformError(
                        "idempotency_key_conflict",
                        "The idempotency key was already used with a different request",
                        {},
                        409,
                    )
                return dict(replay["response_json"]), 200
            window = self._repository.get_open_window(connection)
            if window is None:
                raise PlatformError(
                    "calibration_window_not_open",
                    "No calibration window is open",
                    {},
                    409,
                )
            policy = self._repository.get_policy(
                connection, policy_version=window.policy_version or ""
            )
            grace = policy.close_grace_seconds if policy is not None else 3600
            now = self._now_utc(connection)
            deadline = now + timedelta(seconds=grace)
            ok = self._repository.close_window(
                connection,
                window_id=window.window_id or "",
                closed_by=user_id,
                close_deadline_at=deadline,
                now=now,
            )
            if not ok:
                raise PlatformError(
                    "calibration_window_not_open",
                    "No calibration window is open",
                    {},
                    409,
                )
            updated = self._repository.get_window_by_id(
                connection, window_id=window.window_id or ""
            )
            assert updated is not None
            response = updated.to_json()
            self._repository.insert_window_command(
                connection,
                operator_user_id=user_id,
                idempotency_key=idempotency_key,
                action="close",
                request_hash=request_hash,
                target_window_id=window.window_id,
                response_json=response,
                now=now,
            )
            return response, 200

    def read_window(self, actor: Any) -> dict[str, Any]:
        if getattr(actor, "role", None) not in {"ops", "admin"}:
            raise PlatformError(
                "calibration_window_forbidden",
                "Calibration window access requires ops or admin",
                {},
                403,
            )
        with self._engine.connect() as connection:
            window = self._repository.current_window(connection)
            if window is None:
                return {
                    "window_id": None,
                    "status": "closed",
                    "window_kind": None,
                    "policy_version": None,
                    "sample_rate": 0,
                    "pairs_collected": 0,
                    "opened_at": None,
                    "closed_at": None,
                    "close_deadline_at": None,
                    "opened_by": None,
                    "closed_by": None,
                }
            return window.to_json()


__all__ = ["EvaluationService"]
