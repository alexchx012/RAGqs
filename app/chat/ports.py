"""Cross-domain ports owned or consumed by chat-generation.

Consumed ports (retrieval, provider, calibration window, usage, authorization) are
declared as protocols; production adapters wire the real indexing/usage/identity
implementations and tests inject deterministic fakes. The chat-owned
``GenerationRevocationPort`` is implemented inline here: identity invokes it inside
its session/account revocation transaction and chat converges running generations
to a single stopped terminal.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, Protocol, cast

from sqlalchemy import inspect as sqlalchemy_inspect
from sqlalchemy import select, update
from sqlalchemy.engine import Connection

from app.identity.revocation import (
    GenerationRevocationCommand,
    GenerationRevocationPort,
    GenerationRevocationReceipt,
)
from app.identity.schema import identity_revocation_command_table
from app.indexing.models import RetrievalProfile
from app.platform.errors import PlatformError
from app.usage.ledger import OwnershipSnapshot
from app.usage.ports import UsageSubmissionPort

from .models import CalibrationWindowSnapshot, ChatProviderResponse, RetrievalOutcome
from .schema import (
    chat_ab_pair_table,
    chat_generation_table,
    chat_revocation_consumption_table,
    chat_subscription_lease_table,
)


@dataclass(frozen=True, slots=True)
class ChatProviderRequest:
    generation_id: str
    owner_user_id: str
    content: str
    effort_level: str
    candidate: int | None
    context_items: tuple[Mapping[str, Any], ...]
    source_conflict_contract: Mapping[str, Any] | None = None


class ChatAuthorizationPort(Protocol):
    """Re-checks the authenticated principal inside the chat mutation transaction."""

    def verify_active(self, connection: Connection, principal: Any) -> Any: ...

    def allowed_retrieval_scope(self, principal: Any) -> Mapping[str, Any]: ...


class IdentityChatAuthorizationPort:
    """Wraps the identity access service for chat transaction re-authorization."""

    def __init__(self, identity_access: Any) -> None:
        self._identity = identity_access

    def verify_active(self, connection: Connection, principal: Any) -> Any:
        return self._identity._current_acl_principal(connection, principal)

    def allowed_retrieval_scope(self, principal: Any) -> Mapping[str, Any]:
        return self._identity.allowed_retrieval_scope(principal)


class ChatRetrievalPort(Protocol):
    """Search and citation resolution against the indexing domain.

    Chat never builds space filters or visibility predicates itself: the adapter
    must intersect the client narrowing with the server ACL and resolve every
    citation through the indexing domain before an answer can be published.
    """

    def search(
        self,
        query: str,
        *,
        principal: Any,
        narrowing_scope: Any,
        profile_id: str,
        profile_version: str,
        effort: str,
        budget: Any | None = None,
    ) -> RetrievalOutcome: ...

    def resolve_citations(
        self,
        hits: tuple[Mapping[str, Any], ...],
        *,
        principal: Any,
    ) -> tuple[Mapping[str, Any], ...]: ...


class ChatProviderPort(Protocol):
    """Chat model transport contract."""

    def generate(self, request: ChatProviderRequest) -> ChatProviderResponse: ...


class UnavailableChatProviderPort:
    """Fail-closed production placeholder until a real chat transport exists."""

    def generate(self, request: ChatProviderRequest) -> ChatProviderResponse:
        del request
        raise PlatformError(
            "provider_unavailable",
            "Chat model transport is not configured",
            {"retryable": True},
            503,
            True,
        )


class PromptEnhancePort(Protocol):
    """Single-shot prompt-enhancement transport contract (prompt → text).

    Consumed by the ``POST /v1/prompt-enhancements`` route: one call in, one
    rewritten prompt out, no persistence or chat side effects.
    """

    def enhance(self, prompt: str) -> str: ...


class UnavailablePromptEnhanceProviderPort:
    """Fail-closed placeholder when no prompt-enhance provider is configured."""

    def enhance(self, prompt: str) -> str:
        del prompt
        raise PlatformError(
            "prompt_enhance_unavailable",
            "Prompt enhancement is not configured",
            {"retryable": True},
            503,
            True,
        )


class CalibrationWindowPort(Protocol):
    """Read-only calibration window facts owned by the evaluation domain."""

    def get_open_window(
        self,
        connection: Connection,
        *,
        now: datetime,
        user_id: str,
    ) -> CalibrationWindowSnapshot | None: ...

    def user_ab_opt_out(self, connection: Connection, *, user_id: str) -> bool: ...

    def increment_pairs_collected(self, connection: Connection, window_id: str) -> None: ...


class NoCalibrationWindowPort:
    """Placeholder until be-evaluation-calibration lands: no open window exists."""

    def get_open_window(
        self,
        connection: Connection,
        *,
        now: datetime,
        user_id: str,
    ) -> CalibrationWindowSnapshot | None:
        del connection, now, user_id
        return None

    def user_ab_opt_out(self, connection: Connection, *, user_id: str) -> bool:
        del connection, user_id
        return False

    def increment_pairs_collected(self, connection: Connection, window_id: str) -> None:
        del connection, window_id


class ChatPairExpiryPort(Protocol):
    """Chat-owned A/B pair expiry service consumed by the evaluation domain.

    The evaluation close worker must never write ``chat_ab_pair`` directly
    (A2): it requests pair expiry through this chat-owned port, and the write
    stays inside the chat domain.
    """

    def window_has_votable_pairs(self, connection: Connection, *, window_id: str) -> bool: ...

    def expire_window_pairs(
        self, connection: Connection, *, window_id: str, now: datetime
    ) -> int: ...


class SqlAlchemyChatPairExpiry:
    """Chat-domain pair expiry over ``chat_ab_pair``.

    Missing chat schema (pre-migration) degrades to "no pairs": the evaluation
    close worker may close immediately, matching the chat-maintenance reaper's
    own table-absent guard.
    """

    def __init__(self, engine: Any) -> None:
        self._engine = engine

    @staticmethod
    def _table_present(connection: Connection) -> bool:
        return sqlalchemy_inspect(connection).has_table("chat_ab_pair")

    def window_has_votable_pairs(self, connection: Connection, *, window_id: str) -> bool:
        if not self._table_present(connection):
            return False
        return (
            connection.execute(
                select(chat_ab_pair_table.c.pair_id)
                .where(
                    chat_ab_pair_table.c.window_id == window_id,
                    chat_ab_pair_table.c.status.in_(("open", "pending")),
                )
                .limit(1)
            )
            .mappings()
            .one_or_none()
            is not None
        )

    def expire_window_pairs(self, connection: Connection, *, window_id: str, now: datetime) -> int:
        if not self._table_present(connection):
            return 0
        result = connection.execute(
            update(chat_ab_pair_table)
            .where(
                chat_ab_pair_table.c.window_id == window_id,
                chat_ab_pair_table.c.status.in_(("open", "pending")),
            )
            .values(status="expired", updated_at_utc=now)
        )
        return result.rowcount


class IndexingChatRetrievalPort:
    """Adapts the production IndexingService to the chat retrieval contract.

    Keeps the originating retrieval request alive between ``search`` and
    ``resolve_citations`` so every published citation re-passes indexing's
    visibility, lifecycle and ACL checks against the same generation reference
    lease. The chat worker drives this port synchronously for one generation at
    a time, so a single pending request is sufficient.
    """

    def __init__(self, indexing_service: Any) -> None:
        self._indexing = indexing_service
        self._active_request: Any = None
        self._active_hits: dict[tuple[str, str], Any] | None = None

    def search(
        self,
        query: str,
        *,
        principal: Any,
        narrowing_scope: Any,
        profile_id: str,
        profile_version: str,
        effort: str,
        budget: Any | None = None,
    ) -> RetrievalOutcome:
        from .models import RetrievalHitOutcome

        if self._active_request is not None:
            self._release_request(self._active_request)
        profile = RetrievalProfile(
            profile_id=profile_id,
            version=profile_version,
            effort=cast(Literal["quick", "think", "deep"], effort),
        )
        request = self._indexing.open_retrieval_request()
        result = request.search(
            query,
            principal=principal,
            narrowing_scope=narrowing_scope,
            profile=profile,
            budget=budget,
        )
        self._active_request = request
        candidates = result.candidates
        self._active_hits = {(hit.chunk.document_id, hit.chunk.chunk_id): hit for hit in candidates}
        hits = tuple(
            RetrievalHitOutcome(
                document_id=hit.chunk.document_id,
                document_version_id=hit.chunk.document_version_id,
                publication_id=hit.chunk.publication_id,
                chunk_id=hit.chunk.chunk_id,
                space_id=hit.chunk.space_id,
                locator=dict(hit.chunk.locator),
                snippet=hit.chunk.snippet,
                library=hit.source or "unknown",
                rerank_score=hit.rerank_score,
            )
            for hit in candidates
        )
        return RetrievalOutcome(
            hits=hits,
            degradations=tuple(result.degradations),
            route_output=result.route_output,
        )

    def resolve_citations(
        self,
        hits: tuple[Mapping[str, Any], ...],
        *,
        principal: Any,
    ) -> tuple[Mapping[str, Any], ...]:
        request = self._active_request
        lookup = self._active_hits
        self._active_request = None
        self._active_hits = None
        if request is None or lookup is None:
            raise PlatformError(
                "retrieval_request_required",
                "Citation resolution requires the originating retrieval request",
                {},
                409,
            )
        try:
            citations: list[Mapping[str, Any]] = []
            for item in hits:
                hit = lookup.get((str(item["document_id"]), str(item["chunk_id"])))
                if hit is None:
                    continue
                citation = request.resolve_citation(hit, principal=principal)
                if citation.get("state") != "available":
                    continue
                citations.append(
                    {
                        "document_id": citation["document_id"],
                        "document_version_id": citation["document_version_id"],
                        "publication_id": citation["publication_id"],
                        "chunk_id": citation["chunk_id"],
                        "space_id": citation.get("space_id", hit.chunk.space_id),
                        "library": hit.source or "unknown",
                        "locator": dict(citation["locator"]),
                        "snippet": citation["snippet"],
                    }
                )
            return tuple(citations)
        finally:
            self._release_request(request)

    @staticmethod
    def _release_request(request: Any) -> None:
        request.__exit__(None, None, None)


class RecordingChatRetrievalPort:
    """Test adapter: records searches and returns injected outcomes."""

    def __init__(self) -> None:
        self.searches: list[dict[str, Any]] = []
        self.outcomes: dict[str, RetrievalOutcome] = {}
        self.citations: dict[tuple[str, str], Mapping[str, Any]] = {}

    def search(
        self,
        query: str,
        *,
        principal: Any,
        narrowing_scope: Any,
        profile_id: str,
        profile_version: str,
        effort: str,
        budget: Any | None = None,
    ) -> RetrievalOutcome:
        self.searches.append(
            {
                "query": query,
                "user_id": getattr(principal, "user_id", principal),
                "scope": narrowing_scope,
                "profile_id": profile_id,
                "profile_version": profile_version,
                "effort": effort,
            }
        )
        return self.outcomes.get(query, RetrievalOutcome(hits=()))

    def resolve_citations(
        self,
        hits: tuple[Mapping[str, Any], ...],
        *,
        principal: Any,
    ) -> tuple[Mapping[str, Any], ...]:
        del principal
        return tuple(
            self.citations.get(
                (str(hit["document_id"]), str(hit["chunk_id"])),
                {
                    "document_id": hit["document_id"],
                    "document_version_id": hit.get("document_version_id"),
                    "publication_id": hit.get("publication_id"),
                    "chunk_id": hit.get("chunk_id"),
                    "locator": dict(hit.get("locator") or {}),
                    "snippet": hit.get("snippet"),
                },
            )
            for hit in hits
        )


class ChatUsageRecorder:
    """Provider/local usage submission with the persisted chat ownership facts."""

    def __init__(
        self,
        submission: UsageSubmissionPort,
        *,
        actor_user_id: str,
        actor_role_snapshot: str = "user",
        actor_department_id_snapshot: str | None = None,
        quota_subject_user_id: str | None = None,
        cost_center_key: str | None = None,
        source_space_ids: tuple[str, ...] = (),
    ) -> None:
        self._submission = submission
        self._actor_user_id = actor_user_id
        self._actor_role_snapshot = actor_role_snapshot
        self._actor_department_id_snapshot = actor_department_id_snapshot
        self._quota_subject_user_id = quota_subject_user_id or actor_user_id
        self._cost_center_key = cost_center_key or f"user:{self._quota_subject_user_id}"
        self._source_space_ids = tuple(source_space_ids)

    @property
    def submission(self) -> UsageSubmissionPort:
        return self._submission

    def ownership(self, *, fence_token: int) -> OwnershipSnapshot:
        space_id = self._source_space_ids[0] if len(self._source_space_ids) == 1 else None
        if space_id == "public":
            space_kind = "public"
            space_owner_user_id = None
        elif space_id is not None and space_id.startswith("department:"):
            space_kind = "department"
            space_owner_user_id = None
        elif space_id is not None and space_id.startswith("personal:"):
            space_kind = "personal"
            space_owner_user_id = self._actor_user_id
        else:
            space_kind = None
            space_owner_user_id = None
        return OwnershipSnapshot(
            actor_user_id=self._actor_user_id,
            actor_role_snapshot=self._actor_role_snapshot,
            actor_department_id_snapshot=self._actor_department_id_snapshot,
            quota_subject_user_id=self._quota_subject_user_id,
            cost_center_key=self._cost_center_key,
            space_id=space_id,
            space_kind=space_kind,
            space_owner_user_id=space_owner_user_id,
            fence_token=fence_token,
            source_space_ids=self._source_space_ids,
        )


class ChatGenerationRevocationPort:
    """Inline, idempotent chat side of identity-driven generation revocation.

    Runs inside the identity revocation transaction on the caller's connection:
    invalidates every nonterminal subscription lease in scope, conditionally moves
    running generations to ``stop_requested`` with pending reason
    ``authorization_revoked`` and increments the control version. Terminal
    generations are never rewritten. Replays of the same operation idempotently
    return the recorded receipt without applying effects twice.
    """

    def revoke(
        self,
        command: GenerationRevocationCommand,
        *,
        connection: Connection,
    ) -> GenerationRevocationReceipt:
        if not sqlalchemy_inspect(connection).has_table("chat_generation"):
            # Pre-chat database (migration not applied yet): nothing to revoke.
            # Degrade to the durable "accepted" semantics so identity can complete.
            return GenerationRevocationReceipt(
                reference=f"generation-outbox:{command.operation_id}",
                state="accepted",
            )
        existing = (
            connection.execute(
                select(identity_revocation_command_table).where(
                    identity_revocation_command_table.c.operation_id == command.operation_id
                )
            )
            .mappings()
            .one_or_none()
        )
        if existing is not None:
            return self._receipt_from_existing(command, dict(existing))
        receipt = GenerationRevocationReceipt(
            reference=f"chat-generation-revocation:{command.operation_id}",
            state="completed",
        )
        consumed = connection.execute(
            select(chat_revocation_consumption_table.c.operation_id).where(
                chat_revocation_consumption_table.c.operation_id == command.operation_id
            )
        ).scalar_one_or_none()
        if consumed is not None:
            return receipt
        apply_revocation_effects(connection, command, now=command.revoked_at)
        connection.execute(
            chat_revocation_consumption_table.insert().values(
                operation_id=command.operation_id,
                applied_at_utc=command.revoked_at,
            )
        )
        return receipt

    @staticmethod
    def _receipt_from_existing(
        command: GenerationRevocationCommand,
        existing: Mapping[str, Any],
    ) -> GenerationRevocationReceipt:
        fields_match = (
            str(existing["user_id"]) == command.user_id
            and existing["auth_session_id"] == command.auth_session_id
            and str(existing["reason"]) == command.reason
            and int(str(existing["identity_transition_version"]))
            == command.identity_transition_version
        )
        if not fields_match:
            raise PlatformError(
                "generation_revocation_conflict",
                "Generation revocation command conflicts with an existing operation",
                {},
                409,
            )
        reference = str(existing["receipt_reference"])
        state = str(existing["receipt_state"])
        if not reference.strip() or state not in {"accepted", "completed"}:
            raise PlatformError(
                "generation_revocation_unverified",
                "Generation revocation receipt is invalid",
                {"retryable": True},
                503,
                True,
            )
        return GenerationRevocationReceipt(
            reference=reference,
            state=cast(Literal["accepted", "completed"], state),
        )


def _generation_ids_for_scope(
    connection: Connection,
    command: GenerationRevocationCommand,
) -> tuple[tuple[str, ...], tuple[dict[str, Any], ...]]:
    """Return (lease generation ids, running generation rows) for the command scope."""

    if command.auth_session_id is not None:
        lease_filter = chat_subscription_lease_table.c.auth_session_id == command.auth_session_id
        generation_filter = chat_generation_table.c.auth_session_id == command.auth_session_id
    else:
        lease_filter = chat_subscription_lease_table.c.generation_id.in_(
            select(chat_generation_table.c.id).where(
                chat_generation_table.c.owner_user_id == command.user_id
            )
        )
        generation_filter = chat_generation_table.c.owner_user_id == command.user_id
    lease_generation_ids = tuple(
        str(row[0])
        for row in connection.execute(
            select(chat_subscription_lease_table.c.generation_id).where(lease_filter)
        ).all()
    )
    running_rows = tuple(
        dict(row)
        for row in connection.execute(
            select(
                chat_generation_table.c.id,
                chat_generation_table.c.control_version,
            ).where(generation_filter, chat_generation_table.c.status == "running")
        )
        .mappings()
        .all()
    )
    return lease_generation_ids, running_rows


def apply_revocation_effects(
    connection: Connection,
    command: GenerationRevocationCommand,
    *,
    now: datetime,
) -> None:
    """Invalidate scoped leases and move running generations to stop_requested."""

    lease_generation_ids, running_rows = _generation_ids_for_scope(connection, command)
    if command.auth_session_id is not None:
        connection.execute(
            chat_subscription_lease_table.delete().where(
                chat_subscription_lease_table.c.auth_session_id == command.auth_session_id
            )
        )
    else:
        connection.execute(
            chat_subscription_lease_table.delete().where(
                chat_subscription_lease_table.c.generation_id.in_(
                    select(chat_generation_table.c.id).where(
                        chat_generation_table.c.owner_user_id == command.user_id
                    )
                )
            )
        )
    for row in running_rows:
        connection.execute(
            update(chat_generation_table)
            .where(
                chat_generation_table.c.id == row["id"],
                chat_generation_table.c.status == "running",
            )
            .values(
                status="stop_requested",
                stop_reason="authorization_revoked",
                control_version=int(row["control_version"]) + 1,
                updated_at_utc=now,
            )
        )


def consume_durable_revocation_commands(
    connection: Connection,
    *,
    now: datetime,
) -> int:
    """Apply any durable revocation command the worker has not consumed yet."""

    rows = (
        connection.execute(
            select(identity_revocation_command_table)
            .outerjoin(
                chat_revocation_consumption_table,
                chat_revocation_consumption_table.c.operation_id
                == identity_revocation_command_table.c.operation_id,
            )
            .where(
                chat_revocation_consumption_table.c.operation_id.is_(None),
                identity_revocation_command_table.c.receipt_state == "accepted",
            )
        )
        .mappings()
        .all()
    )
    for row in rows:
        command = GenerationRevocationCommand(
            operation_id=str(row["operation_id"]),
            user_id=str(row["user_id"]),
            auth_session_id=row["auth_session_id"],
            reason=str(row["reason"]),
            revoked_at=row["created_at_utc"],
            identity_transition_version=int(str(row["identity_transition_version"])),
        )
        apply_revocation_effects(connection, command, now=now)
        connection.execute(
            chat_revocation_consumption_table.insert().values(
                operation_id=command.operation_id,
                applied_at_utc=now,
            )
        )
        connection.execute(
            update(identity_revocation_command_table)
            .where(
                identity_revocation_command_table.c.operation_id == command.operation_id,
                identity_revocation_command_table.c.receipt_state == "accepted",
            )
            .values(receipt_state="completed")
        )
    return len(rows)


__all__ = [
    "ChatAuthorizationPort",
    "ChatGenerationRevocationPort",
    "ChatPairExpiryPort",
    "ChatProviderPort",
    "ChatProviderRequest",
    "ChatRetrievalPort",
    "ChatUsageRecorder",
    "CalibrationWindowPort",
    "GenerationRevocationPort",
    "IndexingChatRetrievalPort",
    "IdentityChatAuthorizationPort",
    "NoCalibrationWindowPort",
    "PromptEnhancePort",
    "RecordingChatRetrievalPort",
    "SqlAlchemyChatPairExpiry",
    "UnavailableChatProviderPort",
    "UnavailablePromptEnhanceProviderPort",
    "apply_revocation_effects",
    "consume_durable_revocation_commands",
]
