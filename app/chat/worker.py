"""Durable execution worker: claim, run, fence, checkpoint and converge terminals."""

from __future__ import annotations

import json
import logging
import re
import threading
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.engine import Connection, Engine

from app.agents.selfeval import (
    AcceptingSelfEvaluationPort,
    DeepRetrievalStrategyPlan,
    HeuristicSelfEvaluationPort,
    SelfEvaluationPort,
)
from app.indexing.models import DEEP_RETRIEVAL_STRATEGIES, DeepRetrievalStrategy
from app.platform.errors import PlatformError
from app.usage.ledger import OwnershipSnapshot, ProviderMeasurement
from app.usage.ports import UsageSubmissionPort
from app.usage.reconcile import (
    ConfirmedNotSent,
    ConfirmedUsage,
    ReconciliationOnlyAmount,
    StillUnknown,
)

from .budget import (
    EFFORT_UPGRADE_CHAIN,
    BudgetMeter,
    BudgetPolicy,
    EffortPolicy,
    GenerationBudget,
    conservative_chat_token_estimate,
    effort_policy,
    select_budget_candidates,
)
from .events import append_event, has_terminal_event
from .leases import generation_has_active_lease
from .models import NOTICE_KINDS, RetrievalHitOutcome
from .ports import (
    CalibrationWindowPort,
    ChatProviderPort,
    ChatProviderRequest,
    ChatRetrievalPort,
    consume_durable_revocation_commands,
    source_conflict_contract,
)
from .schema import (
    chat_ab_candidate_table,
    chat_ab_pair_table,
    chat_generation_execution_table,
    chat_generation_table,
    chat_message_table,
)

_logger = logging.getLogger(__name__)

EXECUTION_LEASE_SECONDS = 90
HEARTBEAT_SECONDS = 30
MAX_PHYSICAL_EXECUTIONS = 3
# Conversation context (work item B): the latest turns enter the provider
# prompt and the retrieval router's recent-queries signal.
CONVERSATION_HISTORY_TURNS = 3
ASSISTANT_HISTORY_DIGEST_CHARS = 500
# Candidates at/above this ROUGE-L similarity are near duplicates and the
# pair is collapsed instead of being offered for a vote.
AB_NEAR_DUPLICATE_ROUGE_L = 0.92
# Renewal cadence for the background execution heartbeat; well below the lease TTL.


def _utc(value: Any) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


# Delta events aggregate provider chunks before each durable write: one DB
# write per chunk would dominate worker latency, so flush on either threshold.
_DELTA_FLUSH_CHARS = 240
_DELTA_FLUSH_SECONDS = 0.4


class _DeltaStreamSink:
    """Aggregates provider stream chunks into best-effort delta event writes."""

    def __init__(self, *, emit: Callable[[str], None], clock: Callable[[], datetime]) -> None:
        self._emit = emit
        self._clock = clock
        self._parts: list[str] = []
        self._length = 0
        self._last_flush = clock()

    def __call__(self, text: str) -> None:
        self._parts.append(text)
        self._length += len(text)
        if self._length >= _DELTA_FLUSH_CHARS or (
            (self._clock() - self._last_flush).total_seconds() >= _DELTA_FLUSH_SECONDS
        ):
            self.flush()

    def flush(self) -> None:
        if not self._parts:
            return
        text = "".join(self._parts)
        self._parts = []
        self._length = 0
        self._last_flush = self._clock()
        self._emit(text)


def _source_identity(item: Mapping[str, Any]) -> tuple[str, str, str, str]:
    """Identity used by the final publication recheck.

    A document/chunk pair is not sufficient: replacing the active version or
    publication must invalidate a candidate even when the physical chunk IDs
    happen to be reused.
    """

    return (
        str(item.get("document_id", "")),
        str(item.get("document_version_id", "")),
        str(item.get("publication_id", "")),
        str(item.get("chunk_id", "")),
    )


def _public_route_summary(route_output: Mapping[str, Any] | None) -> dict[str, Any]:
    """Keep SSE route telemetry to stable choices, never model text or tool inputs."""

    route = route_output if isinstance(route_output, Mapping) else {}
    kind = str(route.get("kind") or "no_rewrite")
    if kind not in {"no_rewrite", "rewrite", "split_subquestions", "hyde"}:
        kind = "no_rewrite"
    raw_strategies = route.get("strategy_operations", ())
    strategies = (
        [str(operation) for operation in raw_strategies if operation in DEEP_RETRIEVAL_STRATEGIES]
        if isinstance(raw_strategies, (list, tuple))
        else []
    )
    granularity = str(route.get("return_granularity") or "parent_document")
    if granularity not in {"sub_chunk", "parent_document", "document_summary"}:
        granularity = "parent_document"
    return {"kind": kind, "strategies": strategies, "return_granularity": granularity}


@dataclass(slots=True)
class WorkerOutcome:
    executed: str | None
    stage: str
    details: dict[str, Any]


@dataclass(slots=True)
class ProviderCallOutcome:
    response: Any
    provider_call_id: str
    measurement: ProviderMeasurement
    ownership: OwnershipSnapshot
    started_at_utc: datetime
    provider_request_id: str | None


@dataclass(frozen=True, slots=True)
class RetrievalStageResult:
    """One retrieval round: selected hits, published citations, route summary."""

    hits: tuple[RetrievalHitOutcome, ...]
    citations: list[Mapping[str, Any]]
    route_summary: dict[str, Any] | None
    step_index: int


@dataclass(frozen=True, slots=True)
class GenerationStageResult:
    """One candidate-production pass over the frozen retrieval context."""

    candidates: list[dict[str, Any]]


@dataclass(frozen=True, slots=True)
class SelfEvaluationOutcome:
    """Terminal self-evaluation signal: accept the draft or rewrite once more."""

    accepted: bool
    rewritten_query: str | None


