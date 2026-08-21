"""Shadow-evaluation and calibration-close workers."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from app.chat.ports import ChatPairExpiryPort
from app.platform.errors import PlatformError

from .judge import JudgeProviderPort, JudgeRequest
from .metrics import (
    hit_at_k_candidate,
    hit_at_k_final,
    mrr,
    ndcg_at_k,
)
from .models import JudgeScores, ShadowRunRecord
from .ports import UnavailableAnswerReplayPort
from .repository import SqlAlchemyEvaluationRepository


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _question_hash(text: str) -> str:
    """Same canonical question hash the snapshot/golden publishers use."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _hit_source_ids(hits: tuple[Mapping[str, Any], ...]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for hit in hits:
        value = hit.get("document_id", hit.get("source_id", hit.get("id")))
        if value is None:
            continue
        item = str(value)
        if item not in seen:
            seen.add(item)
            result.append(item)
    return result


_RETRYABLE_JUDGE_CODES = frozenset(
    {"judge_rate_limited", "evaluation_generation_unavailable", "evaluation_judge_unavailable"}
)


class _JudgeFailure(Exception):
    """Internal signal carrying the original judge PlatformError."""

    def __init__(self, error: PlatformError) -> None:
        super().__init__(error.message)
        self.error = error


class _RetryWaitHandled(Exception):
    """Internal signal: the judge failure already moved the run to retry_wait/failed."""


@dataclass(frozen=True, slots=True)
class ShadowEvaluationWorkerStats:
    runs_processed: int = 0
    runs_requeued: int = 0
    runs_failed: int = 0


