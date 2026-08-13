"""Generation identity transactions, stop/retry, feedback and A/B voting."""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.engine import Connection, Engine

from app.platform.errors import PlatformError

from .budget import RAG_BUDGET_POLICY_VERSION
from .events import append_event
from .models import (
    AB_PAIR_OPEN_SECONDS,
    AbVoteRequest,
    AskRequest,
    CalibrationWindowSnapshot,
    FeedbackRequest,
    canonical_request_fingerprint,
)
from .ports import CalibrationWindowPort, ChatAuthorizationPort
from .schema import (
    chat_ab_candidate_table,
    chat_ab_pair_table,
    chat_ab_vote_table,
    chat_conversation_table,
    chat_generation_execution_table,
    chat_generation_table,
    chat_idempotency_table,
    chat_message_feedback_table,
    chat_message_table,
)

DEFAULT_RETRIEVAL_PROFILE_ID = "default"
DEFAULT_RETRIEVAL_PROFILE_VERSION = "1"
GENERATION_ABSOLUTE_DEADLINE_SECONDS = 1800
DEFAULT_MAX_RUNNING_GENERATIONS_PER_USER = 8
IDEMPOTENCY_KINDS = ("ask", "retry", "feedback", "ab_vote")


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _utc(value: Any) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError("expected a datetime")
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _require_owned_conversation(
    connection: Connection, *, conversation_id: str, user_id: str
) -> Mapping[str, Any]:
    row = (
        connection.execute(
            select(chat_conversation_table).where(chat_conversation_table.c.id == conversation_id)
        )
        .mappings()
        .one_or_none()
    )
    if row is None or str(row["owner_user_id"]) != user_id:
        raise PlatformError("conversation_not_found", "Conversation was not found", {}, 404)
    return dict(row)


def _derive_title(content: str, *, limit: int = 80) -> str:
    collapsed = " ".join(content.split())
    if len(collapsed) <= limit:
        return collapsed
    return f"{collapsed[: limit - 1]}…"


@dataclass(slots=True)
class GenerationCreationResult:
    generation_id: str
    message_id: str
    user_message_id: str
    replay: bool