@dataclass(frozen=True, slots=True)
class EffortUpgradeOutcome:
    """Single effort-upgrade transition shared by both loop sites."""

    upgraded: bool
    fence_lost: bool
    generation: Mapping[str, Any]
    # Mirrors the usage-meter snapshot type used by the pre-refactor loop:
    # ``Any`` until ``_build_rag_budget_meter`` settles its snapshot contract.
    budget_meter_snapshot: Any | None = None


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
        budget_meter: Any | None = None,
        self_evaluator: SelfEvaluationPort | None = None,
        lease_seconds: int = EXECUTION_LEASE_SECONDS,
        max_physical_executions: int = MAX_PHYSICAL_EXECUTIONS,
        effort_rag_limits: Mapping[str, int] | None = None,
        disconnect_grace_seconds: int = 60,
        provider_reconciliation: Any | None = None,
        max_scope_retries: int = 1,
        tool_retrieval: ChatRetrievalPort | None = None,
    ) -> None:
        self._engine = engine
        self._clock = clock
        # Model-invoked tool searches run on their own port instance: the
        # production adapter binds one retrieval request per port and the
        # mainline binding must survive until publication revalidates
        # citations against it.
        self._retrieval = retrieval
        self._tool_retrieval = tool_retrieval or retrieval
        self._provider = provider
        self._usage = usage
        self._calibration = calibration
        self._budget_meter = budget_meter
        # Generation-side self-evaluation step (A6/I): think/deep evaluate each
        # candidate and may trigger a bounded rewrite -> re-retrieve loop; the
        # evaluation judge itself is owned by the evaluation capability.
        self._self_evaluator = self_evaluator or HeuristicSelfEvaluationPort()
        self._lease_seconds = lease_seconds
        self._max_physical_executions = max_physical_executions
        self._effort_rag_limits = effort_rag_limits
        self._disconnect_grace_seconds = disconnect_grace_seconds
        self._provider_reconciliation = provider_reconciliation
        self._max_scope_retries = max_scope_retries

    def _now(self, connection: Connection | None = None) -> datetime:
        value = self._clock.now_utc(connection)
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value

    # ------------------------------------------------------------- maintenance

    def run_maintenance(self) -> dict[str, Any]:
        """Reap disconnect grace, recover leases, reconcile deadlines, expire A/B pairs."""

        with self._engine.begin() as connection:
            now = self._now(connection)
            consumed = consume_durable_revocation_commands(connection, now=now)
            self._reap_disconnect_grace(connection, now=now)
            recovered = self._recover_expired_executions(connection, now=now)
            # Do not let an unavailable external reconciliation adapter prevent
            # an already-expired unknown result from reaching its terminal state.
            self._expire_provider_reconciling_executions(connection, now=now)
        reconciled = self._reconcile_provider_reconciling(now=now)
        with self._engine.begin() as connection:
            self._expire_provider_reconciling_executions(connection, now=now)
            expired_pairs = self._expire_past_deadline_pairs(connection, now=now)
        return {
            "revocation_commands_consumed": consumed,
            "executions_recovered": recovered,
            "provider_calls_reconciled": reconciled,
            "ab_pairs_expired": expired_pairs,
        }

    def _reap_disconnect_grace(self, connection: Connection, *, now: datetime) -> int:
        rows = (
            connection.execute(
                select(
                    chat_generation_table.c.id,
                    chat_generation_table.c.disconnect_deadline_at_utc,
                ).where(
                    chat_generation_table.c.status == "running",
                    chat_generation_table.c.disconnect_deadline_at_utc.is_not(None),
                )
                # Concurrent maintenance instances must iterate candidates in
                # the same order so their row locks never cross.
                .order_by(chat_generation_table.c.id)
            )
            .mappings()
            .all()
        )
        stopped = 0
        for row in rows:
            if _utc(row["disconnect_deadline_at_utc"]) > now:
                continue
            generation_id = str(row["id"])
            if generation_has_active_lease(connection, generation_id=generation_id, now=now):
                connection.execute(
                    update(chat_generation_table)
                    .where(chat_generation_table.c.id == generation_id)
                    .values(disconnect_deadline_at_utc=None, updated_at_utc=now)
                )
                continue
            # Same lock order as the execution paths (execution rows first,
            # then the generation row) so a concurrent claim/stop/fail can
            # never deadlock against this reaper on real PostgreSQL.
            self._lock_generation_executions(connection, generation_id=generation_id)
            generation = self._lock_generation(connection, generation_id=generation_id)
            if (
                generation is None
                or str(generation["status"]) != "running"
                or generation["disconnect_deadline_at_utc"] is None
                or _utc(generation["disconnect_deadline_at_utc"]) > now
            ):
                continue
            updated = connection.execute(
                update(chat_generation_table)
                .where(
                    chat_generation_table.c.id == generation_id,
                    chat_generation_table.c.status == "running",
                )
                .values(
                    status="stop_requested",
                    stop_reason="client_disconnected",
                    control_version=chat_generation_table.c.control_version + 1,
                    updated_at_utc=now,
                )
            ).rowcount
            if updated:
                # Persist the intermediate state/event before terminal close.
                # This makes the durable lifecycle observable to SSE replayers.
                append_event(
                    connection,
                    generation_id=generation_id,
                    event_type="stop_requested",
                    data={
                        "generation_id": generation_id,
                        "reason": "client_disconnected",
                    },
                    now=now,
                )
                self._discard_unfinished_ab_pair(connection, generation_id=generation_id, now=now)
                connection.execute(
                    update(chat_generation_table)
                    .where(chat_generation_table.c.id == generation_id)
                    .values(status="stopped", updated_at_utc=now)
                )
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
                connection.execute(
                    update(chat_generation_execution_table)
                    .where(
                        chat_generation_execution_table.c.generation_id == generation_id,
                        chat_generation_execution_table.c.status.in_(
                            ["queued", "retry_wait", "running"]
                        ),
                    )
                    .values(
                        status="cancelled",
                        lease_owner=None,
                        lease_expires_at_utc=None,
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
                    chat_generation_execution_table.c.checkpoint_version,
                    chat_generation_execution_table.c.checkpoint_json,
                ).where(
                    chat_generation_execution_table.c.status == "running",
                )
                # Same cross-instance ordering discipline as the reaper.
                .order_by(chat_generation_execution_table.c.execution_id)
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
                    checkpoint_version=int(row["checkpoint_version"] or 0),
                    checkpoint_json=row["checkpoint_json"],
                    next_attempt_at_utc=now,
                    last_error_classification=None,
                    created_at_utc=now,
                    updated_at_utc=now,
                )
            )
            recovered += 1
        return recovered

    def _expire_provider_reconciling_executions(
        self, connection: Connection, *, now: datetime
    ) -> int:
        """Terminalize unknown provider results only after the generation deadline."""

        rows = (
            connection.execute(
                select(
                    chat_generation_execution_table.c.execution_id,
                    chat_generation_execution_table.c.generation_id,
                ).where(
                    chat_generation_execution_table.c.status == "provider_reconciling",
                )
                # Same cross-instance ordering discipline as the reaper.
                .order_by(chat_generation_execution_table.c.execution_id)
            )
            .mappings()
            .all()
        )
        expired = 0
        for row in rows:
            generation_id = str(row["generation_id"])
            # Same lock order as the execution paths (execution row first, then
            # the generation row) so a concurrent stop/fail can never deadlock
            # against this expirer on real PostgreSQL.
            if (
                self._lock_execution(
                    connection,
                    generation_id=generation_id,
                    execution_id=str(row["execution_id"]),
                )
                is None
            ):
                continue
            generation = self._lock_generation(connection, generation_id=generation_id)
            if generation is None or str(generation["status"]) != "running":
                continue
            if _utc(generation["absolute_deadline_at_utc"]) > now:
                continue
            updated = connection.execute(
                update(chat_generation_table)
                .where(
                    chat_generation_table.c.id == generation_id,
                    chat_generation_table.c.status == "running",
                )
                .values(
                    status="failed",
                    last_error_code="provider_result_unknown",
                    updated_at_utc=now,
                )
            ).rowcount
            if updated != 1:
                continue
            connection.execute(
                update(chat_message_table)
                .where(chat_message_table.c.generation_id == generation_id)
                .values(status="failed", updated_at_utc=now)
            )
            connection.execute(
                update(chat_generation_execution_table)
                .where(
                    chat_generation_execution_table.c.execution_id == row["execution_id"],
                    chat_generation_execution_table.c.generation_id == generation_id,
                    chat_generation_execution_table.c.status == "provider_reconciling",
                )
                .values(
                    status="failed",
                    lease_owner=None,
                    lease_expires_at_utc=None,
                    checkpoint_json=None,
                    last_error_classification="provider_result_unknown",
                    updated_at_utc=now,
                )
            )
            self._discard_unfinished_ab_pair(connection, generation_id=generation_id, now=now)
            self._append_terminal(
                connection,
                generation_id=generation_id,
                event_type="error",
                data={
                    "code": "provider_result_unknown",
                    "message": "The provider result could not be reconciled before the deadline",
                    "details": {},
                    "request_id": str(generation.get("request_id") or "req_system"),
                },
                now=now,
            )
            expired += 1
        return expired

    def _reconcile_provider_reconciling(self, *, now: datetime) -> int:
        """Resolve unknown provider calls without holding a database transaction
        across the provider query.

        A confirmed usage is first recovered in the usage ledger, then the chat
        execution is made runnable with the persisted response checkpoint.  A
        confirmed-not-sent call is simply requeued for the same generation.
        """

        port = self._provider_reconciliation
        if port is None:
            return 0
        with self._engine.connect() as connection:
            rows = (
                connection.execute(
                    select(
                        chat_generation_execution_table.c.execution_id,
                        chat_generation_execution_table.c.generation_id,
                        chat_generation_execution_table.c.checkpoint_json,
                        chat_generation_execution_table.c.status,
                        chat_generation_table.c.absolute_deadline_at_utc,
                    )
                    .join(
                        chat_generation_table,
                        chat_generation_table.c.id
                        == chat_generation_execution_table.c.generation_id,
                    )
                    .where(chat_generation_execution_table.c.status == "provider_reconciling")
                )
                .mappings()
                .all()
            )
            calls_by_execution: dict[str, dict[str, Any]] = {}
            from app.usage.schema import provider_call_table

            for call_row in connection.execute(
                select(provider_call_table).where(
                    provider_call_table.c.execution_id.in_([r["execution_id"] for r in rows])
                )
            ).mappings():
                calls_by_execution.setdefault(str(call_row["execution_id"]), dict(call_row))
        reconciled = 0
        for row in rows:
            call = calls_by_execution.get(str(row["execution_id"]))
            if call is None:
                continue
            decision = port.confirm(
                provider_call_id=str(call["provider_call_id"]),
                fingerprint=str(call["request_fingerprint"]),
                connection=None,
            )
            if not isinstance(
                decision, (ConfirmedUsage, ConfirmedNotSent, StillUnknown, ReconciliationOnlyAmount)
            ):
                raise PlatformError(
                    "provider_reconciliation_contract_error",
                    "Provider reconciliation returned an invalid decision",
                    {},
                    502,
                    True,
                )
            if isinstance(decision, StillUnknown):
                continue
            execution_id = str(row["execution_id"])
            if isinstance(decision, ConfirmedUsage):
                checkpoint = dict(row["checkpoint_json"] or {})
                pending = checkpoint.get("pending_candidate")
                # A usage-only confirmation is sufficient for the generic
                # ledger reconciler, but chat cannot publish without the
                # original response body. Leave it reconciling until the
                # provider adapter can supply that result (or the deadline
                # produces provider_result_unknown) rather than publishing an
                # empty answer.
                if isinstance(pending, Mapping) and decision.content is None:
                    continue
                self._usage.recover_unknown_call(
                    provider_call_id=str(call["provider_call_id"]),
                    measurement=decision.measurement,
                    ownership=decision.ownership,
                    result=decision.result,
                    provider_request_id=decision.provider_request_id,
                    started_at_utc=decision.started_at_utc,
                )
                if isinstance(pending, Mapping):
                    pending = dict(pending)
                    pending["content"] = str(decision.content)
                    # The recovered content is now known, so narrow the
                    # checkpoint citations to the actually referenced subset
                    # (A4) exactly like the in-process publication path.
                    visible = tuple(pending.get("citations") or ())
                    referenced = _referenced_citations(pending["content"], visible)
                    pending["citations"] = [dict(item) for item in referenced]
                    pending["answer_mode"] = (
                        "no_context" if not visible else ("grounded" if referenced else "direct")
                    )
                    next_checkpoint = {
                        "phase": "provider_reconciled",
                        "candidates": [pending],
                    }
                else:
                    next_checkpoint = None
                with self._engine.begin() as connection:
                    connection.execute(
                        update(chat_generation_execution_table)
                        .where(
                            chat_generation_execution_table.c.execution_id == execution_id,
                            chat_generation_execution_table.c.status == "provider_reconciling",
                        )
                        .values(
                            status="retry_wait",
                            next_attempt_at_utc=now,
                            checkpoint_json=next_checkpoint,
                            last_error_classification=None,
                            updated_at_utc=now,
                        )
                    )
                reconciled += 1
            elif isinstance(decision, ConfirmedNotSent):
                self._usage.mark_not_sent(str(call["provider_call_id"]))
                with self._engine.begin() as connection:
                    connection.execute(
                        update(chat_generation_execution_table)
                        .where(
                            chat_generation_execution_table.c.execution_id == execution_id,
                            chat_generation_execution_table.c.status == "provider_reconciling",
                        )
                        .values(
                            status="retry_wait",
                            next_attempt_at_utc=now,
                            checkpoint_json=None,
                            last_error_classification=None,
                            updated_at_utc=now,
                        )
                    )
                reconciled += 1
            else:
                # Amount-only accounting is owned by the usage reconciler. Chat
                # remains pending until a complete result is confirmed.
                continue
        return reconciled

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
                    chat_generation_table.c.request_id,
                    chat_generation_table.c.absolute_deadline_at_utc,
                ).where(chat_generation_table.c.id == generation_id)
            )
            .mappings()
            .one()
        )
        if str(generation["status"]) in {"running", "stop_requested"}:
            # The two unrecoverable shapes keep distinct codes (后端设计 §2.5):
            # the absolute deadline reached vs recovery quota exhausted early.
            deadline_passed = _utc(generation["absolute_deadline_at_utc"]) <= now
            error_code = (
                "generation_deadline_exceeded"
                if deadline_passed
                else "execution_recovery_exhausted"
            )
            error_message = (
                "The generation deadline expired before further execution"
                if deadline_passed
                else "The generation could not be recovered before its deadline"
            )
            connection.execute(
                update(chat_generation_table)
                .where(chat_generation_table.c.id == generation_id)
                .values(
                    status="failed",
                    last_error_code=error_code,
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
                    "code": error_code,
                    "message": error_message,
                    "details": {},
                    "request_id": str(generation.get("request_id") or "req_system"),
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
            claimable = (
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
            if connection.dialect.name == "postgresql":
                # Concurrent workers must not queue behind the same candidate:
                # SKIP LOCKED lets each claim the next free execution instead
                # of idling (outbox dispatcher uses the same pattern).
                claimable = claimable.with_for_update(skip_locked=True)
            due = connection.execute(claimable).mappings().first()
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

    def _build_rag_budget_meter(
        self,
        effort: str,
        snapshot: Mapping[str, Any] | None,
        deadline: datetime,
    ) -> BudgetMeter:
        """Build one logical-operation meter whether or not usage persistence is configured."""

        pricer: Callable[[str, int], float | None]
        if self._budget_meter is None:
            price_version = "local-logical"

            def local_pricer(_operation: str, _tokens: int) -> float:
                return 0.0

            pricer = local_pricer
        else:
            usage = self._budget_meter

            def usage_pricer(operation: str, tokens: int) -> float | None:
                try:
                    return float(usage.estimate_cost(operation, tokens))
                except Exception:
                    return None

            pricer = usage_pricer
            price_version = str((snapshot or {}).get("price_version_id") or "usage-metering")
        ceiling = (snapshot or {}).get("max_estimated_cost_amount")
        try:
            max_cost = float(ceiling) if ceiling is not None else 1000.0
        except (TypeError, ValueError):
            max_cost = 1000.0
        policy = BudgetPolicy.for_effort(
            effort,
            price_version=price_version,
            max_estimated_cost_amount=max(0.01, max_cost),
            pricer=pricer,
            effort_rag_limits=self._effort_rag_limits,
        )
        return BudgetMeter(policy=policy, deadline=deadline)

    def _run_retrieval_stage(
        self,
        *,
        generation: Mapping[str, Any],
        execution_id: str,
        fencing_token: int,
        control_version: int,
        query: str,
        budget: GenerationBudget,
        rag_budget_meter: BudgetMeter,
        budget_meter_snapshot: Any | None,
        profile_id: str,
        profile_version: str,
        reservation_id: str | None,
        step_index: int,
        strategy_operations: tuple[DeepRetrievalStrategy, ...],
        select_candidates: bool,
        emit_routed_event: bool,
        recent_queries: Sequence[str] = (),
        emit_step_events: bool = False,
    ) -> RetrievalStageResult:
        """One retrieval round: step events, search, notices and citations."""

        round_step = None
        if emit_step_events:
            step_index += 1
            round_step = step_index
            self._emit_step(
                generation_id=str(generation["id"]),
                execution_id=execution_id,
                fencing_token=fencing_token,
                control_version=control_version,
                index=round_step,
                label=f"retrieve_round_{round_step}",
                state="active",
            )
        outcome = self._retrieval.search(
            query,
            principal=_principal_from_generation(generation),
            narrowing_scope=generation["request_scope_json"],
            profile_id=profile_id,
            profile_version=profile_version,
            effort=budget.effort_level,
            budget=rag_budget_meter,
            strategy_operations=strategy_operations,
            recent_queries=recent_queries,
        )
        if self._budget_meter is not None:
            assert reservation_id is not None
            self._budget_meter.settle(
                generation_id=str(generation["id"]),
                reservation_id=reservation_id,
                actual_tokens=0,
                actual_cost=Decimal("0"),
            )
        hits = outcome.hits
        route_summary: dict[str, Any] | None = None
        if emit_routed_event:
            route_summary = _public_route_summary(outcome.route_output)
            self._emit_stage(
                generation_id=str(generation["id"]),
                execution_id=execution_id,
                fencing_token=fencing_token,
                control_version=control_version,
                phase="retrieval_routed",
                generation=generation,
                detail={"route": route_summary},
            )
        degradations = tuple(outcome.degradations)
        if select_candidates:
            hits, missing_identities = select_budget_candidates(
                hits,
                limit=(
                    budget_meter_snapshot.candidate_document_limit
                    if budget_meter_snapshot is not None
                    else effort_policy(budget.effort_level).max_candidate_documents
                ),
            )
            degradations = degradations + tuple(dict(item) for item in missing_identities)
        budget.record_rag_round()
        for item in degradations:
            code = str(item.get("code") or "")
            kind = code if code in NOTICE_KINDS else "retrieval_degraded"
            self._emit_notice(
                generation_id=str(generation["id"]),
                execution_id=execution_id,
                fencing_token=fencing_token,
                control_version=control_version,
                kind=kind,
                detail=dict(item),
                generation=generation,
            )
        citations = self._resolve_citations(hits, generation)
        if round_step is not None:
            self._emit_step(
                generation_id=str(generation["id"]),
                execution_id=execution_id,
                fencing_token=fencing_token,
                control_version=control_version,
                index=round_step,
                label=f"retrieve_round_{round_step}",
                state="done",
            )
        return RetrievalStageResult(
            hits=hits,
            citations=citations,
            route_summary=route_summary,
            step_index=step_index,
        )

    def _run_generation_stage(
        self,
        *,
        generation: Mapping[str, Any],
        execution_id: str,
        fencing_token: int,
        control_version: int,
        candidate_config_versions: tuple[str, str] | None,
        hits: tuple[RetrievalHitOutcome, ...],
        citations: list[Mapping[str, Any]],
        rag_budget_meter: BudgetMeter,
        query: str,
        history_messages: tuple[Mapping[str, Any], ...] = (),
        recent_queries: Sequence[str] = (),
        policy: EffortPolicy | None = None,
        resume_state: dict[str, Any] | None = None,
    ) -> GenerationStageResult:
        """Produce answer candidates, streaming provider deltas through the sink."""

        effort_policy_value = policy or effort_policy(str(generation["effective_effort_level"]))
        delta_sink = self._open_delta_sink(
            generation=generation,
            execution_id=execution_id,
            fencing_token=fencing_token,
            control_version=control_version,
            candidate_config_versions=candidate_config_versions,
            enabled=effort_policy_value.stream_deltas,
        )
        try:
            candidates = self._produce_candidates(
                generation=generation,
                execution_id=execution_id,
                fencing_token=fencing_token,
                control_version=control_version,
                hits=hits,
                citations=citations,
                candidate_config_versions=candidate_config_versions,
                retrieval_budget=rag_budget_meter,
                query=query,
                logical_budget=rag_budget_meter,
                on_delta=delta_sink,
                history_messages=history_messages,
                recent_queries=recent_queries,
                policy=effort_policy_value,
                resume_state=resume_state,
            )
        finally:
            if delta_sink is not None:
                delta_sink.flush()
        return GenerationStageResult(candidates=candidates)

    def _run_self_evaluation_stage(
        self,
        *,
        evaluator: SelfEvaluationPort,
        query: str,
        candidates: list[dict[str, Any]],
        citations: list[Mapping[str, Any]],
        hits: tuple[RetrievalHitOutcome, ...],
    ) -> SelfEvaluationOutcome:
        """Evaluate the lead candidate; a rejected draft may supply a rewrite."""

        try:
            evaluation = evaluator.evaluate(
                query=query,
                candidate_content=candidates[0]["content"],
                citations=tuple(citations),
                context_items=tuple(_hit_mapping(hit) for hit in hits),
            )
        except Exception:
            self._complete_deferred_provider_calls_public(candidates)
            raise
        return SelfEvaluationOutcome(
            accepted=bool(evaluation.acceptable or evaluation.rewritten_query is None),
            rewritten_query=evaluation.rewritten_query,
        )

    def _try_upgrade_effort(
        self,
        *,
        generation: Mapping[str, Any],
        execution_id: str,
        fencing_token: int,
        control_version: int,
        budget: GenerationBudget,
        rag_budget_meter: BudgetMeter,
        budget_meter_snapshot: Any | None,
        hits: tuple[RetrievalHitOutcome, ...],
        estimate_query: str,
    ) -> EffortUpgradeOutcome:
        """The single effort-upgrade transition (chain: at most one level)."""

        previous_effort = budget.effort_level
        upgraded = EFFORT_UPGRADE_CHAIN.get(previous_effort)
        if upgraded is None:
            return EffortUpgradeOutcome(
                upgraded=False,
                fence_lost=False,
                generation=generation,
                budget_meter_snapshot=budget_meter_snapshot,
            )
        if self._budget_meter is not None:
            next_step_tokens = conservative_chat_token_estimate(
                estimate_query,
                (hit.snippet for hit in hits),
            )
            if (
                self._budget_meter.upgrade(
                    generation_id=str(generation["id"]),
                    next_step_tokens=next_step_tokens,
                    next_step_cost=self._budget_meter.estimate_cost(
                        "chat_generation", next_step_tokens
                    ),
                    next_step_is_rag=True,
                )
                != upgraded
            ):
                return EffortUpgradeOutcome(
                    upgraded=False,
                    fence_lost=False,
                    generation=generation,
                    budget_meter_snapshot=budget_meter_snapshot,
                )
            budget_meter_snapshot = self._budget_meter.meter(generation_id=str(generation["id"]))
        upgraded = budget.upgrade_effort()
        assert upgraded is not None
        assert rag_budget_meter.upgrade_policy(
            self._build_rag_budget_meter(
                upgraded,
                budget_meter_snapshot,
                _utc(generation["absolute_deadline_at_utc"]),
            ).policy
        )
        if not self._persist_effort_upgrade(
            generation_id=str(generation["id"]),
            execution_id=execution_id,
            fencing_token=fencing_token,
            control_version=control_version,
            previous_effort=previous_effort,
            upgraded_effort=upgraded,
        ):
            return EffortUpgradeOutcome(
                upgraded=False,
                fence_lost=True,
                generation=generation,
                budget_meter_snapshot=budget_meter_snapshot,
            )
        generation = {
            **generation,
            "effective_effort_level": upgraded,
            "upgraded_from": previous_effort,
        }
        self._emit_notice(
            generation_id=str(generation["id"]),
            execution_id=execution_id,
            fencing_token=fencing_token,
            control_version=control_version,
            kind="effort_upgraded",
            detail={"effort_level": upgraded, "from": previous_effort, "to": upgraded},
            generation=generation,
        )
        return EffortUpgradeOutcome(
            upgraded=True,
            fence_lost=False,
            generation=generation,
            budget_meter_snapshot=budget_meter_snapshot,
        )

    def _execute_claimed(
        self,
        *,
        execution_id: str,
        generation_id: str,
        generation: Mapping[str, Any],
        control_version: int,
        fencing_token: int,
    ) -> None:
        checkpoint = self._load_checkpoint(execution_id=execution_id)
        if checkpoint.get("phase") == "provider_reconciled":
            recovered_candidates = checkpoint.get("candidates")
            if isinstance(recovered_candidates, list) and recovered_candidates:
                self._publish(
                    generation=generation,
                    execution_id=execution_id,
                    fencing_token=fencing_token,
                    control_version=control_version,
                    candidates=[
                        dict(item) for item in recovered_candidates if isinstance(item, Mapping)
                    ],
                )
                return
        profile_id = str(generation["retrieval_profile_id"])
        profile_version = str(generation["retrieval_profile_version"])
        candidate_config_versions = self._candidate_config_versions_for_generation(
            generation_id=str(generation["id"])
        )
        if candidate_config_versions is not None:
            # Candidate 0 is the persisted side used by the initial retrieval;
            # the second side is fetched independently during candidate
            # production. The mapping is read-only after pair creation.
            profile_version = candidate_config_versions[0]
        effort = str(generation["effective_effort_level"])
        policy = effort_policy(effort)
        budget = GenerationBudget.from_checkpoint(
            effort,
            checkpoint.get("budget", checkpoint) if checkpoint else None,
        )
        budget_meter_snapshot = None
        if self._budget_meter is not None:
            budget_meter_snapshot = self._budget_meter.ensure_meter(
                generation_id=str(generation["id"]),
                effort_level=effort,
                deadline_at_utc=generation["absolute_deadline_at_utc"],
            )
        rag_budget_meter = self._build_rag_budget_meter(
            effort,
            budget_meter_snapshot,
            _utc(generation["absolute_deadline_at_utc"]),
        )
        conversation_history = self._load_conversation_history(generation=generation)
        recent_queries: tuple[str, ...] = tuple(
            str(message["content"]) for message in conversation_history if message["role"] == "user"
        )
        tool_resume = self._tool_resume_state(checkpoint)
        hits: tuple[RetrievalHitOutcome, ...] = ()
        strategy_operations: tuple[DeepRetrievalStrategy, ...] = ()
        if policy.retrieval_depth == "deep" and tool_resume is None:
            strategy_operations = self._plan_deep_retrieval(
                generation=generation,
                execution_id=execution_id,
                fencing_token=fencing_token,
                control_version=control_version,
                budget=rag_budget_meter,
                history_messages=conversation_history,
            )

        round_index = int(checkpoint.get("round_index", 0)) if checkpoint else 0
        # Deep-tier progress rows: one step index per retrieval round, with the
        # active/done pair sharing that index (frontend contract §3.7).
        step_index = 0
        skip_retrieval_events = bool(checkpoint and checkpoint.get("phase") == "retrieval_complete")
        citations: list[Mapping[str, Any]] = []
        if tool_resume is not None:
            # Crash resume mid-generation: reuse the recorded tool observations
            # and the retrieval context snapshot instead of repeating outbound
            # work; the generation stage continues the interrupted tool loop.
            hits = tool_resume["hits"]
            citations = tool_resume["citations"]
        else:
            while True:
                if _utc(self._now()) >= _utc(generation["absolute_deadline_at_utc"]):
                    # Fail atomically through _fail_execution before any retrieval,
                    # provider or usage side effects of this round.
                    raise PlatformError(
                        "generation_deadline_exceeded",
                        "The generation deadline expired before further execution",
                        {},
                        500,
                    )
                if round_index > 0 and not budget.can_start_rag_round():
                    upgrade = self._try_upgrade_effort(
                        generation=generation,
                        execution_id=execution_id,
                        fencing_token=fencing_token,
                        control_version=control_version,
                        budget=budget,
                        rag_budget_meter=rag_budget_meter,
                        budget_meter_snapshot=budget_meter_snapshot,
                        hits=hits,
                        estimate_query=str(generation["request_content"]),
                    )
                    if upgrade.fence_lost:
                        return
                    if not upgrade.upgraded:
                        break
                    generation = upgrade.generation
                    budget_meter_snapshot = upgrade.budget_meter_snapshot
                    policy = effort_policy(budget.effort_level)
                self._emit_stage(
                    generation_id=generation_id,
                    execution_id=execution_id,
                    fencing_token=fencing_token,
                    control_version=control_version,
                    phase="retrieving" if round_index == 0 else "retrieving_again",
                    generation=generation,
                )
                budget_reservation_id = None
                if self._budget_meter is not None:
                    budget_reservation_id = f"rag:{generation_id}:{round_index}"
                    self._budget_reserve(
                        generation=generation,
                        execution_id=execution_id,
                        fencing_token=fencing_token,
                        control_version=control_version,
                        reservation_id=budget_reservation_id,
                        operation_kind="rag_retrieval",
                        estimated_tokens=0,
                        estimated_cost=Decimal("0"),
                        is_rag=True,
                    )
                result = self._run_retrieval_stage(
                    generation=generation,
                    execution_id=execution_id,
                    fencing_token=fencing_token,
                    control_version=control_version,
                    query=str(generation["request_content"]),
                    budget=budget,
                    rag_budget_meter=rag_budget_meter,
                    budget_meter_snapshot=budget_meter_snapshot,
                    profile_id=profile_id,
                    profile_version=profile_version,
                    reservation_id=budget_reservation_id,
                    step_index=step_index,
                    strategy_operations=strategy_operations if round_index == 0 else (),
                    select_candidates=True,
                    emit_routed_event=not skip_retrieval_events,
                    emit_step_events=policy.emit_step_events,
                    recent_queries=recent_queries,
                )
                step_index = result.step_index
                hits = result.hits
                citations = result.citations
                if len(citations) == len(hits):
                    if not self._persist_checkpoint(
                        generation_id=generation_id,
                        execution_id=execution_id,
                        fencing_token=fencing_token,
                        control_version=control_version,
                        checkpoint={
                            "phase": "retrieval_complete",
                            "round_index": round_index,
                            "completed_operations": [f"retrieval:{round_index}"],
                            "retrieval_scope": {
                                "profile_id": profile_id,
                                "profile_version": profile_version,
                            },
                            "query_config_version": profile_version,
                            "budget": budget.to_checkpoint(),
                        },
                    ):
                        return
                    break
                visible_ids = {
                    (str(item["document_id"]), str(item["chunk_id"])) for item in citations
                }
                hits = tuple(hit for hit in hits if (hit.document_id, hit.chunk_id) in visible_ids)
                if not hits:
                    # Every hit was ACL-filtered between search and citation
                    # resolution: this is a normal business outcome (no_context),
                    # not a generation failure.
                    citations = []
                    break
                round_index += 1
                skip_retrieval_events = False

        # Generation with the bounded self-evaluation rewrite loop (A6):
        # tiers whose policy disables self-evaluation accept the first draft
        # (quick); think/deep re-run retrieval within the same frozen scope
        # only while the tier still has RAG rounds and open budget gates.
        # Rejected drafts never become public.
        effective_query = str(generation["request_content"])
        evaluator: SelfEvaluationPort = (
            self._self_evaluator if policy.self_evaluation else AcceptingSelfEvaluationPort()
        )
        tool_resume_state = tool_resume
        while True:
            candidates = self._run_generation_stage(
                generation=generation,
                execution_id=execution_id,
                fencing_token=fencing_token,
                control_version=control_version,
                candidate_config_versions=candidate_config_versions,
                hits=hits,
                citations=citations,
                rag_budget_meter=rag_budget_meter,
                query=effective_query,
                history_messages=conversation_history,
                recent_queries=recent_queries,
                policy=policy,
                resume_state=tool_resume_state,
            ).candidates
            tool_resume_state = None
            evaluation = self._run_self_evaluation_stage(
                evaluator=evaluator,
                query=effective_query,
                candidates=candidates,
                citations=citations,
                hits=hits,
            )
            if evaluation.accepted:
                break
            if _utc(self._now()) >= _utc(generation["absolute_deadline_at_utc"]):
                self._complete_deferred_provider_calls_public(candidates)
                break
            if not budget.can_start_rag_round():
                upgrade = self._try_upgrade_effort(
                    generation=generation,
                    execution_id=execution_id,
                    fencing_token=fencing_token,
                    control_version=control_version,
                    budget=budget,
                    rag_budget_meter=rag_budget_meter,
                    budget_meter_snapshot=budget_meter_snapshot,
                    hits=hits,
                    estimate_query=effective_query,
                )
                if not upgrade.upgraded:
                    self._complete_deferred_provider_calls_public(candidates)
                    if upgrade.fence_lost:
                        return
                    break
                generation = upgrade.generation
                budget_meter_snapshot = upgrade.budget_meter_snapshot
                policy = effort_policy(budget.effort_level)
            # The current draft is discarded before the rewritten query is
            # retrieved; settle its provider calls independently of publish.
            self._complete_deferred_provider_calls_public(candidates)
            rewrite_reservation_id = None
            if self._budget_meter is not None:
                rewrite_reservation_id = f"rag:{generation_id}:rewrite-{budget.rag_calls_used}"
                self._budget_reserve(
                    generation=generation,
                    execution_id=execution_id,
                    fencing_token=fencing_token,
                    control_version=control_version,
                    reservation_id=rewrite_reservation_id,
                    operation_kind="rag_rewrite",
                    estimated_tokens=0,
                    estimated_cost=Decimal("0"),
                    is_rag=True,
                )
            self._emit_stage(
                generation_id=generation_id,
                execution_id=execution_id,
                fencing_token=fencing_token,
                control_version=control_version,
                phase="rewriting",
                generation=generation,
            )
            effective_query = str(evaluation.rewritten_query)
            result = self._run_retrieval_stage(
                generation=generation,
                execution_id=execution_id,
                fencing_token=fencing_token,
                control_version=control_version,
                query=effective_query,
                budget=budget,
                rag_budget_meter=rag_budget_meter,
                budget_meter_snapshot=budget_meter_snapshot,
                profile_id=profile_id,
                profile_version=profile_version,
                reservation_id=rewrite_reservation_id,
                step_index=step_index,
                strategy_operations=(),
                select_candidates=False,
                emit_routed_event=False,
                emit_step_events=policy.emit_step_events,
                recent_queries=recent_queries,
            )
            step_index = result.step_index
            hits = result.hits
            citations = result.citations
            if len(citations) != len(hits):
                visible_ids = {
                    (str(item["document_id"]), str(item["chunk_id"])) for item in citations
                }
                hits = tuple(hit for hit in hits if (hit.document_id, hit.chunk_id) in visible_ids)
                if not hits:
                    citations = []
            if not self._persist_checkpoint(
                generation_id=generation_id,
                execution_id=execution_id,
                fencing_token=fencing_token,
                control_version=control_version,
                checkpoint={
                    "phase": "retrieval_complete",
                    "round_index": budget.rag_calls_used,
                    "completed_operations": [f"rewrite:{budget.rag_calls_used}"],
                    "retrieval_scope": {
                        "profile_id": profile_id,
                        "profile_version": profile_version,
                    },
                    "query_config_version": profile_version,
                    "budget": budget.to_checkpoint(),
                },
            ):
                return
        try:
            checkpoint_persisted = self._persist_checkpoint(
                generation_id=generation_id,
                execution_id=execution_id,
                fencing_token=fencing_token,
                control_version=control_version,
                checkpoint={
                    "phase": "generation_ready",
                    "round_index": budget.rag_calls_used,
                    "completed_operations": ["generation_candidates_ready"],
                    "query_config_version": profile_version,
                    "budget": budget.to_checkpoint(),
                },
            )
        except Exception:
            self._complete_deferred_provider_calls_public(candidates)
            raise
        if not checkpoint_persisted:
            self._complete_deferred_provider_calls_public(candidates)
            return
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

    def _load_checkpoint(self, *, execution_id: str) -> dict[str, Any]:
        with self._engine.connect() as connection:
            value = connection.execute(
                select(chat_generation_execution_table.c.checkpoint_json).where(
                    chat_generation_execution_table.c.execution_id == execution_id
                )
            ).scalar_one_or_none()
        return dict(value) if isinstance(value, Mapping) else {}

    def _persist_checkpoint(
        self,
        *,
        generation_id: str,
        execution_id: str,
        fencing_token: int,
        control_version: int,
        checkpoint: Mapping[str, Any],
    ) -> bool:
        """Persist one fenced, monotonic stage checkpoint."""

        with self._engine.begin() as connection:
            now = self._now(connection)
            existing_checkpoint = connection.execute(
                select(chat_generation_execution_table.c.checkpoint_json).where(
                    chat_generation_execution_table.c.execution_id == execution_id
                )
            ).scalar_one_or_none()
            merged_checkpoint = dict(checkpoint)
            if (
                isinstance(existing_checkpoint, Mapping)
                and "scope_retry_count" in existing_checkpoint
            ):
                merged_checkpoint.setdefault(
                    "scope_retry_count", int(existing_checkpoint["scope_retry_count"])
                )
            current_generation = (
                select(chat_generation_table.c.id)
                .where(
                    chat_generation_table.c.id == generation_id,
                    chat_generation_table.c.control_version == control_version,
                    chat_generation_table.c.status == "running",
                )
                .exists()
            )
            updated = connection.execute(
                update(chat_generation_execution_table)
                .where(
                    chat_generation_execution_table.c.execution_id == execution_id,
                    chat_generation_execution_table.c.generation_id == generation_id,
                    chat_generation_execution_table.c.status == "running",
                    chat_generation_execution_table.c.fencing_token == fencing_token,
                    current_generation,
                )
                .values(
                    checkpoint_version=chat_generation_execution_table.c.checkpoint_version + 1,
                    checkpoint_json=merged_checkpoint,
                    updated_at_utc=now,
                )
            ).rowcount
        return updated == 1

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

    def _load_conversation_history(
        self, *, generation: Mapping[str, Any]
    ) -> tuple[dict[str, str], ...]:
        """Load the conversation's latest turns in chronological order."""

        conversation_id = str(generation.get("conversation_id") or "")
        if not conversation_id:
            return ()
        excluded_ids = [
            str(generation.get(key))
            for key in ("user_message_id", "message_id")
            if generation.get(key)
        ]
        with self._engine.connect() as connection:
            rows = connection.execute(
                select(chat_message_table.c.role, chat_message_table.c.content)
                .where(
                    chat_message_table.c.conversation_id == conversation_id,
                    chat_message_table.c.role.in_(("user", "assistant")),
                    chat_message_table.c.content != "",
                    chat_message_table.c.id.not_in(excluded_ids),
                )
                .order_by(
                    # Newest first; within one identical timestamp (one ask
                    # writes the user question and the assistant message with
                    # the same ``now``) the assistant row precedes the user
                    # row so the reversed history reads question → answer.
                    chat_message_table.c.created_at_utc.desc(),
                    chat_message_table.c.role.asc(),
                    chat_message_table.c.id.desc(),
                )
                .limit(CONVERSATION_HISTORY_TURNS * 2)
            ).fetchall()
        history: list[dict[str, str]] = []
        for row in reversed(rows):
            content = str(row.content)
            if row.role == "assistant":
                content = content[:ASSISTANT_HISTORY_DIGEST_CHARS]
            history.append({"role": str(row.role), "content": content})
        return tuple(history)

    _TOOL_DEFINITIONS: dict[str, dict[str, Any]] = {
        "search_retrieval": {
            "type": "function",
            "function": {
                "name": "search_retrieval",
                "description": "混合检索知识库文档，返回与 query 最相关的文档片段（含树检索/图谱路由，由检索配置决定）。",
                "parameters": {
                    "type": "object",
                    "properties": {"query": {"type": "string", "description": "检索查询"}},
                    "required": ["query"],
                },
            },
        },
        "search_tree": {
            "type": "function",
            "function": {
                "name": "search_tree",
                "description": "按树检索策略深入知识库，适合需要定位章节/子块的多跳问题。",
                "parameters": {
                    "type": "object",
                    "properties": {"query": {"type": "string", "description": "检索查询"}},
                    "required": ["query"],
                },
            },
        },
        "search_graph": {
            "type": "function",
            "function": {
                "name": "search_graph",
                "description": "沿知识图谱关系检索，适合实体间关联问题。",
                "parameters": {
                    "type": "object",
                    "properties": {"query": {"type": "string", "description": "检索查询"}},
                    "required": ["query"],
                },
            },
        },
    }

    def _tool_definitions(self, allowed_tools: tuple[str, ...]) -> tuple[dict[str, Any], ...]:
        return tuple(
            dict(self._TOOL_DEFINITIONS[name])
            for name in allowed_tools
            if name in self._TOOL_DEFINITIONS
        )

    def _execute_tool(
        self,
        *,
        name: str,
        arguments: str,
        generation: Mapping[str, Any],
        execution_id: str,
        fencing_token: int,
        control_version: int,
        rag_budget_meter: BudgetMeter | None,
        step_sequence: int,
        profile_version: str,
    ) -> str:
        """Execute one model-requested retrieval tool; return its observation."""

        try:
            arguments_data = json.loads(arguments) if arguments.strip() else {}
        except ValueError:
            arguments_data = {}
        query = ""
        if isinstance(arguments_data, Mapping):
            query = str(arguments_data.get("query") or "").strip()
        if not query:
            query = str(generation["request_content"])
        strategy: tuple[DeepRetrievalStrategy, ...] = ("tree",) if name == "search_tree" else ()
        route_graph = name == "search_graph"
        reservation_id = f"rag:{generation['id']}:tool-{step_sequence}"
        if self._budget_meter is not None:
            self._budget_reserve(
                generation=generation,
                execution_id=execution_id,
                fencing_token=fencing_token,
                control_version=control_version,
                reservation_id=reservation_id,
                operation_kind="rag_retrieval",
                estimated_tokens=0,
                estimated_cost=Decimal("0"),
                is_rag=True,
            )
        outcome = self._tool_retrieval.search(
            query,
            principal=_principal_from_generation(generation),
            narrowing_scope=generation["request_scope_json"],
            profile_id=str(generation["retrieval_profile_id"]),
            profile_version=profile_version,
            effort=str(generation["effective_effort_level"]),
            budget=rag_budget_meter,
            strategy_operations=strategy,
            recent_queries=(),
            route_graph=route_graph,
        )
        if self._budget_meter is not None:
            self._budget_meter.settle(
                generation_id=str(generation["id"]),
                reservation_id=reservation_id,
                actual_tokens=0,
                actual_cost=Decimal("0"),
            )
        degradations = [dict(item) for item in outcome.degradations]
        for item in degradations:
            code = str(item.get("code") or "")
            kind = code if code in NOTICE_KINDS else "retrieval_degraded"
            self._emit_notice(
                generation_id=str(generation["id"]),
                execution_id=execution_id,
                fencing_token=fencing_token,
                control_version=control_version,
                kind=kind,
                detail=dict(item),
                generation=generation,
            )
        documents = [
            {
                "document_id": hit.document_id,
                "document_version_id": hit.document_version_id,
                "snippet": hit.snippet[:300] if hit.snippet else "",
            }
            for hit in outcome.hits[:5]
        ]
        return json.dumps(
            {"query": query, "documents": documents, "degradations": degradations},
            ensure_ascii=True,
        )

    def _tool_resume_state(self, checkpoint: Mapping[str, Any]) -> dict[str, Any] | None:
        """Restore executed tool observations after a crash mid-generation."""

        if not checkpoint or checkpoint.get("phase") != "tool_observed":
            return None
        if checkpoint.get("candidate") not in (None, 0):
            # A/B candidate 1 is not resumable; fall back to a fresh attempt
            # with the pre-change re-execution semantics.
            return None
        hits = tuple(
            RetrievalHitOutcome(
                document_id=str(item.get("document_id") or ""),
                document_version_id=str(item.get("document_version_id") or ""),
                publication_id=str(item.get("publication_id") or ""),
                chunk_id=str(item.get("chunk_id") or ""),
                space_id=str(item.get("space_id") or ""),
                locator=dict(item.get("locator") or {}),
                snippet=str(item.get("snippet") or ""),
                library=str(item.get("library") or "unknown"),
            )
            for item in checkpoint.get("hits", ())
            if isinstance(item, Mapping)
        )
        return {
            "hits": hits,
            "citations": [
                dict(item) for item in checkpoint.get("citations", ()) if isinstance(item, Mapping)
            ],
            "observations": [
                dict(item)
                for item in checkpoint.get("observations", ())
                if isinstance(item, Mapping)
            ],
            "followup_messages": tuple(
                dict(item)
                for item in checkpoint.get("followup_messages", ())
                if isinstance(item, Mapping)
            ),
        }

    def _plan_deep_retrieval(
        self,
        *,
        generation: Mapping[str, Any],
        execution_id: str,
        fencing_token: int,
        control_version: int,
        budget: BudgetMeter,
        history_messages: tuple[Mapping[str, Any], ...] = (),
    ) -> tuple[DeepRetrievalStrategy, ...]:
        """Use the main chat model transport for one validated deep strategy plan."""

        request = ChatProviderRequest(
            generation_id=str(generation["id"]),
            owner_user_id=str(generation["owner_user_id"]),
            content=str(generation["request_content"]),
            effort_level="deep",
            candidate=None,
            context_items=(),
            source_conflict_contract=source_conflict_contract(),
            history_messages=history_messages,
            enable_thinking=True,
            purpose="deep_retrieval_plan",
        )
        try:
            response = self._provider_call(
                request,
                generation=generation,
                execution_id=execution_id,
                fencing_token=fencing_token,
                control_version=control_version,
                logical_budget=budget,
            )
            return DeepRetrievalStrategyPlan.from_model_content(response.content).operations
        except ValueError:
            reason = "strategy_plan_invalid"
        except PlatformError as error:
            if error.code == "provider_result_unknown":
                raise
            reason = "strategy_plan_unavailable"
        self._emit_notice(
            generation_id=str(generation["id"]),
            execution_id=execution_id,
            fencing_token=fencing_token,
            control_version=control_version,
            kind="retrieval_degraded",
            detail={"reason": reason},
            generation=generation,
        )
        return ()

    def _produce_candidates(
        self,
        *,
        generation: Mapping[str, Any],
        execution_id: str,
        fencing_token: int,
        control_version: int,
        hits: tuple[RetrievalHitOutcome, ...],
        citations: list[Mapping[str, Any]],
        candidate_config_versions: tuple[str, str] | None = None,
        retrieval_budget: Any | None = None,
        query: str | None = None,
        logical_budget: BudgetMeter | None = None,
        on_delta: Callable[[str], None] | None = None,
        history_messages: tuple[Mapping[str, Any], ...] = (),
        recent_queries: Sequence[str] = (),
        policy: EffortPolicy | None = None,
        resume_state: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        pair = self._pair_for_generation(generation_id=str(generation["id"]))
        candidate_numbers = (0, 1) if pair is not None else (0,)
        results: list[dict[str, Any]] = []
        for candidate in candidate_numbers:
            candidate_hits = hits
            candidate_citations = citations
            if candidate == 1 and candidate_config_versions is not None:
                outcome = self._retrieval.search(
                    str(query if query is not None else ""),
                    principal=_principal_from_generation(generation),
                    narrowing_scope=generation["request_scope_json"],
                    profile_id="default",
                    profile_version=candidate_config_versions[1],
                    effort=str(generation["effective_effort_level"]),
                    budget=retrieval_budget,
                    recent_queries=recent_queries,
                )
                candidate_hits = outcome.hits
                candidate_citations = self._resolve_citations(candidate_hits, generation)
                for item in outcome.degradations:
                    code = str(item.get("code") or "")
                    kind = code if code in NOTICE_KINDS else "retrieval_degraded"
                    self._emit_notice(
                        generation_id=str(generation["id"]),
                        execution_id=execution_id,
                        fencing_token=fencing_token,
                        control_version=control_version,
                        kind=kind,
                        detail=dict(item),
                        generation=generation,
                    )
            context = tuple(
                {
                    "document_id": hit.document_id,
                    "document_version_id": hit.document_version_id,
                    "publication_id": hit.publication_id,
                    "chunk_id": hit.chunk_id,
                    "space_id": hit.space_id,
                    "library": hit.library,
                    "locator": dict(hit.locator),
                    "snippet": hit.snippet,
                    "claim_contract": {
                        "annotate": ["library", "space_id"],
                        "conflicts": "state_each_claim_separately_with_own_citation",
                    },
                }
                for hit in candidate_hits
            )
            self._emit_stage(
                generation_id=str(generation["id"]),
                execution_id=execution_id,
                fencing_token=fencing_token,
                control_version=control_version,
                phase="generating",
                generation=generation,
            )
            policy_value = policy or effort_policy(str(generation["effective_effort_level"]))
            tool_profile_version = (
                candidate_config_versions[0]
                if candidate_config_versions is not None
                else str(generation["retrieval_profile_version"])
            )
            request = ChatProviderRequest(
                generation_id=str(generation["id"]),
                owner_user_id=str(generation["owner_user_id"]),
                content=query if query is not None else str(generation["request_content"]),
                effort_level=str(generation["effective_effort_level"]),
                candidate=None if pair is None else candidate,
                context_items=context,
                source_conflict_contract=source_conflict_contract(),
                history_messages=history_messages,
                on_delta=on_delta,
            )
            tools = self._tool_definitions(policy_value.allowed_tools)
            followup_messages: list[dict[str, Any]] = list(
                (resume_state or {}).get("followup_messages", ())
            )
            observations: list[dict[str, Any]] = list((resume_state or {}).get("observations", []))
            step = len(observations)
            deadline = _utc(generation["absolute_deadline_at_utc"])
            while True:
                if _utc(self._now()) >= deadline:
                    raise PlatformError(
                        "generation_deadline_exceeded",
                        "The generation deadline expired before the provider call",
                        {},
                        500,
                    )
                offer_tools = bool(tools) and step < policy_value.max_model_steps - 1
                step_request = replace(
                    request,
                    tools=tools if offer_tools else (),
                    enable_thinking=policy_value.enable_thinking,
                    followup_messages=tuple(followup_messages),
                )
                answer_mode = _answer_mode(candidate_hits, candidate_citations)
                pending_checkpoint = {
                    "phase": "provider_pending",
                    "pending_candidate": {
                        "candidate": candidate,
                        "content": "",
                        "citations": [dict(item) for item in candidate_citations],
                        "answer_mode": answer_mode,
                    },
                    "observations": observations,
                }
                try:
                    response = self._provider_call(
                        step_request,
                        generation=generation,
                        execution_id=execution_id,
                        fencing_token=fencing_token,
                        control_version=control_version,
                        defer_completion=True,
                        pending_checkpoint=pending_checkpoint,
                        logical_budget=logical_budget,
                        call_sequence=step,
                    )
                except Exception:
                    # Earlier candidates are known results even if a later
                    # candidate fails or becomes unknown.
                    self._complete_deferred_provider_calls_public(results)
                    raise
                provider_meta: dict[str, Any] = {}
                if isinstance(response, ProviderCallOutcome):
                    provider_response = response.response
                    provider_meta = {
                        "_provider_call_id": response.provider_call_id,
                        "_provider_measurement": response.measurement,
                        "_provider_ownership": response.ownership,
                        "_provider_started_at_utc": response.started_at_utc,
                        "_provider_request_id": response.provider_request_id,
                    }
                else:
                    provider_response = response
                calls = provider_response.tool_calls
                if not calls or not offer_tools:
                    if not str(provider_response.content or "").strip():
                        # A model that still answers empty after its final step
                        # would previously have been rejected by the transport.
                        raise PlatformError(
                            "provider_unavailable",
                            "Chat model transport returned an empty answer",
                            {"retryable": True},
                            503,
                            True,
                        )
                    break
                # Intermediate tool-step calls are final usage facts: settle
                # them into the ledger now; only the last call stays deferred
                # and completes with the published candidate.
                if isinstance(response, ProviderCallOutcome):
                    self._usage.complete_provider_call(
                        provider_call_id=response.provider_call_id,
                        measurement=response.measurement,
                        ownership=response.ownership,
                        result="succeeded",
                        provider_request_id=response.provider_request_id,
                        started_at_utc=response.started_at_utc,
                    )
                # ReAct continuation: execute the requested tools, record the
                # observations durably, then continue the model conversation.
                step += 1
                assistant_turn: dict[str, Any] = {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [dict(call) for call in calls],
                }
                tool_turns: list[dict[str, Any]] = []
                for call in calls:
                    name = str(call.get("name") or "")
                    observation = self._execute_tool(
                        name=name,
                        arguments=str(call.get("arguments") or ""),
                        generation=generation,
                        execution_id=execution_id,
                        fencing_token=fencing_token,
                        control_version=control_version,
                        rag_budget_meter=logical_budget or retrieval_budget,
                        step_sequence=step,
                        profile_version=tool_profile_version,
                    )
                    observations.append(
                        {
                            "tool": name,
                            "arguments": call.get("arguments"),
                            "observation": observation,
                        }
                    )
                    tool_turns.append(
                        {
                            "role": "tool",
                            "tool_call_id": str(call.get("id") or ""),
                            "content": observation,
                        }
                    )
                followup_messages.append(assistant_turn)
                followup_messages.extend(tool_turns)
                if policy_value.emit_step_events:
                    for position in range(1, len(tool_turns) + 1):
                        self._emit_step(
                            generation_id=str(generation["id"]),
                            execution_id=execution_id,
                            fencing_token=fencing_token,
                            control_version=control_version,
                            index=step * 10 + position,
                            label=f"tool_{position}_step_{step}",
                            state="done",
                        )
                if not self._persist_checkpoint(
                    generation_id=str(generation["id"]),
                    execution_id=execution_id,
                    fencing_token=fencing_token,
                    control_version=control_version,
                    checkpoint={
                        "phase": "tool_observed",
                        "candidate": candidate,
                        "observations": observations,
                        "followup_messages": followup_messages,
                        "hits": [_hit_mapping(hit) for hit in candidate_hits],
                        "citations": [dict(item) for item in candidate_citations],
                        "budget": (
                            logical_budget.to_checkpoint()
                            if isinstance(logical_budget, BudgetMeter)
                            else None
                        ),
                    },
                ):
                    raise PlatformError(
                        "generation_state_conflict",
                        "The generation control fence was invalidated",
                        {},
                        409,
                    )
            content = str(provider_response.content)
            # Citations carry only the hits the model actually referenced in
            # its content (A4); the answer mode follows the referenced subset.
            referenced_citations = _referenced_citations(content, candidate_citations)
            answer_mode = _answer_mode(candidate_hits, candidate_citations, referenced_citations)
            results.append(
                {
                    "candidate": candidate,
                    "content": content,
                    "citations": referenced_citations,
                    "answer_mode": answer_mode,
                    **provider_meta,
                }
            )
        return results

    def _budget_reserve(
        self,
        *,
        generation: Mapping[str, Any],
        execution_id: str,
        fencing_token: int,
        control_version: int,
        reservation_id: str,
        operation_kind: str,
        estimated_tokens: int,
        estimated_cost: Decimal,
        is_rag: bool,
    ) -> None:
        if self._budget_meter is None:
            return
        try:
            self._budget_meter.reserve(
                generation_id=str(generation["id"]),
                reservation_id=reservation_id,
                operation_kind=operation_kind,
                estimated_tokens=estimated_tokens,
                estimated_cost=estimated_cost,
                is_rag=is_rag,
                request_fingerprint=reservation_id,
            )
        except PlatformError as error:
            if error.code in {"budget_exhausted", "cost_unavailable"}:
                self._emit_notice(
                    generation_id=str(generation["id"]),
                    execution_id=execution_id,
                    fencing_token=fencing_token,
                    control_version=control_version,
                    kind="retrieval_degraded",
                    detail={"reason": error.code},
                    generation=generation,
                )
            raise

    def _estimated_provider_tokens(self, request: ChatProviderRequest) -> int:
        snippets = [
            str(item.get("snippet") or "")
            for item in request.context_items
            if isinstance(item, Mapping)
        ]
        snippets.extend(
            str(message.get("content") or "")
            for message in request.history_messages
            if isinstance(message, Mapping)
        )
        return conservative_chat_token_estimate(request.content, snippets)

    def _provider_call(
        self,
        request: ChatProviderRequest,
        *,
        generation: Mapping[str, Any],
        execution_id: str,
        fencing_token: int,
        control_version: int,
        defer_completion: bool = False,
        pending_checkpoint: Mapping[str, Any] | None = None,
        logical_budget: BudgetMeter | None = None,
        call_sequence: int = 0,
    ) -> Any:
        budget_reservation_id = None
        estimated_cost = Decimal("0")
        if self._budget_meter is not None:
            candidate_key = request.candidate if request.candidate is not None else 0
            budget_reservation_id = (
                f"provider:{request.generation_id}:{execution_id}:"
                f"{request.purpose}:{candidate_key}:s{call_sequence}"
            )
            estimated_tokens = self._estimated_provider_tokens(request)
            estimated_cost = self._budget_meter.estimate_cost("chat_generation", estimated_tokens)
            self._budget_reserve(
                generation=generation,
                execution_id=execution_id,
                fencing_token=fencing_token,
                control_version=control_version,
                reservation_id=budget_reservation_id,
                operation_kind="chat_generation",
                estimated_tokens=estimated_tokens,
                estimated_cost=estimated_cost,
                is_rag=False,
            )
        logical_reservation = None
        if logical_budget is not None:
            logical_reservation = logical_budget.reserve(
                "chat_generation",
                estimated_tokens=self._estimated_provider_tokens(request),
                now=self._now(),
            )
        call_id = self._usage.prepare_provider_call(
            provider="chat",
            model="chat-model",
            operation="chat_generation",
            execution_kind="chat_generation",
            execution_id=execution_id,
            generation_id=str(generation["id"]),
            deadline_utc=generation["absolute_deadline_at_utc"],
            request_fingerprint=(
                f"chat:{generation['id']}:{execution_id}:{request.purpose}:{request.candidate or 0}"
            ),
        )
        started_at = self._now()
        if not self._usage.mark_dispatching(call_id, started_at_provider=started_at):
            raise PlatformError(
                "provider_dispatch_failed",
                "The chat provider call could not be dispatched",
                {},
                503,
            )
        owner_user_id = str(generation["owner_user_id"])
        source_space_ids = tuple(
            str(space_id) for space_id in (generation.get("source_space_ids_json") or ())
        )
        space_id = source_space_ids[0] if len(source_space_ids) == 1 else None
        if space_id == "public":
            space_kind = "public"
            space_owner_user_id = None
        elif space_id is not None and space_id.startswith("department:"):
            space_kind = "department"
            space_owner_user_id = None
        elif space_id is not None and space_id.startswith("personal:"):
            space_kind = "personal"
            space_owner_user_id = owner_user_id
        else:
            space_kind = None
            space_owner_user_id = None
        quota_subject_user_id = str(generation.get("quota_subject_user_id") or owner_user_id)
        ownership = OwnershipSnapshot(
            actor_user_id=owner_user_id,
            actor_role_snapshot=str(generation.get("actor_role_snapshot") or "user"),
            actor_department_id_snapshot=generation.get("actor_department_id_snapshot"),
            quota_subject_user_id=quota_subject_user_id,
            cost_center_key=str(
                generation.get("cost_center_key") or f"user:{quota_subject_user_id}"
            ),
            space_id=space_id,
            space_kind=space_kind,
            space_owner_user_id=space_owner_user_id,
            fence_token=fencing_token,
            source_space_ids=source_space_ids,
        )
        try:
            response = self._provider.generate(request)
        except PlatformError as error:
            if logical_budget is not None and logical_reservation is not None:
                logical_budget.reconcile(logical_reservation, actual_tokens=0)
            if self._budget_meter is not None:
                self._budget_meter.mark_unknown(
                    generation_id=str(generation["id"]),
                    reservation_id=budget_reservation_id,
                )
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
            if pending_checkpoint is not None:
                self._persist_checkpoint(
                    generation_id=str(generation["id"]),
                    execution_id=execution_id,
                    fencing_token=fencing_token,
                    control_version=control_version,
                    checkpoint={
                        **dict(pending_checkpoint),
                        "provider_call_id": call_id,
                    },
                )
            if self._budget_meter is not None:
                self._budget_meter.mark_unknown(
                    generation_id=str(generation["id"]),
                    reservation_id=budget_reservation_id,
                )
            if logical_budget is not None and logical_reservation is not None:
                logical_budget.reconcile(logical_reservation, actual_tokens=0)
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
            measurement_sources={
                field: "provider_reported"
                for field in ("input_tokens", "output_tokens", "reasoning_tokens")
                if getattr(response, field, None) is not None
            },
        )
        provider_request_id = getattr(response, "provider_request_id", None)
        actual_tokens = (
            int(getattr(response, "input_tokens", 0) or 0)
            + int(getattr(response, "output_tokens", 0) or 0)
            + int(getattr(response, "reasoning_tokens", 0) or 0)
        )
        if logical_budget is not None and logical_reservation is not None:
            logical_budget.reconcile(logical_reservation, actual_tokens=actual_tokens)
        if defer_completion:
            if self._budget_meter is not None:
                self._budget_meter.settle(
                    generation_id=str(generation["id"]),
                    reservation_id=budget_reservation_id,
                    actual_tokens=actual_tokens,
                    actual_cost=self._budget_meter.estimate_cost("chat_generation", actual_tokens),
                )
            return ProviderCallOutcome(
                response=response,
                provider_call_id=call_id,
                measurement=measurement,
                ownership=ownership,
                started_at_utc=started_at,
                provider_request_id=provider_request_id,
            )
        self._usage.complete_provider_call(
            provider_call_id=call_id,
            measurement=measurement,
            ownership=ownership,
            result="succeeded",
            provider_request_id=provider_request_id,
            started_at_utc=started_at,
        )
        if self._budget_meter is not None:
            self._budget_meter.settle(
                generation_id=str(generation["id"]),
                reservation_id=budget_reservation_id,
                actual_tokens=actual_tokens,
                actual_cost=self._budget_meter.estimate_cost("chat_generation", actual_tokens),
            )
        return response

    def _complete_deferred_provider_call_in_transaction(
        self, connection: Connection, item: Mapping[str, Any]
    ) -> None:
        call_id = item.get("_provider_call_id")
        if not call_id:
            return
        method = getattr(self._usage, "complete_provider_call_in_transaction", None)
        kwargs = {
            "provider_call_id": str(call_id),
            "measurement": item["_provider_measurement"],
            "ownership": item["_provider_ownership"],
            "result": "succeeded",
            "provider_request_id": item.get("_provider_request_id"),
            "started_at_utc": item.get("_provider_started_at_utc"),
        }
        if callable(method):
            method(connection, **kwargs)
        else:
            # Test adapters predating the connection-aware port remain usable;
            # production runtime always supplies the ledger-backed method.
            self._usage.complete_provider_call(**kwargs)

    def _complete_deferred_provider_calls_public(
        self, candidates: Sequence[Mapping[str, Any]]
    ) -> None:
        for item in candidates:
            call_id = item.get("_provider_call_id")
            if not call_id:
                continue
            self._usage.complete_provider_call(
                provider_call_id=str(call_id),
                measurement=item["_provider_measurement"],
                ownership=item["_provider_ownership"],
                result="succeeded",
                provider_request_id=item.get("_provider_request_id"),
                started_at_utc=item.get("_provider_started_at_utc"),
            )

    def _source_scope_is_current(
        self, generation: Mapping[str, Any], candidates: Sequence[Mapping[str, Any]]
    ) -> bool:
        citations: list[Mapping[str, Any]] = []
        seen: set[tuple[str, str, str, str]] = set()
        for item in candidates:
            for citation in item.get("citations", ()) or ():
                if not isinstance(citation, Mapping):
                    continue
                key = _source_identity(citation)
                if key in seen:
                    continue
                seen.add(key)
                citations.append(citation)
        if not citations:
            return True
        method = getattr(self._retrieval, "revalidate_citations", None)
        if not callable(method):
            return True
        try:
            current = method(tuple(citations), principal=_principal_from_generation(generation))
        except PlatformError:
            return False
        expected_ids = {_source_identity(item) for item in citations}
        current_ids = {_source_identity(item) for item in current if isinstance(item, Mapping)}
        return current_ids == expected_ids

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
        stale_publication = False
        try:
            with self._engine.begin() as connection:
                now = self._now(connection)
                if not self._fence_current(
                    connection,
                    generation_id=str(generation["id"]),
                    execution_id=execution_id,
                    fencing_token=fencing_token,
                    control_version=control_version,
                ):
                    stale_publication = True
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
                    stale_publication = True
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
                    stale_publication = True
                    self._cancel_stale_execution(
                        connection,
                        execution_id=execution_id,
                        generation_id=str(generation["id"]),
                        now=now,
                    )
                    return

                # Revalidate after the execution/generation fence is current,
                # but before any provider ledger or public chat writes commit.
                if not self._source_scope_is_current(generation, candidates):
                    raise PlatformError(
                        "source_scope_changed",
                        "The retrieval source scope changed before publication",
                        {"generation_id": str(generation["id"])},
                        409,
                        True,
                    )

                pair = self._pair_row(connection, generation_id=str(generation["id"]))
                for item in candidates:
                    self._complete_deferred_provider_call_in_transaction(connection, item)
                ab_open = pair is not None and len(candidates) == 2
                near_duplicate = bool(
                    ab_open
                    and _rouge_l(candidates[0]["content"], candidates[1]["content"])
                    >= AB_NEAR_DUPLICATE_ROUGE_L
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
                    .values(status="completed", checkpoint_json=None, updated_at_utc=now)
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
        except PlatformError as error:
            if error.code == "source_scope_changed":
                # Provider work has already happened, so preserve its accounting
                # even though the answer is rejected by the publication fence.
                self._complete_deferred_provider_calls_public(candidates)
            raise
        finally:
            if stale_publication:
                # Commit the stale execution cancellation first; the public
                # wrapper then records only the provider ledger rows.
                self._complete_deferred_provider_calls_public(candidates)

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
            if error.code == "provider_result_unknown":
                connection.execute(
                    update(chat_generation_execution_table)
                    .where(
                        chat_generation_execution_table.c.execution_id == execution_id,
                        chat_generation_execution_table.c.generation_id == generation_id,
                        chat_generation_execution_table.c.status == "running",
                    )
                    .values(
                        status="provider_reconciling",
                        last_error_classification=error.code,
                        updated_at_utc=now,
                    )
                )
                return
            if error.code == "source_scope_changed":
                execution_row = connection.execute(
                    select(chat_generation_execution_table.c.checkpoint_json).where(
                        chat_generation_execution_table.c.execution_id == execution_id,
                        chat_generation_execution_table.c.generation_id == generation_id,
                    )
                ).scalar_one_or_none()
                checkpoint = dict(execution_row) if isinstance(execution_row, Mapping) else {}
                retries = int(checkpoint.get("scope_retry_count", 0))
                if retries < self._max_scope_retries:
                    connection.execute(
                        update(chat_generation_execution_table)
                        .where(
                            chat_generation_execution_table.c.execution_id == execution_id,
                            chat_generation_execution_table.c.status == "running",
                        )
                        .values(
                            status="retry_wait",
                            lease_owner=None,
                            lease_expires_at_utc=None,
                            next_attempt_at_utc=now,
                            checkpoint_json={
                                **checkpoint,
                                "scope_retry_count": retries + 1,
                            },
                            last_error_classification=error.code,
                            updated_at_utc=now,
                        )
                    )
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
                    checkpoint_json=None,
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
                    "request_id": str(generation.get("request_id") or "req_system"),
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

    def _open_delta_sink(
        self,
        *,
        generation: Mapping[str, Any],
        execution_id: str,
        fencing_token: int,
        control_version: int,
        candidate_config_versions: tuple[str, str] | None,
        enabled: bool = True,
    ) -> _DeltaStreamSink | None:
        """Streaming pass-through only where the draft is the published answer.

        The effort policy decides which tiers stream (quick: yes; think/deep
        drafts can be discarded by the rewrite loop, so they keep the buffered
        answer path), and A/B candidates must stay blind before publication.
        """

        if not enabled:
            return None
        if candidate_config_versions is not None:
            return None
        return _DeltaStreamSink(
            emit=lambda text: self._emit_delta(
                generation_id=str(generation["id"]),
                execution_id=execution_id,
                fencing_token=fencing_token,
                control_version=control_version,
                content=text,
            ),
            clock=self._now,
        )

    def _emit_delta(
        self,
        *,
        generation_id: str,
        execution_id: str,
        fencing_token: int,
        control_version: int,
        content: str,
    ) -> None:
        """Best-effort progressive rendering: a dropped delta must never fail
        the generation; the answer event stays the single authoritative text."""

        try:
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
                    event_type="delta",
                    data={"candidate": 0, "content": content},
                    now=self._now(connection),
                )
        except Exception:
            _logger.warning(
                "chat delta event dropped generation_id=%s", generation_id, exc_info=True
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
        detail: Mapping[str, Any] | None = None,
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
                data={"phase": phase, **(dict(detail) if detail is not None else {})},
                now=self._now(connection),
            )

    def _emit_step(
        self,
        *,
        generation_id: str,
        execution_id: str,
        fencing_token: int,
        control_version: int,
        index: int,
        label: str,
        state: str,
    ) -> None:
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
                event_type="step",
                data={"index": index, "label": label, "state": state},
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
            # SKIP LOCKED: a row another transaction already locked is not this
            # transaction's candidate (claim must move on, and stop/fail/fence
            # converge through the subsequent conditional updates instead of
            # queueing behind maintenance).
            statement = statement.with_for_update(skip_locked=True)
        row = connection.execute(statement).mappings().one_or_none()
        return None if row is None else dict(row)

    @staticmethod
    def _lock_generation_executions(connection: Connection, *, generation_id: str) -> None:
        """Row-lock every execution of one generation in a stable order.

        Called before the generation row is locked so maintenance holds the
        same execution-then-generation order as claim/stop/fail.
        """

        statement = (
            select(chat_generation_execution_table.c.execution_id)
            .where(chat_generation_execution_table.c.generation_id == generation_id)
            .order_by(chat_generation_execution_table.c.execution_id)
        )
        if connection.dialect.name != "sqlite":
            statement = statement.with_for_update()
        connection.execute(statement).all()

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

    def _candidate_config_versions_for_generation(
        self, *, generation_id: str
    ) -> tuple[str, str] | None:
        """Read the immutable blind-side mapping created with the pair."""

        with self._engine.connect() as connection:
            rows = (
                connection.execute(
                    select(
                        chat_ab_candidate_table.c.candidate,
                        chat_ab_candidate_table.c.candidate_config_version,
                    )
                    .join(
                        chat_ab_pair_table,
                        chat_ab_pair_table.c.pair_id == chat_ab_candidate_table.c.pair_id,
                    )
                    .join(
                        chat_generation_table,
                        chat_generation_table.c.id == chat_ab_pair_table.c.generation_id,
                    )
                    .where(chat_generation_table.c.id == generation_id)
                    .order_by(chat_ab_candidate_table.c.candidate)
                )
                .mappings()
                .all()
            )
        if len(rows) != 2:
            return None
        versions = [row["candidate_config_version"] for row in rows]
        if any(value is None or not str(value).strip() for value in versions):
            return None
        first, second = (str(value) for value in versions)
        if first == second:
            return None
        return first, second

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
        "library": hit.library,
        "locator": dict(hit.locator),
        "snippet": hit.snippet,
    }


def _referenced_citations(
    content: str, citations: Sequence[Mapping[str, Any]]
) -> list[Mapping[str, Any]]:
    """Citations the model content actually references by source marker.

    The generation prompt presents each context block with its opaque
    ``document_id@version_id`` identifier and instructs the model to annotate
    cited claims with it, so an identifier match is the reference signal. Hits
    the model never referenced are not published as citations (A4).
    """

    referenced: list[Mapping[str, Any]] = []
    for citation in citations:
        document_id = str(citation.get("document_id") or "")
        if not document_id:
            continue
        pattern = re.compile(rf"(?<![0-9A-Za-z_]){re.escape(document_id)}(?![0-9A-Za-z_])")
        if pattern.search(content) is not None:
            referenced.append(citation)
    return referenced


def _answer_mode(
    hits: tuple[RetrievalHitOutcome, ...],
    citations: Sequence[Mapping[str, Any]],
    referenced: Sequence[Mapping[str, Any]] | None = None,
) -> str:
    """Design §2.5.2: ``no_context`` on zero hits or fully ACL-filtered hits;
    ``direct`` when hits survived ACL filtering but the model referenced none;
    ``grounded`` when at least one reference survived."""

    if not hits or not citations:
        return "no_context"
    if referenced is None:
        # Provisional pre-provider checkpoint value: the content does not
        # exist yet, so the referenced subset cannot be derived.
        return "grounded"
    return "grounded" if referenced else "direct"


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