class ShadowEvaluationWorker:
    """Claims one run, replays samples per candidate config, and judges results.

    Each ``(run, sample, candidate_config)`` uses an independent shadow session
    ``shadow:{run_id}:{item_id}:{candidate_config_version}`` (A10); no online
    message or SSE is emitted. All running-state writes are fence-gated (A11).
    Retryable judge failures move the run to ``retry_wait`` within the frozen
    policy max attempts; exhaustion or non-retryable errors fail it (A12/A18).
    """

    def __init__(
        self,
        engine: Any,
        repository: SqlAlchemyEvaluationRepository,
        judge: JudgeProviderPort,
        retrieval: Any,
        *,
        answer_replay: Any | None = None,
        owner: str = "shadow-evaluation-worker",
        now: Any = None,
        suggestion_callback: Callable[[str], None] | None = None,
    ) -> None:
        self._engine = engine
        self._repository = repository
        self._judge = judge
        self._retrieval = retrieval
        self._answer_replay = answer_replay or UnavailableAnswerReplayPort()
        self._owner = owner
        self._now = now or (lambda: datetime.now(UTC))
        self._suggestion_callback = suggestion_callback

    def _now_utc(self) -> datetime:
        return _utc(self._now())

    def run_once(self) -> ShadowEvaluationWorkerStats:
        requeued = 0
        with self._engine.begin() as connection:
            requeued = self._repository.requeue_retry_wait(connection, now=self._now_utc())
            for run_id in self._repository.list_expired_running(connection, now=self._now_utc()):
                run = self._repository.get_run(connection, run_id=run_id)
                if run is None:
                    continue
                policy = self._repository.get_policy(connection, policy_version=run.policy_version)
                max_attempts = policy.max_attempts if policy is not None else 3
                next_at = self._now_utc() + timedelta(seconds=1)
                self._repository.recover_expired(
                    connection,
                    run_id=run_id,
                    attempt=run.attempt,
                    next_attempt_at=next_at,
                    now=self._now_utc(),
                    max_attempts=max_attempts,
                )
                requeued += 1
            run = self._repository.claim_next(
                connection,
                owner=self._owner,
                lease_ttl_seconds=self._lease_seconds(connection, None),
                now=self._now_utc(),
            )
            if run is None:
                return ShadowEvaluationWorkerStats(runs_requeued=requeued)
        try:
            self._process(run)
            return ShadowEvaluationWorkerStats(runs_processed=1, runs_requeued=requeued)
        except _RetryWaitHandled:
            return ShadowEvaluationWorkerStats(runs_processed=1, runs_requeued=requeued)
        except PlatformError:
            return ShadowEvaluationWorkerStats(
                runs_processed=1, runs_requeued=requeued, runs_failed=1
            )
        except _JudgeFailure as failure:
            # Defensive: _process handles judge failures itself; treat an
            # escaping one as an immediate terminal failure.
            self._handle_judge_failure(
                run,
                failure.error,
                max_attempts=1,
            )
            return ShadowEvaluationWorkerStats(
                runs_processed=1, runs_requeued=requeued, runs_failed=1
            )

    def _lease_seconds(self, connection: Any, run: ShadowRunRecord | None) -> int:
        policy_version = run.policy_version if run is not None else None
        policy = (
            self._repository.get_policy(connection, policy_version=policy_version)
            if policy_version
            else self._repository.latest_policy(connection)
        )
        return policy.lease_seconds if policy is not None else 60

    def _process(self, run: ShadowRunRecord) -> None:
        policy = self._policy_for(run)
        candidate_configs = run.candidate_config_versions or ()
        now = self._now_utc()
        deadline = self._run_deadline(run)
        if deadline is not None and now >= deadline:
            self._finish_failed(run, failure_class="evaluation_run_deadline_exceeded")
            return
        attempt_id = f"{run.run_id}:{run.attempt}"
        actor_user_id = str(run.frozen_snapshot.get("initiator_user_id") or "system:evaluation")
        judge_k = policy.judge_k if policy is not None else 5
        heartbeat_seconds = policy.heartbeat_seconds if policy is not None else 15
        golden = self._golden_by_hash(run)
        last_heartbeat = now
        try:
            with self._engine.begin() as connection:
                items = self._sample_items(connection, run)
                total = len(items) * len(candidate_configs)
                completed = 0
                for item in items:
                    golden_item = golden.get(str(item["question_hash"]))
                    expected_sources = (
                        tuple(golden_item["expected_sources_json"]) if golden_item else ()
                    )
                    expects_refusal = (
                        bool(golden_item["expects_refusal"]) if golden_item is not None else None
                    )
                    for candidate in candidate_configs:
                        if (self._now_utc() - last_heartbeat).total_seconds() >= heartbeat_seconds:
                            ok = self._repository.heartbeat(
                                connection,
                                run_id=run.run_id,
                                attempt=run.attempt,
                                owner=self._owner,
                                fencing_token=run.fencing_token or "",
                                now=self._now_utc(),
                            )
                            if not ok:
                                raise PlatformError(
                                    "evaluation_lease_lost",
                                    "evaluation run fence was lost",
                                    {},
                                    409,
                                )
                            last_heartbeat = self._now_utc()
                        session_id = f"shadow:{run.run_id}:{item['item_id']}:{candidate}"
                        outcome = self._retrieval.replay(
                            question=item["question_text"],
                            principal=self._principal_for(run),
                            space_id=run.space_id,
                            candidate_config_version=candidate,
                            session_id=session_id,
                        )
                        hits = tuple(outcome.get("hits", ()))
                        try:
                            answer = self._answer_replay.replay(
                                question=str(item["question_text"]),
                                source_ref=str(item["source_ref"]),
                                principal=self._principal_for(run),
                                space_id=run.space_id,
                                candidate_config_version=candidate,
                                session_id=session_id,
                            )
                        except PlatformError as error:
                            raise _JudgeFailure(error) from error
                        if not isinstance(answer, str) or not answer.strip():
                            raise _JudgeFailure(
                                PlatformError(
                                    "evaluation_generation_unavailable",
                                    "Answer replay did not return a usable answer",
                                    {"retryable": True},
                                    503,
                                    True,
                                )
                            )
                        judge_request = JudgeRequest(
                            question=str(item["question_text"]),
                            answer=answer.strip(),
                            context=hits,
                            expected_sources=expected_sources,
                            expects_refusal=expects_refusal,
                            run_id=run.run_id,
                            attempt_id=attempt_id,
                            actor_user_id=actor_user_id,
                            deadline_utc=deadline,
                        )
                        try:
                            scores = self._judge.judge(judge_request)
                        except PlatformError as error:
                            raise _JudgeFailure(error) from error
                        metrics = self._metrics(
                            scores,
                            hits=hits,
                            candidate_hits=tuple(outcome.get("candidate_hits", ())),
                            expected_sources=expected_sources,
                            expects_refusal=expects_refusal,
                            judge_k=judge_k,
                        )
                        self._repository.insert_result(
                            connection,
                            run_id=run.run_id,
                            sample_item_id=str(item["item_id"]),
                            candidate_config_version=candidate,
                            session_id=session_id,
                            metrics_json=metrics,
                            weak_signals_json=dict(item.get("weak_signals", {})),
                            judged_at=self._now_utc(),
                        )
                        completed += 1
                self._finish_succeeded(connection, run, total=total, completed=completed)
        except _JudgeFailure as failure:
            self._handle_judge_failure(
                run,
                failure.error,
                max_attempts=policy.max_attempts if policy is not None else 3,
            )
            raise _RetryWaitHandled() from None
        if self._suggestion_callback is not None:
            self._suggestion_callback(run.run_id)

    def _policy_for(self, run: ShadowRunRecord) -> Any:
        with self._engine.connect() as connection:
            policy = self._repository.get_policy(connection, policy_version=run.policy_version)
            return policy

    def _run_deadline(self, run: ShadowRunRecord) -> datetime | None:
        limits = run.frozen_snapshot.get("limits", {})
        deadline_seconds = limits.get("deadline_seconds")
        if not isinstance(deadline_seconds, int) or deadline_seconds <= 0:
            return None
        return _utc(run.created_at) + timedelta(seconds=deadline_seconds)

    def _golden_by_hash(self, run: ShadowRunRecord) -> dict[str, Mapping[str, Any]]:
        golden_version = run.frozen_snapshot.get("golden_set_version")
        if not golden_version:
            return {}
        with self._engine.connect() as connection:
            items = self._repository.list_golden_items(
                connection,
                space_id=run.space_id,
                golden_version=str(golden_version),
            )
        return {str(item["question_hash"]): item for item in items}

    def _handle_judge_failure(
        self,
        run: ShadowRunRecord,
        error: PlatformError,
        *,
        max_attempts: int,
    ) -> None:
        failure_class = (
            error.code
            if error.code
            in {
                "judge_rate_limited",
                "evaluation_judge_unavailable",
                "evaluation_judge_deadline_exceeded",
                "evaluation_generation_unavailable",
            }
            else "shadow_judge_failed"
        )
        with self._engine.begin() as connection:
            if error.code in _RETRYABLE_JUDGE_CODES and run.attempt < max_attempts:
                delay = min(2 ** (run.attempt - 1) * 60, 600)
                self._repository.transition_retry_wait(
                    connection,
                    run_id=run.run_id,
                    attempt=run.attempt,
                    owner=self._owner,
                    fencing_token=run.fencing_token or "",
                    next_attempt_at=self._now_utc() + timedelta(seconds=delay),
                    now=self._now_utc(),
                    failure_class=failure_class,
                    increment_attempt=True,
                )
                return
            self._repository.transition_terminal(
                connection,
                run_id=run.run_id,
                attempt=run.attempt,
                owner=self._owner,
                fencing_token=run.fencing_token or "",
                to_state="failed",
                now=self._now_utc(),
                failure_class=failure_class,
            )

    def _sample_items(self, connection: Any, run: ShadowRunRecord) -> list[Mapping[str, Any]]:
        from sqlalchemy import select

        from .schema import evaluation_sample_snapshot_item_table

        rows = (
            connection.execute(
                select(evaluation_sample_snapshot_item_table)
                .where(
                    evaluation_sample_snapshot_item_table.c.snapshot_id
                    == run.frozen_snapshot.get("snapshot_id", "")
                )
                .order_by(evaluation_sample_snapshot_item_table.c.position)
            )
            .mappings()
            .all()
        )
        return [dict(row) for row in rows]

    @staticmethod
    def _principal_for(run: ShadowRunRecord) -> Any:
        class _Principal:
            user_id = run.space_id
            role = "ops"
            auth_session_id = "shadow"
            username = "shadow"
            department_id = None

        return _Principal()

    @staticmethod
    def _metrics(
        scores: JudgeScores,
        *,
        hits: tuple[Mapping[str, Any], ...],
        candidate_hits: tuple[Mapping[str, Any], ...],
        expected_sources: tuple[str, ...],
        expects_refusal: bool | None,
        judge_k: int,
    ) -> dict[str, float]:
        final_ids = _hit_source_ids(hits)
        candidate_ids = _hit_source_ids(candidate_hits) if candidate_hits else final_ids
        if expected_sources:
            hit_candidate = (
                1.0 if hit_at_k_candidate(candidate_ids, expected_sources, judge_k) else 0.0
            )
            hit_final = 1.0 if hit_at_k_final(final_ids, expected_sources, judge_k) else 0.0
            mrr_value = mrr(final_ids, expected_sources)
            ndcg = (
                ndcg_at_k(final_ids, expected_sources, judge_k)
                if len(expected_sources) > 1
                else 0.0
            )
        else:
            # No golden labels: retrieval metrics stay uncomputed; weak signals
            # never masquerade as hit/MRR (A14).
            hit_candidate = 0.0
            hit_final = 0.0
            mrr_value = 0.0
            ndcg = 0.0
        metrics = {
            "faithfulness": scores.faithfulness or 0.0,
            "answer_relevancy": scores.answer_relevancy or 0.0,
            "hit_at_k_candidate": hit_candidate,
            "hit_at_k_final": hit_final,
            "mrr": mrr_value,
            "ndcg_at_k": ndcg,
            "p95_latency_ms": float(scores.latency_ms or 0),
            "cost_per_query": 0.0,
        }
        if expects_refusal is not None:
            metrics["refusal_rate"] = 1.0 if scores.is_refusal == expects_refusal else 0.0
        return metrics

    def _finish_succeeded(
        self,
        connection: Any,
        run: ShadowRunRecord,
        *,
        total: int,
        completed: int,
    ) -> None:
        progress = {"total": total, "completed": completed, "failed": 0}
        ok = self._repository.transition_terminal(
            connection,
            run_id=run.run_id,
            attempt=run.attempt,
            owner=self._owner,
            fencing_token=run.fencing_token or "",
            to_state="succeeded",
            now=self._now_utc(),
            progress=progress,
            report_ref=f"eval_report:{run.run_id}",
        )
        if not ok:
            raise PlatformError("evaluation_lease_lost", "evaluation run fence was lost", {}, 409)

    def _finish_failed(self, run: ShadowRunRecord, *, failure_class: str) -> None:
        with self._engine.begin() as connection:
            self._repository.transition_terminal(
                connection,
                run_id=run.run_id,
                attempt=run.attempt,
                owner=self._owner,
                fencing_token=run.fencing_token or "",
                to_state="failed",
                now=self._now_utc(),
                failure_class=failure_class,
            )