class GenerationService:
    """Owns conversation-scoped generation identity, mutations and read facts."""

    def __init__(
        self,
        engine: Engine,
        *,
        clock: Any,
        authorization: ChatAuthorizationPort,
        calibration: CalibrationWindowPort,
        sampler: Any = None,
        max_running_per_user: int = DEFAULT_MAX_RUNNING_GENERATIONS_PER_USER,
        absolute_deadline_seconds: int = GENERATION_ABSOLUTE_DEADLINE_SECONDS,
    ) -> None:
        self._engine = engine
        self._clock = clock
        self._authorization = authorization
        self._calibration = calibration
        self._sampler = sampler or _default_sampler
        self._max_running_per_user = max_running_per_user
        self._absolute_deadline_seconds = absolute_deadline_seconds

    def _now(self, connection: Connection) -> datetime:
        value = self._clock.now_utc(connection)
        if value.tzinfo is None:
            value = value.replace(tzinfo=datetime.now().astimezone().tzinfo)
        return value

    # ------------------------------------------------------------------ ask

    def ask(
        self,
        *,
        principal: Any,
        conversation_id: str,
        request: AskRequest,
        idempotency_key: str,
    ) -> GenerationCreationResult:
        user_id = str(principal.user_id)
        fingerprint = canonical_request_fingerprint(
            "ask",
            {
                "conversation_id": conversation_id,
                "content": request.content,
                "effort_level": request.effort_level,
                "scope": request.scope.to_json() if request.scope is not None else None,
            },
        )
        with self._engine.begin() as connection:
            self._authorization.verify_active(connection, principal)
            conversation = _require_owned_conversation(
                connection, conversation_id=conversation_id, user_id=user_id
            )
            replay = self._find_idempotency(
                connection,
                user_id=user_id,
                kind="ask",
                target_id=conversation_id,
                idempotency_key=idempotency_key,
                fingerprint=fingerprint,
            )
            if replay is not None:
                return self._creation_result(connection, generation_id=replay)
            self._enforce_concurrency(connection, user_id=user_id)
            now = self._now(connection)
            scope_json = request.scope.to_json() if request.scope is not None else {}
            result = self._create_generation(
                connection,
                user_id=user_id,
                principal=principal,
                conversation=conversation,
                content=request.content,
                effort_level=request.effort_level,
                scope_json=scope_json,
                now=now,
            )
            self._record_idempotency(
                connection,
                user_id=user_id,
                kind="ask",
                target_id=conversation_id,
                idempotency_key=idempotency_key,
                fingerprint=fingerprint,
                response_target=result.generation_id,
                now=now,
            )
            return result

    def _creation_result(
        self, connection: Connection, *, generation_id: str
    ) -> GenerationCreationResult:
        row = (
            connection.execute(
                select(
                    chat_generation_table.c.message_id,
                    chat_generation_table.c.user_message_id,
                ).where(chat_generation_table.c.id == generation_id)
            )
            .mappings()
            .one()
        )
        return GenerationCreationResult(
            generation_id=generation_id,
            message_id=str(row["message_id"]),
            user_message_id=str(row["user_message_id"]),
            replay=True,
        )

    def _enforce_concurrency(self, connection: Connection, *, user_id: str) -> None:
        count = len(
            connection.execute(
                select(chat_generation_table.c.id)
                .where(
                    chat_generation_table.c.owner_user_id == user_id,
                    chat_generation_table.c.status.in_(["running", "stop_requested"]),
                )
                .limit(self._max_running_per_user + 1)
            )
            .mappings()
            .all()
        )
        if count >= self._max_running_per_user:
            raise PlatformError(
                "concurrency_limit_exceeded",
                "Too many generations are already running",
                {"max": self._max_running_per_user},
                429,
            )

    def _create_generation(
        self,
        connection: Connection,
        *,
        user_id: str,
        principal: Any,
        conversation: Mapping[str, Any],
        content: str,
        effort_level: str,
        scope_json: Mapping[str, Any],
        now: datetime,
        parent: Mapping[str, Any] | None = None,
    ) -> GenerationCreationResult:
        generation_id = _new_id("gen")
        user_message_id = _new_id("msg")
        message_id = _new_id("msg")
        window = self._calibration.get_open_window(connection, now=now, user_id=user_id)
        create_ab = bool(
            window is not None
            and window.status == "open"
            and not self._calibration.user_ab_opt_out(connection, user_id=user_id)
            and self._sampler() < window.sample_rate
        )
        if parent is None:
            user_message_content = content
            user_scope = scope_json
            requested_effort = effort_level
            attempt_number = 1
            root_generation_id = generation_id
            retry_of_generation_id = None
        else:
            user_message_content = str(parent["request_content"])
            user_scope = dict(parent["request_scope_json"])
            requested_effort = str(parent["requested_effort_level"])
            attempt_number = int(parent["attempt_number"]) + 1
            root_generation_id = str(parent["root_generation_id"])
            retry_of_generation_id = str(parent["id"])
        if parent is None:
            connection.execute(
                chat_message_table.insert().values(
                    id=user_message_id,
                    conversation_id=str(conversation["id"]),
                    owner_user_id=user_id,
                    role="user",
                    content=user_message_content,
                    created_at_utc=now,
                )
            )
            user_message_id_to_keep = user_message_id
        else:
            user_message_id_to_keep = str(parent["user_message_id"])
        connection.execute(
            chat_message_table.insert().values(
                id=message_id,
                conversation_id=str(conversation["id"]),
                owner_user_id=user_id,
                role="assistant",
                content="",
                answer_mode=None,
                effort_level=requested_effort,
                generation_id=generation_id,
                root_generation_id=root_generation_id,
                retry_of_generation_id=retry_of_generation_id,
                attempt_number=attempt_number,
                status="generating",
                stop_reason=None,
                notices_json=None,
                citations_json=None,
                created_at_utc=now,
            )
        )
        generation_values = {
            "id": generation_id,
            "conversation_id": str(conversation["id"]),
            "owner_user_id": user_id,
            "user_message_id": user_message_id_to_keep,
            "message_id": message_id,
            "root_generation_id": root_generation_id,
            "retry_of_generation_id": retry_of_generation_id,
            "attempt_number": attempt_number,
            "status": "running",
            "stop_reason": None,
            "requested_effort_level": requested_effort,
            "effective_effort_level": requested_effort,
            "upgraded_from": None,
            "retrieval_profile_id": DEFAULT_RETRIEVAL_PROFILE_ID,
            "retrieval_profile_version": DEFAULT_RETRIEVAL_PROFILE_VERSION,
            "rag_budget_policy_version": RAG_BUDGET_POLICY_VERSION,
            "absolute_deadline_at_utc": now + timedelta(seconds=self._absolute_deadline_seconds),
            "auth_session_id": str(principal.auth_session_id),
            "control_version": 1,
            "request_content": user_message_content,
            "request_scope_json": dict(user_scope),
            "window_id": window.window_id if window else None,
            "window_policy_version": window.policy_version if window else None,
            "window_sample_rate": str(window.sample_rate) if window else None,
            "window_kind": window.window_kind if window else None,
            "disconnect_deadline_at_utc": None,
            "last_error_code": None,
            "provider_reconciliation_state": None,
            "version": 1,
            "created_at_utc": now,
            "updated_at_utc": now,
        }
        connection.execute(chat_generation_table.insert().values(**generation_values))
        connection.execute(
            chat_generation_execution_table.insert().values(
                execution_id=_new_id("exec"),
                generation_id=generation_id,
                execution_attempt_number=1,
                status="queued",
                lease_owner=None,
                lease_expires_at_utc=None,
                heartbeat_at_utc=None,
                fencing_token=1,
                checkpoint_version=0,
                checkpoint_json=None,
                next_attempt_at_utc=now,
                last_error_classification=None,
                provider_reconciliation_state=None,
                created_at_utc=now,
                updated_at_utc=now,
            )
        )
        append_event(
            connection,
            generation_id=generation_id,
            event_type="start",
            data={
                "generation_id": generation_id,
                "message_id": message_id,
                "user_message_id": user_message_id_to_keep,
                "attempt_number": attempt_number,
            },
            now=now,
        )
        if create_ab and window is not None:
            self._create_ab_pair(
                connection,
                generation_id=generation_id,
                message_id=message_id,
                user_id=user_id,
                window=window,
                now=now,
            )
        if parent is None:
            self._touch_conversation(
                connection,
                conversation=conversation,
                content=content,
                effort_level=effort_level,
                scope_json=scope_json,
                now=now,
            )
        return GenerationCreationResult(
            generation_id=generation_id,
            message_id=message_id,
            user_message_id=user_message_id_to_keep,
            replay=False,
        )

    @staticmethod
    def _create_ab_pair(
        connection: Connection,
        *,
        generation_id: str,
        message_id: str,
        user_id: str,
        window: CalibrationWindowSnapshot,
        now: datetime,
    ) -> None:
        pair_id = _new_id("pair")
        ttl_seconds = window.pair_vote_ttl_seconds
        if not ttl_seconds or ttl_seconds <= 0:
            ttl_seconds = AB_PAIR_OPEN_SECONDS
        expires_at = now + timedelta(seconds=ttl_seconds)
        # The pair expiry is the earlier of the policy TTL and the existing
        # window close deadline (A31).
        if window.close_deadline_at_utc is not None and window.close_deadline_at_utc < expires_at:
            expires_at = window.close_deadline_at_utc
        connection.execute(
            chat_ab_pair_table.insert().values(
                pair_id=pair_id,
                generation_id=generation_id,
                message_id=message_id,
                window_id=window.window_id,
                owner_user_id=user_id,
                status="pending",
                voted=False,
                choice=None,
                voted_at_utc=None,
                expires_at_utc=expires_at,
                close_deadline_at_utc=window.close_deadline_at_utc,
                version=1,
                created_at_utc=now,
                updated_at_utc=now,
            )
        )
        for candidate in (0, 1):
            connection.execute(
                chat_ab_candidate_table.insert().values(
                    pair_id=pair_id,
                    candidate=candidate,
                    status="planned",
                    content="",
                    citations_json=[],
                    answer_mode="no_context",
                    created_at_utc=now,
                )
            )
        append_event(
            connection,
            generation_id=generation_id,
            event_type="ab_start",
            data={"pair_id": pair_id, "message_id": message_id, "candidates": [0, 1]},
            now=now,
        )

    @staticmethod
    def _touch_conversation(
        connection: Connection,
        *,
        conversation: Mapping[str, Any],
        content: str,
        effort_level: str,
        scope_json: Mapping[str, Any],
        now: datetime,
    ) -> None:
        values: dict[str, Any] = {
            "effort_level": effort_level,
            "scope_json": dict(scope_json),
            "last_active_at_utc": now,
            "updated_at_utc": now,
        }
        if not str(conversation["title"]).strip():
            values["title"] = _derive_title(content)
        connection.execute(
            update(chat_conversation_table)
            .where(chat_conversation_table.c.id == str(conversation["id"]))
            .values(**values)
        )

    # ---------------------------------------------------------------- stop

    def stop(self, *, principal: Any, generation_id: str) -> dict[str, Any]:
        with self._engine.begin() as connection:
            self._authorization.verify_active(connection, principal)
            generation = self._require_owned_generation(
                connection, principal=principal, generation_id=generation_id
            )
            status = str(generation["status"])
            if status == "stopped":
                return {
                    "generation_id": generation_id,
                    "message_id": generation["message_id"],
                    "status": "stopped",
                    "stop_reason": generation["stop_reason"],
                }
            if status in {"completed", "failed"}:
                raise PlatformError(
                    "generation_already_terminal",
                    "The generation is already in a terminal state",
                    {"status": status},
                    409,
                )
            if status == "running":
                connection.execute(
                    update(chat_generation_table)
                    .where(
                        chat_generation_table.c.id == generation_id,
                        chat_generation_table.c.status == "running",
                    )
                    .values(
                        status="stop_requested",
                        stop_reason="manual_request",
                        control_version=chat_generation_table.c.control_version + 1,
                        updated_at_utc=self._now(connection),
                    )
                )
            return {
                "generation_id": generation_id,
                "message_id": generation["message_id"],
                "status": "stop_requested",
            }

    def _require_owned_generation(
        self, connection: Connection, *, principal: Any, generation_id: str
    ) -> Mapping[str, Any]:
        row = (
            connection.execute(
                select(chat_generation_table)
                .where(chat_generation_table.c.id == generation_id)
                .with_for_update()
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise PlatformError("generation_not_found", "Generation was not found", {}, 404)
        if str(row["owner_user_id"]) != str(principal.user_id):
            raise PlatformError("generation_not_found", "Generation was not found", {}, 404)
        return dict(row)

    # --------------------------------------------------------------- retry

    def retry(
        self,
        *,
        principal: Any,
        failed_generation_id: str,
        idempotency_key: str,
    ) -> GenerationCreationResult:
        user_id = str(principal.user_id)
        with self._engine.begin() as connection:
            self._authorization.verify_active(connection, principal)
            failed = self._require_owned_generation(
                connection, principal=principal, generation_id=failed_generation_id
            )
            if str(failed["status"]) != "failed":
                raise PlatformError(
                    "generation_not_retryable",
                    "Only a failed generation can be retried",
                    {"status": failed["status"]},
                    409,
                )
            direct_retry = connection.execute(
                select(chat_generation_table.c.id).where(
                    chat_generation_table.c.retry_of_generation_id == failed_generation_id
                )
            ).scalar_one_or_none()
            if direct_retry is not None:
                fingerprint = self._retry_fingerprint(failed)
                replay = self._find_idempotency(
                    connection,
                    user_id=user_id,
                    kind="retry",
                    target_id=failed_generation_id,
                    idempotency_key=idempotency_key,
                    fingerprint=fingerprint,
                )
                if replay is not None:
                    return self._creation_result(connection, generation_id=replay)
                self._record_idempotency(
                    connection,
                    user_id=user_id,
                    kind="retry",
                    target_id=failed_generation_id,
                    idempotency_key=idempotency_key,
                    fingerprint=fingerprint,
                    response_target=str(direct_retry),
                    now=self._now(connection),
                )
                return self._creation_result(connection, generation_id=str(direct_retry))
            fingerprint = self._retry_fingerprint(failed)
            replay = self._find_idempotency(
                connection,
                user_id=user_id,
                kind="retry",
                target_id=failed_generation_id,
                idempotency_key=idempotency_key,
                fingerprint=fingerprint,
            )
            if replay is not None:
                return self._creation_result(connection, generation_id=replay)
            conversation = _require_owned_conversation(
                connection,
                conversation_id=str(failed["conversation_id"]),
                user_id=user_id,
            )
            now = self._now(connection)
            result = self._create_generation(
                connection,
                user_id=user_id,
                principal=principal,
                conversation=conversation,
                content="",
                effort_level=str(failed["requested_effort_level"]),
                scope_json=dict(failed["request_scope_json"]),
                now=now,
                parent=failed,
            )
            self._record_idempotency(
                connection,
                user_id=user_id,
                kind="retry",
                target_id=failed_generation_id,
                idempotency_key=idempotency_key,
                fingerprint=fingerprint,
                response_target=result.generation_id,
                now=now,
            )
            return result

    @staticmethod
    def _retry_fingerprint(failed: Mapping[str, Any]) -> str:
        return canonical_request_fingerprint(
            "retry",
            {
                "failed_generation_id": str(failed["id"]),
                "content": str(failed["request_content"]),
                "effort_level": str(failed["requested_effort_level"]),
                "scope": dict(failed["request_scope_json"]),
            },
        )

    # ------------------------------------------------------------- feedback

    def submit_feedback(
        self,
        *,
        principal: Any,
        message_id: str,
        request: FeedbackRequest,
        idempotency_key: str,
    ) -> None:
        user_id = str(principal.user_id)
        fingerprint = canonical_request_fingerprint(
            "feedback",
            {
                "message_id": message_id,
                "vote": request.vote,
                "down_reason": request.down_reason,
            },
        )
        with self._engine.begin() as connection:
            self._authorization.verify_active(connection, principal)
            self._require_owned_assistant_message(
                connection, principal=principal, message_id=message_id
            )
            pair = (
                connection.execute(
                    select(
                        chat_ab_pair_table.c.voted,
                        chat_ab_pair_table.c.choice,
                    ).where(chat_ab_pair_table.c.message_id == message_id)
                )
                .mappings()
                .one_or_none()
            )
            if pair is not None and (not bool(pair["voted"]) or str(pair["choice"]) == "neither"):
                raise PlatformError(
                    "feedback_not_available",
                    "A/B answers only accept permanent feedback after a 0/1 vote",
                    {},
                    409,
                )
            replay = self._find_idempotency(
                connection,
                user_id=user_id,
                kind="feedback",
                target_id=message_id,
                idempotency_key=idempotency_key,
                fingerprint=fingerprint,
            )
            if replay is not None:
                return
            existing = connection.execute(
                select(chat_message_feedback_table.c.message_id).where(
                    chat_message_feedback_table.c.message_id == message_id,
                    chat_message_feedback_table.c.voter_user_id == user_id,
                )
            ).scalar_one_or_none()
            if existing is not None:
                raise PlatformError(
                    "feedback_already_submitted",
                    "Feedback has already been submitted for this message",
                    {},
                    409,
                )
            now = self._now(connection)
            connection.execute(
                chat_message_feedback_table.insert().values(
                    message_id=message_id,
                    voter_user_id=user_id,
                    vote=request.vote,
                    down_reason=request.down_reason,
                    created_at_utc=now,
                )
            )
            self._record_idempotency(
                connection,
                user_id=user_id,
                kind="feedback",
                target_id=message_id,
                idempotency_key=idempotency_key,
                fingerprint=fingerprint,
                response_target=message_id,
                now=now,
            )

    def _require_owned_assistant_message(
        self, connection: Connection, *, principal: Any, message_id: str
    ) -> Mapping[str, Any]:
        row = (
            connection.execute(
                select(chat_message_table)
                .where(chat_message_table.c.id == message_id)
                .with_for_update()
            )
            .mappings()
            .one_or_none()
        )
        if row is None or str(row["owner_user_id"]) != str(principal.user_id):
            raise PlatformError("message_not_found", "Message was not found", {}, 404)
        if str(row["role"]) != "assistant":
            raise PlatformError(
                "validation_error",
                "Only assistant messages accept feedback",
                {"field": "message_id"},
                422,
            )
        return dict(row)

    # -------------------------------------------------------------- ab vote

    def submit_ab_vote(
        self,
        *,
        principal: Any,
        message_id: str,
        request: AbVoteRequest,
        idempotency_key: str,
    ) -> dict[str, Any]:
        user_id = str(principal.user_id)
        fingerprint = canonical_request_fingerprint(
            "ab_vote",
            {
                "message_id": message_id,
                "pair_id": request.pair_id,
                "choice": request.choice,
            },
        )
        with self._engine.begin() as connection:
            self._authorization.verify_active(connection, principal)
            self._require_owned_assistant_message(
                connection, principal=principal, message_id=message_id
            )
            pair = (
                connection.execute(
                    select(chat_ab_pair_table)
                    .where(
                        chat_ab_pair_table.c.message_id == message_id,
                        chat_ab_pair_table.c.pair_id == request.pair_id,
                    )
                    .with_for_update()
                )
                .mappings()
                .one_or_none()
            )
            if pair is None:
                raise PlatformError("ab_pair_not_found", "A/B pair was not found", {}, 404)
            replay = self._find_idempotency(
                connection,
                user_id=user_id,
                kind="ab_vote",
                target_id=message_id,
                idempotency_key=idempotency_key,
                fingerprint=fingerprint,
            )
            if replay is not None:
                return {
                    "pair_id": request.pair_id,
                    "voted": True,
                    "choice": str(pair["choice"]),
                }
            if bool(pair["voted"]):
                raise PlatformError(
                    "ab_vote_already_submitted",
                    "This A/B pair has already been voted",
                    {},
                    409,
                )
            now = self._now(connection)
            deadline = self._pair_vote_deadline(pair, now)
            if str(pair["status"]) != "open" or (deadline is not None and now > deadline):
                if str(pair["status"]) != "expired":
                    connection.execute(
                        update(chat_ab_pair_table)
                        .where(chat_ab_pair_table.c.pair_id == request.pair_id)
                        .values(status="expired", updated_at_utc=now)
                    )
                raise PlatformError(
                    "ab_pair_expired", "This A/B pair is no longer open for voting", {}, 409
                )
            connection.execute(
                chat_ab_vote_table.insert().values(
                    pair_id=request.pair_id,
                    voter_user_id=user_id,
                    choice=request.choice,
                    created_at_utc=now,
                )
            )
            content = ""
            answer_mode = None
            if request.choice in {"0", "1"}:
                candidate = (
                    connection.execute(
                        select(
                            chat_ab_candidate_table.c.content,
                            chat_ab_candidate_table.c.answer_mode,
                        ).where(
                            chat_ab_candidate_table.c.pair_id == request.pair_id,
                            chat_ab_candidate_table.c.candidate == int(request.choice),
                        )
                    )
                    .mappings()
                    .one()
                )
                content = str(candidate["content"])
                answer_mode = str(candidate["answer_mode"])
            connection.execute(
                update(chat_message_table)
                .where(chat_message_table.c.id == message_id)
                .values(
                    content=content,
                    answer_mode=answer_mode,
                    updated_at_utc=now,
                )
            )
            connection.execute(
                update(chat_ab_pair_table)
                .where(chat_ab_pair_table.c.pair_id == request.pair_id)
                .values(
                    status="voted",
                    voted=True,
                    choice=request.choice,
                    voted_at_utc=now,
                    updated_at_utc=now,
                )
            )
            if pair["window_id"] is not None:
                self._calibration.increment_pairs_collected(connection, str(pair["window_id"]))
            self._record_idempotency(
                connection,
                user_id=user_id,
                kind="ab_vote",
                target_id=message_id,
                idempotency_key=idempotency_key,
                fingerprint=fingerprint,
                response_target=request.pair_id,
                now=now,
            )
            return {"pair_id": request.pair_id, "voted": True, "choice": request.choice}

    @staticmethod
    def _pair_vote_deadline(pair: Any, now: datetime) -> datetime | None:
        candidates = [
            _utc(value)
            for value in (pair["expires_at_utc"], pair["close_deadline_at_utc"])
            if value is not None
        ]
        return min(candidates) if candidates else _utc(now) + timedelta(days=3650)

    # ---------------------------------------------------------- idempotency

    @staticmethod
    def _find_idempotency(
        connection: Connection,
        *,
        user_id: str,
        kind: str,
        target_id: str,
        idempotency_key: str,
        fingerprint: str,
    ) -> str | None:
        row = (
            connection.execute(
                select(chat_idempotency_table).where(
                    chat_idempotency_table.c.user_id == user_id,
                    chat_idempotency_table.c.kind == kind,
                    chat_idempotency_table.c.target_id == target_id,
                    chat_idempotency_table.c.idempotency_key == idempotency_key,
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            return None
        if str(row["request_hash"]) != fingerprint:
            raise PlatformError(
                "idempotency_key_conflict",
                "The idempotency key was already used with a different request",
                {},
                409,
            )
        return str(row["response_target"])

    @staticmethod
    def _record_idempotency(
        connection: Connection,
        *,
        user_id: str,
        kind: str,
        target_id: str,
        idempotency_key: str,
        fingerprint: str,
        response_target: str,
        now: datetime,
    ) -> None:
        connection.execute(
            chat_idempotency_table.insert().values(
                user_id=user_id,
                kind=kind,
                target_id=target_id,
                idempotency_key=idempotency_key,
                request_hash=fingerprint,
                response_target=response_target,
                created_at_utc=now,
            )
        )

    # ------------------------------------------------------------- reading

    def list_events(
        self, connection: Connection, *, generation_id: str, after_seq: int
    ) -> list[Any]:
        from .events import list_events_after

        return list_events_after(connection, generation_id=generation_id, after_seq=after_seq)

    def generation_for_stream(self, *, principal: Any, generation_id: str) -> Mapping[str, Any]:
        with self._engine.connect() as connection:
            return self._require_owned_generation(
                connection, principal=principal, generation_id=generation_id
            )


def _default_sampler() -> float:
    import random

    return random.random()


__all__ = [
    "DEFAULT_RETRIEVAL_PROFILE_ID",
    "DEFAULT_RETRIEVAL_PROFILE_VERSION",
    "GenerationCreationResult",
    "GenerationService",
]
