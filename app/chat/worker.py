"""Durable execution worker: claim, run, fence, checkpoint and converge terminals."""

from __future__ import annotations

import threading
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.engine import Connection, Engine

from app.platform.errors import PlatformError
from app.usage.ledger import OwnershipSnapshot, ProviderMeasurement
from app.usage.ports import UsageSubmissionPort

from .budget import GenerationBudget
from .events import append_event, has_terminal_event
from .leases import generation_has_active_lease
from .models import RetrievalHitOutcome
from .ports import (
    CalibrationWindowPort,
    ChatProviderPort,
    ChatProviderRequest,
    ChatRetrievalPort,
    consume_durable_revocation_commands,
)
from .schema import (
    chat_ab_candidate_table,
    chat_ab_pair_table,
    chat_generation_execution_table,
    chat_generation_table,
    chat_message_table,
)

EXECUTION_LEASE_SECONDS = 90
HEARTBEAT_SECONDS = 30
MAX_PHYSICAL_EXECUTIONS = 3
# Renewal cadence for the background execution heartbeat; well below the lease TTL.


def _utc(value: Any) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


@dataclass(slots=True)
class WorkerOutcome:
    executed: str | None
    stage: str
    details: dict[str, Any]


class ChatGenerationWorker:
    """Claims queued executions and drives retrieval/provider to a terminal."""

    def __init__(
        self,
        engine: Engine,
        *,
        clock: Any,
        retrieval: ChatRetrievalPort,
        provider: ChatProviderPort,
        usage: UsageSubmissionPort,
        calibration: CalibrationWindowPort,
        lease_seconds: int = EXECUTION_LEASE_SECONDS,
        max_physical_executions: int = MAX_PHYSICAL_EXECUTIONS,
    ) -> None:
        self._engine = engine
        self._clock = clock
        self._retrieval = retrieval
        self._provider = provider
        self._usage = usage
        self._calibration = calibration
        self._lease_seconds = lease_seconds
        self._max_physical_executions = max_physical_executions

    def _now(self, connection: Connection | None = None) -> datetime:
        value = self._clock.now_utc(connection)
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value

    # ------------------------------------------------------------- maintenance

    def run_maintenance(self) -> dict[str, Any]:
        """Reap disconnect grace, recover expired leases, expire A/B pairs."""

        with self._engine.begin() as connection:
            now = self._now(connection)
            consumed = consume_durable_revocation_commands(connection, now=now)
            self._reap_disconnect_grace(connection, now=now)
            recovered = self._recover_expired_executions(connection, now=now)
            expired_pairs = self._expire_past_deadline_pairs(connection, now=now)
        return {
            "revocation_commands_consumed": consumed,
            "executions_recovered": recovered,
            "ab_pairs_expired": expired_pairs,
        }

    def _reap_disconnect_grace(self, connection: Connection, *, now: datetime) -> int:
        rows = (
            connection.execute(
                select(
                    chat_generation_table.c.id,
                    chat_generation_table.c.disconnect_deadline_at_utc,
                ).where(
                    chat_generation_table.c.status.in_(["running", "stop_requested"]),
                    chat_generation_table.c.disconnect_deadline_at_utc.is_not(None),
                )
            )
            .mappings()
            .all()
        )
        stopped = 0
        for row in rows:
            if _utc(row["disconnect_deadline_at_utc"]) > now:
                continue
            generation_id = str(row["id"])
            if generation_has_active_lease(connection, generation_id=generation_id):
                connection.execute(
                    update(chat_generation_table)
                    .where(chat_generation_table.c.id == generation_id)
                    .values(disconnect_deadline_at_utc=None, updated_at_utc=now)
                )
                continue
            updated = connection.execute(
                update(chat_generation_table)
                .where(
                    chat_generation_table.c.id == generation_id,
                    chat_generation_table.c.status.in_(["running", "stop_requested"]),
                )
                .values(
                    status="stopped",
                    stop_reason="client_disconnected",
                    updated_at_utc=now,
                )
            ).rowcount
            if updated:
                self._discard_unfinished_ab_pair(connection, generation_id=generation_id, now=now)
                self._append_terminal(
                    connection,
                    generation_id=generation_id,
                    event_type="stopped",
                    data={
                        "generation_id": generation_id,
                        "message_id": self._message_id(connection, generation_id),
                        "status": "stopped",
                        "stop_reason": "client_disconnected",
                    },
                    now=now,
                )
                connection.execute(
                    update(chat_message_table)
                    .where(chat_message_table.c.generation_id == generation_id)
                    .values(
                        status="stopped",
                        stop_reason="client_disconnected",
                        updated_at_utc=now,
                    )
                )
                stopped += 1
        return stopped

    def _recover_expired_executions(self, connection: Connection, *, now: datetime) -> int:
        rows = (
            connection.execute(
                select(
                    chat_generation_execution_table.c.execution_id,
                    chat_generation_execution_table.c.generation_id,
                    chat_generation_execution_table.c.execution_attempt_number,
                    chat_generation_execution_table.c.fencing_token,
                    chat_generation_execution_table.c.lease_expires_at_utc,
                ).where(
                    chat_generation_execution_table.c.status == "running",
                )
            )
            .mappings()
            .all()
        )
        recovered = 0
        for row in rows:
            if row["lease_expires_at_utc"] is not None and _utc(row["lease_expires_at_utc"]) >= now:
                continue
            generation_id = str(row["generation_id"])
            claimed = connection.execute(
                update(chat_generation_execution_table)
                .where(
                    chat_generation_execution_table.c.execution_id == row["execution_id"],
                    chat_generation_execution_table.c.status == "running",
                )
                .values(
                    status="expired",
                    fencing_token=int(row["fencing_token"]) + 1,
                    lease_owner=None,
                    lease_expires_at_utc=None,
                    updated_at_utc=now,
                )
            ).rowcount
            if claimed != 1:
                continue
            generation = (
                connection.execute(
                    select(
                        chat_generation_table.c.status,
                        chat_generation_table.c.absolute_deadline_at_utc,
                    ).where(chat_generation_table.c.id == generation_id)
                )
                .mappings()
                .one_or_none()
            )
            if generation is None:
                continue
            if (
                str(generation["status"]) not in {"running", "stop_requested"}
                or _utc(generation["absolute_deadline_at_utc"]) <= now
            ):
                self._terminalize_unrecoverable(connection, generation_id=generation_id, now=now)
                continue
            physical_count = (
                connection.execute(
                    select(chat_generation_execution_table.c.execution_id).where(
                        chat_generation_execution_table.c.generation_id == generation_id
                    )
                )
                .mappings()
                .all()
            )
            if len(physical_count) >= self._max_physical_executions:
                self._terminalize_unrecoverable(connection, generation_id=generation_id, now=now)
                continue
            connection.execute(
                chat_generation_execution_table.insert().values(
                    execution_id=f"exec_{uuid.uuid4().hex}",
                    generation_id=generation_id,
                    execution_attempt_number=int(row["execution_attempt_number"]) + 1,
                    status="queued",
                    lease_owner=None,
                    lease_expires_at_utc=None,
                    heartbeat_at_utc=None,
                    fencing_token=int(row["fencing_token"]) + 1,
                    checkpoint_version=0,
                    checkpoint_json=None,
                    next_attempt_at_utc=now,
                    last_error_classification=None,
                    provider_reconciliation_state=None,
                    created_at_utc=now,
                    updated_at_utc=now,
                )
            )
            recovered += 1
        return recovered

    def _terminalize_unrecoverable(
        self, connection: Connection, *, generation_id: str, now: datetime
    ) -> None:
        execution = connection.execute(
            select(chat_generation_execution_table.c.execution_id).where(
                chat_generation_execution_table.c.generation_id == generation_id,
                chat_generation_execution_table.c.status == "running",
            )
        ).scalar_one_or_none()
        generation = (
            connection.execute(
                select(
                    chat_generation_table.c.message_id,
                    chat_generation_table.c.status,
                ).where(chat_generation_table.c.id == generation_id)
            )
            .mappings()
            .one()
        )
        if str(generation["status"]) in {"running", "stop_requested"}:
            connection.execute(
                update(chat_generation_table)
                .where(chat_generation_table.c.id == generation_id)
                .values(
                    status="failed",
                    last_error_code="execution_recovery_exhausted",
                    updated_at_utc=now,
                )
            )
            connection.execute(
                update(chat_message_table)
                .where(chat_message_table.c.id == generation["message_id"])
                .values(status="failed", updated_at_utc=now)
            )
            self._discard_unfinished_ab_pair(connection, generation_id=generation_id, now=now)
            self._append_terminal(
                connection,
                generation_id=generation_id,
                event_type="error",
                data={
                    "code": "execution_recovery_exhausted",
                    "message": "The generation could not be recovered before its deadline",
                    "details": {},
                    "request_id": "req_system",
                },
                now=now,
            )
        if execution is not None:
            connection.execute(
                update(chat_generation_execution_table)
                .where(
                    chat_generation_execution_table.c.execution_id == execution,
                    chat_generation_execution_table.c.status == "running",
                )
                .values(status="failed", updated_at_utc=now)
            )

    def _expire_past_deadline_pairs(self, connection: Connection, *, now: datetime) -> int:
        rows = (
            connection.execute(
                select(
                    chat_ab_pair_table.c.pair_id,
                    chat_ab_pair_table.c.expires_at_utc,
                    chat_ab_pair_table.c.close_deadline_at_utc,
                ).where(
                    chat_ab_pair_table.c.status == "open",
                )
            )
            .mappings()
            .all()
        )
        expired = 0
        for row in rows:
            deadlines = [
                _utc(value)
                for value in (row["expires_at_utc"], row["close_deadline_at_utc"])
                if value is not None
            ]
            if not deadlines or min(deadlines) > now:
                continue
            expired += connection.execute(
                update(chat_ab_pair_table)
                .where(
                    chat_ab_pair_table.c.pair_id == row["pair_id"],
                    chat_ab_pair_table.c.status == "open",
                )
                .values(status="expired", updated_at_utc=now)
            ).rowcount
        return expired

    # ----------------------------------------------------------------- execute

    def run_once(self) -> WorkerOutcome:
        """Claim and execute one due generation synchronously."""

        claimed = self._claim_execution()
        if claimed is None:
            return WorkerOutcome(executed=None, stage="idle", details={})
        execution_id, generation_id = claimed
        try:
            self._execute(execution_id=execution_id, generation_id=generation_id)
            return WorkerOutcome(executed=generation_id, stage="executed", details={})
        except PlatformError as error:
            self._fail_execution(
                execution_id=execution_id,
                generation_id=generation_id,
                error=error,
            )
            return WorkerOutcome(
                executed=generation_id,
                stage="failed",
                details={"code": error.code},
            )

    def _claim_execution(self) -> tuple[str, str] | None:
        with self._engine.begin() as connection:
            now = self._now(connection)
            running_generations = select(chat_generation_execution_table.c.generation_id).where(
                chat_generation_execution_table.c.status == "running"
            )
            due = (
                connection.execute(
                    select(
                        chat_generation_execution_table.c.execution_id,
                        chat_generation_execution_table.c.generation_id,
                        chat_generation_execution_table.c.next_attempt_at_utc,
                    )
                    .where(
                        chat_generation_execution_table.c.status.in_(["queued", "retry_wait"]),
                        chat_generation_execution_table.c.generation_id.not_in(running_generations),
                    )
                    .order_by(chat_generation_execution_table.c.next_attempt_at_utc)
                    .limit(1)
                )
                .mappings()
                .first()
            )
            if due is None:
                return None
            if _utc(due["next_attempt_at_utc"]) > now:
                return None
            execution_id = str(due["execution_id"])
            generation_id = str(due["generation_id"])
            execution = self._lock_execution(
                connection,
                generation_id=generation_id,
                execution_id=execution_id,
            )
            if execution is None or str(execution["status"]) not in {"queued", "retry_wait"}:
                return None
            generation = self._lock_generation(connection, generation_id=generation_id)
            if generation is None or str(generation["status"]) not in {"running", "stop_requested"}:
                connection.execute(
                    update(chat_generation_execution_table)
                    .where(chat_generation_execution_table.c.execution_id == execution_id)
                    .values(status="cancelled", updated_at_utc=now)
                )
                return None
            connection.execute(
                update(chat_generation_execution_table)
                .where(chat_generation_execution_table.c.execution_id == execution_id)
                .values(
                    status="running",
                    lease_owner=f"worker_{uuid.uuid4().hex[:12]}",
                    lease_expires_at_utc=now + timedelta(seconds=self._lease_seconds),
                    heartbeat_at_utc=now,
                    fencing_token=chat_generation_execution_table.c.fencing_token + 1,
                    updated_at_utc=now,
                )
            )
            return execution_id, generation_id

    def _execute(self, *, execution_id: str, generation_id: str) -> None:
        generation = self._read_generation(generation_id)
        if str(generation["status"]) == "stop_requested":
            self._stop_terminal(
                execution_id=execution_id,
                generation_id=generation_id,
                stop_reason=str(generation["stop_reason"] or "manual_request"),
            )
            return
        control_version = int(generation["control_version"])
        fencing_token = self._execution_fence(generation_id, execution_id)
        stop_heartbeat = threading.Event()
        heartbeat = threading.Thread(
            target=self._heartbeat_loop,
            kwargs={
                "execution_id": execution_id,
                "fencing_token": fencing_token,
                "stop": stop_heartbeat,
            },
            daemon=True,
            name=f"chat-execution-heartbeat-{execution_id}",
        )
        heartbeat.start()
        try:
            self._execute_claimed(
                execution_id=execution_id,
                generation_id=generation_id,
                generation=generation,
                control_version=control_version,
                fencing_token=fencing_token,
            )
        finally:
            stop_heartbeat.set()
            heartbeat.join(timeout=HEARTBEAT_SECONDS)

    def _heartbeat_loop(
        self, *, execution_id: str, fencing_token: int, stop: threading.Event
    ) -> None:
        """Extend the execution lease while the fenced execution still owns it.

        Provider calls can run far longer than the lease TTL; without renewal,
        maintenance would expire the running execution and re-enqueue a retry
        that duplicates the provider call.
        """

        while not stop.wait(HEARTBEAT_SECONDS):
            try:
                with self._engine.begin() as connection:
                    now = self._now(connection)
                    renewed = connection.execute(
                        update(chat_generation_execution_table)
                        .where(
                            chat_generation_execution_table.c.execution_id == execution_id,
                            chat_generation_execution_table.c.status == "running",
                            chat_generation_execution_table.c.fencing_token == fencing_token,
                        )
                        .values(
                            lease_expires_at_utc=now + timedelta(seconds=self._lease_seconds),
                            heartbeat_at_utc=now,
                        )
                    ).rowcount
                if not renewed:
                    return
            except Exception:
                # Best effort: a transient DB error must not kill the execution;
                # the next tick retries, and a persistent failure lets the lease
                # expire so recovery can take over.
                continue

    def _execute_claimed(
        self,
        *,
        execution_id: str,
        generation_id: str,
        generation: Mapping[str, Any],
        control_version: int,
        fencing_token: int,
    ) -> None:
        profile_id = str(generation["retrieval_profile_id"])
        profile_version = str(generation["retrieval_profile_version"])
        effort = str(generation["effective_effort_level"])
        budget = GenerationBudget(effort_level=effort)
        hits: tuple[RetrievalHitOutcome, ...] = ()

        round_index = 0
        while True:
            if round_index > 0 and not budget.can_start_rag_round():
                previous_effort = budget.effort_level
                upgraded = budget.upgrade_effort()
                if upgraded is None:
                    break
                if not self._persist_effort_upgrade(
                    generation_id=generation_id,
                    execution_id=execution_id,
                    fencing_token=fencing_token,
                    control_version=control_version,
                    previous_effort=previous_effort,
                    upgraded_effort=upgraded,
                ):
                    return
                generation = {
                    **generation,
                    "effective_effort_level": upgraded,
                    "upgraded_from": previous_effort,
                }
                self._emit_notice(
                    generation_id=generation_id,
                    execution_id=execution_id,
                    fencing_token=fencing_token,
                    control_version=control_version,
                    kind="effort_upgraded",
                    detail={"effort_level": upgraded},
                    generation=generation,
                )
            stage = "retrieving" if round_index == 0 else "retrieving_again"
            self._emit_stage(
                generation_id=generation_id,
                execution_id=execution_id,
                fencing_token=fencing_token,
                control_version=control_version,
                phase=stage,
                generation=generation,
            )
            outcome = self._retrieval.search(
                str(generation["request_content"]),
                principal=_principal_from_generation(generation),
                narrowing_scope=generation["request_scope_json"],
                profile_id=profile_id,
                profile_version=profile_version,
                effort=budget.effort_level,
            )
            hits = outcome.hits
            budget.record_rag_round()
            for item in outcome.degradations:
                self._emit_notice(
                    generation_id=generation_id,
                    execution_id=execution_id,
                    fencing_token=fencing_token,
                    control_version=control_version,
                    kind="retrieval_degraded",
                    detail=dict(item),
                    generation=generation,
                )
            citations = self._resolve_citations(hits, generation)
            if len(citations) == len(hits):
                break
            visible_ids = {(str(item["document_id"]), str(item["chunk_id"])) for item in citations}
            hits = tuple(hit for hit in hits if (hit.document_id, hit.chunk_id) in visible_ids)
            if not hits:
                raise PlatformError(
                    "source_scope_changed",
                    "All retrieval sources left the online path during generation",
                    {},
                    500,
                )
            round_index += 1

        candidates = self._produce_candidates(
            generation=generation,
            execution_id=execution_id,
            fencing_token=fencing_token,
            control_version=control_version,
            hits=hits,
            citations=citations,
        )
        self._publish(
            generation=generation,
            execution_id=execution_id,
            fencing_token=fencing_token,
            control_version=control_version,
            candidates=candidates,
        )

    def _persist_effort_upgrade(
        self,
        *,
        generation_id: str,
        execution_id: str,
        fencing_token: int,
        control_version: int,
        previous_effort: str,
        upgraded_effort: str,
    ) -> bool:
        with self._engine.begin() as connection:
            if not self._fence_current(
                connection,
                generation_id=generation_id,
                execution_id=execution_id,
                fencing_token=fencing_token,
                control_version=control_version,
            ):
                self._cancel_stale_execution(
                    connection,
                    execution_id=execution_id,
                    generation_id=generation_id,
                    now=self._now(connection),
                )
                return False
            current_execution = (
                select(chat_generation_execution_table.c.execution_id)
                .where(
                    chat_generation_execution_table.c.execution_id == execution_id,
                    chat_generation_execution_table.c.generation_id == generation_id,
                    chat_generation_execution_table.c.fencing_token == fencing_token,
                    chat_generation_execution_table.c.status == "running",
                )
                .exists()
            )
            updated = connection.execute(
                update(chat_generation_table)
                .where(
                    chat_generation_table.c.id == generation_id,
                    chat_generation_table.c.control_version == control_version,
                    chat_generation_table.c.status == "running",
                    current_execution,
                )
                .values(
                    effective_effort_level=upgraded_effort,
                    upgraded_from=previous_effort,
                    updated_at_utc=self._now(connection),
                )
            ).rowcount
            if updated:
                return True
            self._cancel_stale_execution(
                connection,
                execution_id=execution_id,
                generation_id=generation_id,
                now=self._now(connection),
            )
            return False

    def _resolve_citations(
        self,
        hits: tuple[RetrievalHitOutcome, ...],
        generation: Mapping[str, Any],
    ) -> list[Mapping[str, Any]]:
        """Resolve final, re-authorized citations for the given hits."""
        if not hits:
            return []
        return list(
            self._retrieval.resolve_citations(
                tuple(_hit_mapping(hit) for hit in hits),
                principal=_principal_from_generation(generation),
            )
        )

    def _produce_candidates(
        self,
        *,
        generation: Mapping[str, Any],
        execution_id: str,
        fencing_token: int,
        control_version: int,
        hits: tuple[RetrievalHitOutcome, ...],
        citations: list[Mapping[str, Any]],
    ) -> list[dict[str, Any]]:
        context = tuple(
            {
                "document_id": hit.document_id,
                "document_version_id": hit.document_version_id,
                "publication_id": hit.publication_id,
                "chunk_id": hit.chunk_id,
                "locator": dict(hit.locator),
                "snippet": hit.snippet,
            }
            for hit in hits
        )
        pair = self._pair_for_generation(generation_id=str(generation["id"]))
        candidate_numbers = (0, 1) if pair is not None else (0,)
        results: list[dict[str, Any]] = []
        for candidate in candidate_numbers:
            self._emit_stage(
                generation_id=str(generation["id"]),
                execution_id=execution_id,
                fencing_token=fencing_token,
                control_version=control_version,
                phase="generating",
                generation=generation,
            )
            if _utc(self._now()) >= _utc(generation["absolute_deadline_at_utc"]):
                raise PlatformError(
                    "generation_deadline_exceeded",
                    "The generation deadline expired before the provider call",
                    {},
                    500,
                )
            request = ChatProviderRequest(
                generation_id=str(generation["id"]),
                owner_user_id=str(generation["owner_user_id"]),
                content=str(generation["request_content"]),
                effort_level=str(generation["effective_effort_level"]),
                candidate=None if pair is None else candidate,
                context_items=context,
            )
            response = self._provider_call(
                request,
                generation=generation,
                execution_id=execution_id,
                fencing_token=fencing_token,
            )
            answer_mode = _answer_mode(hits, citations)
            results.append(
                {
                    "candidate": candidate,
                    "content": response.content,
                    "citations": citations,
                    "answer_mode": answer_mode,
                }
            )
        return results

    def _provider_call(
        self,
        request: ChatProviderRequest,
        *,
        generation: Mapping[str, Any],
        execution_id: str,
        fencing_token: int,
    ) -> Any:
        call_id = self._usage.prepare_provider_call(
            provider="chat",
            model="chat-model",
            operation="chat_generation",
            execution_kind="chat_generation",
            execution_id=execution_id,
            generation_id=str(generation["id"]),
            deadline_utc=generation["absolute_deadline_at_utc"],
            request_fingerprint=f"chat:{generation['id']}:{execution_id}",
        )
        started_at = self._now()
        if not self._usage.mark_dispatching(call_id, started_at_provider=started_at):
            raise PlatformError(
                "provider_dispatch_failed",
                "The chat provider call could not be dispatched",
                {},
                503,
            )
        ownership = OwnershipSnapshot(
            actor_user_id=str(generation["owner_user_id"]),
            actor_role_snapshot="user",
            actor_department_id_snapshot=None,
            quota_subject_user_id=None,
            cost_center_key="public",
            fence_token=fencing_token,
        )
        try:
            response = self._provider.generate(request)
        except PlatformError as error:
            self._usage.complete_provider_call(
                provider_call_id=call_id,
                measurement=ProviderMeasurement(
                    input_tokens=None,
                    prompt_cache_hit_tokens=None,
                    prompt_cache_miss_tokens=None,
                    output_tokens=None,
                    reasoning_tokens=None,
                    image_count=None,
                    visual_input_tokens=None,
                    embedding_input_tokens=None,
                    vector_count=None,
                    measurement_sources={},
                ),
                ownership=ownership,
                result="failed",
            )
            raise error
        except Exception:
            # 已派发但传输中断（连接断开/超时等）：结果未知而非确定失败，
            # 按 §6.5 记 unknown 并以 provider_result_unknown 终态化（SSE error 事件）。
            self._usage.mark_unknown(call_id)
            raise PlatformError(
                "provider_result_unknown",
                "The chat provider result is unknown after dispatch",
                {},
                504,
            ) from None
        measurement = ProviderMeasurement(
            input_tokens=getattr(response, "input_tokens", None),
            prompt_cache_hit_tokens=None,
            prompt_cache_miss_tokens=None,
            output_tokens=getattr(response, "output_tokens", None),
            reasoning_tokens=getattr(response, "reasoning_tokens", None),
            image_count=None,
            visual_input_tokens=None,
            embedding_input_tokens=None,
            vector_count=None,
            measurement_sources={},
        )
        self._usage.complete_provider_call(
            provider_call_id=call_id,
            measurement=measurement,
            ownership=ownership,
            result="succeeded",
        )
        return response

    # ------------------------------------------------------------- publication

    def _publish(
        self,
        *,
        generation: Mapping[str, Any],
        execution_id: str,
        fencing_token: int,
        control_version: int,
        candidates: list[dict[str, Any]],
    ) -> None:
        with self._engine.begin() as connection:
            now = self._now(connection)
            if not self._fence_current(
                connection,
                generation_id=str(generation["id"]),
                execution_id=execution_id,
                fencing_token=fencing_token,
                control_version=control_version,
            ):
                self._cancel_stale_execution(
                    connection,
                    execution_id=execution_id,
                    generation_id=str(generation["id"]),
                    now=now,
                )
                return
            current_execution = connection.execute(
                update(chat_generation_execution_table)
                .where(
                    chat_generation_execution_table.c.execution_id == execution_id,
                    chat_generation_execution_table.c.generation_id == str(generation["id"]),
                    chat_generation_execution_table.c.fencing_token == fencing_token,
                    chat_generation_execution_table.c.status == "running",
                )
                .values(heartbeat_at_utc=now, updated_at_utc=now)
            ).rowcount
            if not current_execution:
                self._cancel_stale_execution(
                    connection,
                    execution_id=execution_id,
                    generation_id=str(generation["id"]),
                    now=now,
                )
                return
            current_generation = connection.execute(
                update(chat_generation_table)
                .where(
                    chat_generation_table.c.id == str(generation["id"]),
                    chat_generation_table.c.control_version == control_version,
                    chat_generation_table.c.status == "running",
                )
                .values(updated_at_utc=now)
            ).rowcount
            if not current_generation:
                self._cancel_stale_execution(
                    connection,
                    execution_id=execution_id,
                    generation_id=str(generation["id"]),
                    now=now,
                )
                return
            pair = self._pair_row(connection, generation_id=str(generation["id"]))
            ab_open = pair is not None and len(candidates) == 2
            near_duplicate = bool(
                ab_open and _rouge_l(candidates[0]["content"], candidates[1]["content"]) >= 0.92
            )
            if near_duplicate:
                assert pair is not None
                connection.execute(
                    update(chat_ab_candidate_table)
                    .where(
                        chat_ab_candidate_table.c.pair_id == pair["pair_id"],
                        chat_ab_candidate_table.c.candidate == 1,
                    )
                    .values(status="discarded")
                )
                connection.execute(
                    update(chat_ab_pair_table)
                    .where(chat_ab_pair_table.c.pair_id == pair["pair_id"])
                    .values(status="expired", updated_at_utc=now)
                )
                ab_open = False
            for item in candidates if ab_open else candidates[:1]:
                self._append_terminal(
                    connection,
                    generation_id=str(generation["id"]),
                    event_type="answer",
                    data={
                        "candidate": item["candidate"],
                        "content": item["content"],
                        "citations": item["citations"],
                        "answer_mode": item["answer_mode"],
                        "effort_level": str(generation["effective_effort_level"]),
                        "upgraded_from": generation["upgraded_from"],
                    },
                    now=now,
                )
                if pair is not None:
                    connection.execute(
                        update(chat_ab_candidate_table)
                        .where(
                            chat_ab_candidate_table.c.pair_id == pair["pair_id"],
                            chat_ab_candidate_table.c.candidate == item["candidate"],
                        )
                        .values(
                            status="published",
                            content=item["content"],
                            citations_json=list(item["citations"]),
                            answer_mode=item["answer_mode"],
                        )
                    )
            message_values: dict[str, Any] = {
                "status": "completed",
                "stop_reason": None,
                "updated_at_utc": now,
            }
            if pair is not None:
                if ab_open:
                    # The pair's expires_at_utc was frozen at creation as the
                    # earlier of the policy TTL and the window deadline (A31);
                    # opening for voting must not overwrite it.
                    connection.execute(
                        update(chat_ab_pair_table)
                        .where(chat_ab_pair_table.c.pair_id == pair["pair_id"])
                        .values(
                            status="open",
                            updated_at_utc=now,
                        )
                    )
                    # A/B open: assistant body stays empty until a vote selects one candidate.
                    message_values["content"] = ""
                    message_values["answer_mode"] = None
                    message_values["citations_json"] = None
                else:
                    connection.execute(
                        update(chat_ab_pair_table)
                        .where(chat_ab_pair_table.c.pair_id == pair["pair_id"])
                        .values(status="expired", updated_at_utc=now)
                    )
                    visible = candidates[0]
                    message_values["content"] = visible["content"]
                    message_values["answer_mode"] = visible["answer_mode"]
                    message_values["citations_json"] = list(visible["citations"])
            else:
                visible = candidates[0]
                message_values["content"] = visible["content"]
                message_values["answer_mode"] = visible["answer_mode"]
                message_values["citations_json"] = list(visible["citations"])
            connection.execute(
                update(chat_message_table)
                .where(chat_message_table.c.id == generation["message_id"])
                .values(**message_values)
            )
            connection.execute(
                update(chat_generation_table)
                .where(chat_generation_table.c.id == str(generation["id"]))
                .values(
                    status="completed",
                    stop_reason=None,
                    updated_at_utc=now,
                )
            )
            connection.execute(
                update(chat_generation_execution_table)
                .where(chat_generation_execution_table.c.execution_id == execution_id)
                .values(status="completed", updated_at_utc=now)
            )
            self._append_terminal(
                connection,
                generation_id=str(generation["id"]),
                event_type="done",
                data={
                    "generation_id": str(generation["id"]),
                    "message_id": str(generation["message_id"]),
                    "status": "completed",
                },
                now=now,
            )

    def _stop_terminal(self, *, execution_id: str, generation_id: str, stop_reason: str) -> None:
        with self._engine.begin() as connection:
            now = self._now(connection)
            self._lock_execution(
                connection,
                generation_id=generation_id,
                execution_id=execution_id,
            )
            generation = self._lock_generation(connection, generation_id=generation_id)
            if generation is None:
                return
            if str(generation["status"]) not in {"stop_requested", "running"}:
                return
            self._stop_terminal_in_transaction(
                connection,
                execution_id=execution_id,
                generation=generation,
                stop_reason=stop_reason,
                now=now,
            )

    def _cancel_stale_execution(
        self,
        connection: Connection,
        *,
        execution_id: str,
        generation_id: str,
        now: datetime,
    ) -> None:
        self._lock_execution(
            connection,
            generation_id=generation_id,
            execution_id=execution_id,
        )
        generation = self._lock_generation(connection, generation_id=generation_id)
        if generation is None:
            self._cancel_execution_row(connection, execution_id=execution_id, now=now)
            return
        if str(generation["status"]) == "stop_requested":
            self._stop_terminal_in_transaction(
                connection,
                execution_id=execution_id,
                generation=generation,
                stop_reason=str(generation["stop_reason"] or "manual_request"),
                now=now,
            )
            return
        connection.execute(
            update(chat_generation_execution_table)
            .where(
                chat_generation_execution_table.c.execution_id == execution_id,
                chat_generation_execution_table.c.status == "running",
            )
            .values(
                status="cancelled",
                lease_owner=None,
                lease_expires_at_utc=None,
                updated_at_utc=now,
            )
        )

    @staticmethod
    def _cancel_execution_row(connection: Connection, *, execution_id: str, now: datetime) -> None:
        connection.execute(
            update(chat_generation_execution_table)
            .where(
                chat_generation_execution_table.c.execution_id == execution_id,
                chat_generation_execution_table.c.status == "running",
            )
            .values(
                status="cancelled",
                lease_owner=None,
                lease_expires_at_utc=None,
                updated_at_utc=now,
            )
        )

    def _stop_terminal_in_transaction(
        self,
        connection: Connection,
        *,
        execution_id: str,
        generation: Mapping[str, Any],
        stop_reason: str,
        now: datetime,
    ) -> None:
        generation_id = str(generation["id"])
        connection.execute(
            update(chat_generation_table)
            .where(chat_generation_table.c.id == generation_id)
            .values(
                status="stopped",
                stop_reason=stop_reason,
                updated_at_utc=now,
            )
        )
        connection.execute(
            update(chat_message_table)
            .where(chat_message_table.c.generation_id == generation_id)
            .values(
                status="stopped",
                stop_reason=stop_reason,
                updated_at_utc=now,
            )
        )
        connection.execute(
            update(chat_generation_execution_table)
            .where(chat_generation_execution_table.c.execution_id == execution_id)
            .values(
                status="cancelled",
                lease_owner=None,
                lease_expires_at_utc=None,
                updated_at_utc=now,
            )
        )
        self._discard_unfinished_ab_pair(connection, generation_id=generation_id, now=now)
        self._append_terminal(
            connection,
            generation_id=generation_id,
            event_type="stopped",
            data={
                "generation_id": generation_id,
                "message_id": str(generation["message_id"]),
                "status": "stopped",
                "stop_reason": stop_reason,
            },
            now=now,
        )

    def _fail_execution(
        self, *, execution_id: str, generation_id: str, error: PlatformError
    ) -> None:
        with self._engine.begin() as connection:
            now = self._now(connection)
            if error.code == "generation_state_conflict":
                self._cancel_stale_execution(
                    connection,
                    execution_id=execution_id,
                    generation_id=generation_id,
                    now=now,
                )
                return
            self._lock_execution(
                connection,
                generation_id=generation_id,
                execution_id=execution_id,
            )
            generation = self._lock_generation(connection, generation_id=generation_id)
            if generation is None:
                self._cancel_execution_row(connection, execution_id=execution_id, now=now)
                return
            if str(generation["status"]) == "stop_requested":
                self._stop_terminal_in_transaction(
                    connection,
                    execution_id=execution_id,
                    generation=generation,
                    stop_reason=str(generation["stop_reason"] or "authorization_revoked"),
                    now=now,
                )
                return
            if str(generation["status"]) != "running":
                return
            connection.execute(
                update(chat_generation_table)
                .where(chat_generation_table.c.id == generation_id)
                .values(
                    status="failed",
                    last_error_code=error.code,
                    updated_at_utc=now,
                )
            )
            connection.execute(
                update(chat_message_table)
                .where(chat_message_table.c.generation_id == generation_id)
                .values(status="failed", updated_at_utc=now)
            )
            connection.execute(
                update(chat_generation_execution_table)
                .where(chat_generation_execution_table.c.execution_id == execution_id)
                .values(
                    status="failed",
                    last_error_classification=error.code,
                    updated_at_utc=now,
                )
            )
            self._discard_unfinished_ab_pair(connection, generation_id=generation_id, now=now)
            self._append_terminal(
                connection,
                generation_id=generation_id,
                event_type="error",
                data={
                    "code": error.code,
                    "message": error.message,
                    "details": dict(error.details),
                    "request_id": "req_system",
                },
                now=now,
            )

    # ----------------------------------------------------------------- helpers

    def _fence_current(
        self,
        connection: Connection,
        *,
        generation_id: str,
        execution_id: str,
        fencing_token: int,
        control_version: int,
    ) -> bool:
        execution = self._lock_execution(
            connection,
            generation_id=generation_id,
            execution_id=execution_id,
        )
        generation = self._lock_generation(connection, generation_id=generation_id)
        return bool(
            execution is not None
            and generation is not None
            and int(execution["fencing_token"]) == fencing_token
            and str(execution["status"]) == "running"
            and int(generation["control_version"]) == control_version
            and str(generation["status"]) in {"running", "stop_requested"}
        )

    def _emit_stage(
        self,
        *,
        generation_id: str,
        execution_id: str,
        fencing_token: int,
        control_version: int,
        phase: str,
        generation: Mapping[str, Any],
    ) -> None:
        del generation
        with self._engine.begin() as connection:
            if not self._fence_current(
                connection,
                generation_id=generation_id,
                execution_id=execution_id,
                fencing_token=fencing_token,
                control_version=control_version,
            ):
                raise PlatformError(
                    "generation_state_conflict",
                    "The generation control fence was invalidated",
                    {},
                    409,
                )
            self._append_terminal(
                connection,
                generation_id=generation_id,
                event_type="stage",
                data={"phase": phase},
                now=self._now(connection),
            )

    def _emit_notice(
        self,
        *,
        generation_id: str,
        execution_id: str,
        fencing_token: int,
        control_version: int,
        kind: str,
        detail: Mapping[str, Any],
        generation: Mapping[str, Any],
    ) -> None:
        del generation
        with self._engine.begin() as connection:
            if not self._fence_current(
                connection,
                generation_id=generation_id,
                execution_id=execution_id,
                fencing_token=fencing_token,
                control_version=control_version,
            ):
                return
            self._append_terminal(
                connection,
                generation_id=generation_id,
                event_type="notice",
                data={"kind": kind, "detail": dict(detail)},
                now=self._now(connection),
            )
            notices = (
                connection.execute(
                    select(chat_message_table.c.notices_json).where(
                        chat_message_table.c.generation_id == generation_id
                    )
                ).scalar_one_or_none()
            ) or []
            notices = list(notices) if isinstance(notices, list) else [notices]
            notices.append({"kind": kind, "detail": dict(detail)})
            connection.execute(
                update(chat_message_table)
                .where(chat_message_table.c.generation_id == generation_id)
                .values(notices_json=notices, updated_at_utc=self._now(connection))
            )

    @staticmethod
    def _append_terminal(
        connection: Connection,
        *,
        generation_id: str,
        event_type: str,
        data: Mapping[str, Any],
        now: datetime,
    ) -> None:
        if event_type in {"done", "error", "stopped"} and has_terminal_event(
            connection, generation_id=generation_id
        ):
            return
        append_event(
            connection,
            generation_id=generation_id,
            event_type=event_type,
            data=dict(data),
            now=now,
        )

    def _lock_execution(
        self,
        connection: Connection,
        *,
        generation_id: str,
        execution_id: str,
    ) -> Mapping[str, Any] | None:
        statement = select(chat_generation_execution_table).where(
            chat_generation_execution_table.c.execution_id == execution_id,
            chat_generation_execution_table.c.generation_id == generation_id,
        )
        if connection.dialect.name != "sqlite":
            statement = statement.with_for_update()
        row = connection.execute(statement).mappings().one_or_none()
        return None if row is None else dict(row)

    def _lock_generation(
        self, connection: Connection, *, generation_id: str
    ) -> Mapping[str, Any] | None:
        statement = select(chat_generation_table).where(chat_generation_table.c.id == generation_id)
        if connection.dialect.name != "sqlite":
            statement = statement.with_for_update()
        row = connection.execute(statement).mappings().one_or_none()
        # A missing row means the conversation cascade deleted the generation;
        # deletion is itself a terminal state, not an error for the worker.
        return None if row is None else dict(row)

    def _read_generation(self, generation_id: str) -> Mapping[str, Any]:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    select(chat_generation_table).where(chat_generation_table.c.id == generation_id)
                )
                .mappings()
                .one()
            )
            return dict(row)

    def _execution_fence(self, generation_id: str, execution_id: str) -> int:
        with self._engine.connect() as connection:
            value = connection.execute(
                select(chat_generation_execution_table.c.fencing_token).where(
                    chat_generation_execution_table.c.execution_id == execution_id,
                    chat_generation_execution_table.c.generation_id == generation_id,
                )
            ).scalar_one()
            return int(value)

    @staticmethod
    def _message_id(connection: Connection, generation_id: str) -> str:
        value = connection.execute(
            select(chat_generation_table.c.message_id).where(
                chat_generation_table.c.id == generation_id
            )
        ).scalar_one()
        return str(value)

    def _pair_for_generation(self, *, generation_id: str) -> str | None:
        with self._engine.connect() as connection:
            return connection.execute(
                select(chat_ab_pair_table.c.pair_id).where(
                    chat_ab_pair_table.c.generation_id == generation_id
                )
            ).scalar_one_or_none()

    @staticmethod
    def _pair_row(connection: Connection, *, generation_id: str) -> Mapping[str, Any] | None:
        row = (
            connection.execute(
                select(chat_ab_pair_table).where(
                    chat_ab_pair_table.c.generation_id == generation_id
                )
            )
            .mappings()
            .one_or_none()
        )
        return dict(row) if row is not None else None

    @staticmethod
    def _discard_unfinished_ab_pair(
        connection: Connection, *, generation_id: str, now: datetime
    ) -> None:
        """Discard unpublished A/B candidates and expire the pair on failure/stop."""

        pair = (
            connection.execute(
                select(chat_ab_pair_table).where(
                    chat_ab_pair_table.c.generation_id == generation_id
                )
            )
            .mappings()
            .one_or_none()
        )
        if pair is None or str(pair["status"]) in {"voted", "expired"}:
            return
        pair_id = str(pair["pair_id"])
        connection.execute(
            update(chat_ab_candidate_table)
            .where(
                chat_ab_candidate_table.c.pair_id == pair_id,
                chat_ab_candidate_table.c.status == "planned",
            )
            .values(status="discarded")
        )
        connection.execute(
            update(chat_ab_pair_table)
            .where(chat_ab_pair_table.c.pair_id == pair_id)
            .values(status="expired", updated_at_utc=now)
        )