class CalibrationCloseWorker:
    """Closes ``closing`` calibration windows (A31).

    Immediate close: no votable ``open`` pair and no ``pending`` pair that can
    still turn open. Deadline close: request pair expiry through the chat-owned
    ``ChatPairExpiryPort`` (the evaluation domain never writes ``chat_ab_pair``
    directly, A2), then transition the window to ``closed`` (A31).
    """

    def __init__(
        self,
        engine: Any,
        repository: SqlAlchemyEvaluationRepository,
        pair_expiry: ChatPairExpiryPort | None = None,
        *,
        now: Any = None,
    ) -> None:
        self._engine = engine
        self._repository = repository
        self._pair_expiry = pair_expiry
        self._now = now or (lambda: datetime.now(UTC))

    def _now_utc(self) -> datetime:
        return _utc(self._now())

    def _window_has_open_or_pending_pairs(
        self,
        connection: Any,
        *,
        window_id: str,
    ) -> bool:
        if self._pair_expiry is None:
            raise PlatformError(
                "evaluation_pair_expiry_unavailable",
                "The chat pair expiry port is not configured",
                {"retryable": True},
                503,
                True,
            )
        return self._pair_expiry.window_has_votable_pairs(connection, window_id=window_id)

    def _expire_window_pairs(self, connection: Any, *, window_id: str, now: datetime) -> None:
        if self._pair_expiry is None:
            raise PlatformError(
                "evaluation_pair_expiry_unavailable",
                "The chat pair expiry port is not configured",
                {"retryable": True},
                503,
                True,
            )
        self._pair_expiry.expire_window_pairs(connection, window_id=window_id, now=now)

    def run_once(self) -> int:
        closed = 0
        with self._engine.begin() as connection:
            window = self._repository.get_closing_window(connection)
            if window is None:
                return 0
            now = self._now_utc()
            window_id = window.window_id or ""
            deadline = window.close_deadline_at
            if deadline is not None and deadline <= now:
                self._expire_window_pairs(connection, window_id=window_id, now=now)
                if self._repository.finalize_window(connection, window_id=window_id, now=now):
                    closed += 1
            elif not self._window_has_open_or_pending_pairs(connection, window_id=window_id):
                if self._repository.finalize_window(connection, window_id=window_id, now=now):
                    closed += 1
        return closed


__all__ = [
    "CalibrationCloseWorker",
    "ShadowEvaluationWorker",
    "ShadowEvaluationWorkerStats",
    "_question_hash",
]
