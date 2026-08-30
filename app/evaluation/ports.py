"""Evaluation domain consumed ports and production adapters."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any, Protocol

from sqlalchemy.engine import Connection

from app.chat.models import ChatProviderResponse
from app.chat.ports import ChatProviderPort, ChatProviderRequest, source_conflict_contract
from app.platform.errors import PlatformError

from .judge import JudgeProviderPort, JudgeRequest, JudgeScores
from .snapshot import SqlAlchemyChatFactsSnapshot


class ChatFactsPort(Protocol):
    """Read-only committed chat facts for real-question sampling."""

    def collect_samples(
        self,
        connection: Connection,
        *,
        space_id: str,
        limit: int,
    ) -> tuple[dict[str, Any], ...]: ...


class SqlAlchemyChatFactsPort:
    """Production adapter over the read-only chat facts snapshot."""

    def __init__(self, engine: Any) -> None:
        self._snapshot = SqlAlchemyChatFactsSnapshot(engine)

    def collect_samples(
        self,
        connection: Connection,
        *,
        space_id: str,
        limit: int,
    ) -> tuple[dict[str, Any], ...]:
        return self._snapshot.collect_samples(connection, space_id=space_id, limit=limit)


class CandidateConfigSourcePort(Protocol):
    """Resolves the candidate query configurations for a space."""

    def candidate_config_versions(self, *, space_id: str) -> tuple[str, ...]: ...


class IndexGenerationSourcePort(Protocol):
    """Reads the active index generation id and revision."""

    def active_generation(self) -> tuple[str, int]: ...


class IndexingGenerationSourceAdapter:
    """Adapts the indexing repository/generation manager to the source port."""

    def __init__(self, repository: Any, generation_manager: Any) -> None:
        self._repository = repository
        self._generation_manager = generation_manager

    def active_generation(self) -> tuple[str, int]:
        generation_id = self._generation_manager.active_generation_id
        revision = self._generation_manager.current_revision
        return str(generation_id), int(revision)


class RetrievalReplayPort(Protocol):
    """Controlled query-pipeline replay for shadow sessions (A10)."""

    def replay(
        self,
        *,
        question: str,
        principal: Any,
        space_id: str,
        candidate_config_version: str,
        session_id: str,
    ) -> Mapping[str, Any]: ...


class AnswerReplayPort(Protocol):
    """Produces one answer through the online generation provider boundary."""

    def replay(
        self,
        *,
        question: str,
        source_ref: str,
        principal: Any,
        space_id: str,
        candidate_config_version: str,
        session_id: str,
        context_items: tuple[Mapping[str, Any], ...] = (),
    ) -> str: ...


class OnlineAnswerReplayAdapter:
    """Run one evaluation answer through the normal chat provider pipeline.

    Evaluation owns the retrieval replay and supplies its immutable context;
    this adapter deliberately skips chat persistence, SSE and A/B publication.
    The provider still receives the canonical ``ChatProviderRequest`` so prompt
    assembly and source-conflict instructions remain identical to online chat.
    """

    def __init__(self, provider: ChatProviderPort, *, effort_level: str = "think") -> None:
        self._provider = provider
        self._effort_level = effort_level

    def replay(
        self,
        *,
        question: str,
        source_ref: str,
        principal: Any,
        space_id: str,
        candidate_config_version: str,
        session_id: str,
        context_items: tuple[Mapping[str, Any], ...] = (),
    ) -> str:
        del source_ref, space_id, candidate_config_version
        owner_user_id = str(getattr(principal, "user_id", "") or "system:evaluation")
        context = tuple(dict(item) for item in context_items if isinstance(item, Mapping))
        request = ChatProviderRequest(
            generation_id=session_id,
            owner_user_id=owner_user_id,
            content=str(question),
            effort_level=self._effort_level,
            # Evaluation answers remain blind to the deployment configuration.
            candidate=None,
            context_items=context,
            source_conflict_contract=source_conflict_contract(),
        )
        try:
            response: ChatProviderResponse = self._provider.generate(request)
            content = getattr(response, "content", None)
        except Exception:
            # Do not expose provider-specific diagnostics through evaluation
            # run state, logs or the HTTP API.
            raise PlatformError(
                "evaluation_generation_unavailable",
                "The evaluation answer replay is unavailable",
                {"retryable": True},
                503,
                True,
            ) from None
        if not isinstance(content, str) or not content.strip():
            raise PlatformError(
                "evaluation_generation_unavailable",
                "The evaluation answer replay returned no answer",
                {"retryable": True},
                503,
                True,
            )
        return content.strip()


class IndexingReplayAdapter:
    """Replays one sample through the indexing retrieval port with a fresh lease.

    Each sample uses an independent ``open_retrieval_request``/lease and no
    cross-sample state, mirroring ``IndexingChatRetrievalPort`` without the
    online-generation coupling (A10).
    """

    def __init__(self, indexing_service: Any) -> None:
        self._indexing = indexing_service

    def replay(
        self,
        *,
        question: str,
        principal: Any,
        space_id: str,
        candidate_config_version: str,
        session_id: str,
    ) -> Mapping[str, Any]:
        from app.indexing.models import RetrievalProfile

        profile = RetrievalProfile(
            profile_id="default",
            version=candidate_config_version,
            effort="think",
        )
        request = self._indexing.open_retrieval_request()
        try:
            result = request.search(
                question,
                principal=principal,
                narrowing_scope={"space_ids": [space_id]},
                profile=profile,
            )

            def _to_mapping(hit: Any) -> dict[str, Any]:
                return {
                    "document_id": hit.chunk.document_id,
                    "document_version_id": hit.chunk.document_version_id,
                    "publication_id": hit.chunk.publication_id,
                    "chunk_id": hit.chunk.chunk_id,
                    "space_id": hit.chunk.space_id,
                    "locator": dict(hit.chunk.locator),
                    "snippet": hit.chunk.snippet,
                }

            return {
                "session_id": session_id,
                "candidate_config_version": candidate_config_version,
                "hits": tuple(_to_mapping(hit) for hit in result.hits),
                # Pre-rerank candidate pool feeds hit_at_k_candidate while the
                # final ranking feeds hit_at_k_final (A4).
                "candidate_hits": tuple(_to_mapping(hit) for hit in result.candidates),
                "degradations": tuple(result.degradations),
            }
        finally:
            request.__exit__(None, None, None)


class CalibrationOutboxPort(Protocol):
    """Publishes the calibration_window_suggested outbox event (A27)."""

    def publish_suggested(
        self,
        *,
        suggestion_id: str,
        transition_version: int,
        occurred_at: datetime,
        connection: Connection,
    ) -> str: ...


class UsageSubmissionPort(Protocol):
    def prepare_provider_call(
        self,
        *,
        provider: str,
        model: str,
        operation: str,
        execution_kind: str,
        execution_id: str,
        provider_call_id: str | None = None,
        attempt_id: str | None = None,
        generation_id: str | None = None,
        resource_id: str | None = None,
        deadline_utc: datetime,
        request_fingerprint: str,
        replay_generation: int = 0,
    ) -> str: ...

    def mark_dispatching(
        self,
        provider_call_id: str,
        *,
        started_at_provider: Any,
    ) -> bool: ...

    def complete_provider_call(
        self,
        *,
        provider_call_id: str,
        measurement: Any,
        ownership: Any,
        result: str,
        provider_request_id: str | None = None,
        started_at_utc: Any | None = None,
    ) -> str: ...


class SpaceVisibilityPort(Protocol):
    """Server-owned ACL space visibility for cross-space aggregation (A24)."""

    def visible_space_ids(self, actor: Any) -> frozenset[str]: ...


class IdentitySpaceVisibilityPort:
    """Adapts the identity retrieval scope to the evaluation visibility port.

    Aggregation happens only over spaces the principal may retrieve from; the
    identity service owns the ACL projection and evaluation never re-derives
    role or department rules itself.
    """

    def __init__(self, identity_access: Any) -> None:
        self._identity = identity_access

    def visible_space_ids(self, actor: Any) -> frozenset[str]:
        scope = self._identity.allowed_retrieval_scope(actor)
        return frozenset(str(item) for item in scope.get("space_ids", ()))


class UnavailableRetrievalReplayPort:
    def replay(
        self,
        *,
        question: str,
        principal: Any,
        space_id: str,
        candidate_config_version: str,
        session_id: str,
    ) -> Mapping[str, Any]:
        del question, principal, space_id, candidate_config_version, session_id
        raise PlatformError(
            "evaluation_retrieval_unavailable",
            "Retrieval replay is not configured",
            {"retryable": True},
            503,
            True,
        )


class UnavailableAnswerReplayPort:
    def replay(
        self,
        *,
        question: str,
        source_ref: str,
        principal: Any,
        space_id: str,
        candidate_config_version: str,
        session_id: str,
        context_items: tuple[Mapping[str, Any], ...] = (),
    ) -> str:
        del (
            question,
            source_ref,
            principal,
            space_id,
            candidate_config_version,
            session_id,
            context_items,
        )
        raise PlatformError(
            "evaluation_generation_unavailable",
            "Answer replay is not configured",
            {"retryable": True},
            503,
            True,
        )


__all__ = [
    "AnswerReplayPort",
    "CalibrationOutboxPort",
    "CandidateConfigSourcePort",
    "ChatFactsPort",
    "IdentitySpaceVisibilityPort",
    "IndexGenerationSourcePort",
    "IndexingGenerationSourceAdapter",
    "IndexingReplayAdapter",
    "OnlineAnswerReplayAdapter",
    "JudgeProviderPort",
    "JudgeRequest",
    "JudgeScores",
    "RetrievalReplayPort",
    "SpaceVisibilityPort",
    "SqlAlchemyChatFactsPort",
    "UnavailableAnswerReplayPort",
    "UnavailableRetrievalReplayPort",
    "UsageSubmissionPort",
]