def _principal_from_generation(generation: Mapping[str, Any]) -> Any:
    class _Principal:
        def __init__(self, user_id: str, auth_session_id: str) -> None:
            self.user_id = user_id
            self.auth_session_id = auth_session_id
            self.username = ""
            self.role = "user"
            self.department_id = None

    return _Principal(
        user_id=str(generation["owner_user_id"]),
        auth_session_id=str(generation["auth_session_id"]),
    )


def _hit_mapping(hit: RetrievalHitOutcome) -> Mapping[str, Any]:
    return {
        "document_id": hit.document_id,
        "document_version_id": hit.document_version_id,
        "publication_id": hit.publication_id,
        "chunk_id": hit.chunk_id,
        "space_id": hit.space_id,
        "locator": dict(hit.locator),
        "snippet": hit.snippet,
    }


def _answer_mode(hits: tuple[RetrievalHitOutcome, ...], citations: list[Mapping[str, Any]]) -> str:
    if not hits:
        return "no_context"
    if not citations:
        return "direct"
    return "grounded"


def _rouge_l(left: str, right: str) -> float:
    """Normalized ROUGE-L F-measure on whitespace tokens via LCS."""

    left_tokens = left.split()
    right_tokens = right.split()
    if not left_tokens or not right_tokens:
        return 1.0 if left_tokens == right_tokens else 0.0
    lcs = _lcs_length(left_tokens, right_tokens)
    recall = lcs / len(left_tokens)
    precision = lcs / len(right_tokens)
    if recall + precision == 0:
        return 0.0
    return 2 * recall * precision / (recall + precision)


def _lcs_length(left: list[str], right: list[str]) -> int:
    previous = [0] * (len(right) + 1)
    for _i, left_token in enumerate(left, start=1):
        current = [0] * (len(right) + 1)
        for j, right_token in enumerate(right, start=1):
            if left_token == right_token:
                current[j] = previous[j - 1] + 1
            else:
                current[j] = max(previous[j], current[j - 1])
        previous = current
    return previous[len(right)]


__all__ = [
    "EXECUTION_LEASE_SECONDS",
    "ChatGenerationWorker",
    "WorkerOutcome",
]
