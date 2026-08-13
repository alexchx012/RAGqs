"""API read-model and event payload shapes for the chat-generation domain."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.platform.errors import PlatformError

EFFORT_LEVELS = ("quick", "think", "deep")
ANSWER_MODES = ("no_context", "grounded", "direct")
GENERATION_STATUSES = ("running", "stop_requested", "completed", "failed", "stopped")
ASSISTANT_STATUSES = ("generating", "completed", "failed", "stopped")
STOP_REASONS = ("manual_request", "client_disconnected", "authorization_revoked")
NOTICE_KINDS = ("effort_upgraded", "retrieval_degraded", "rerank_degraded")
FEEDBACK_VOTES = ("up", "down")
FEEDBACK_DOWN_REASONS = ("no_grounding", "wrong_citation")
AB_CHOICES = ("0", "1", "neither")
AB_PAIR_STATUSES = ("pending", "open", "voted", "expired")
AB_CANDIDATE_STATUSES = ("planned", "published", "discarded")


@dataclass(frozen=True, slots=True)
class ConversationScope:
    space_ids: tuple[str, ...]
    document_ids: tuple[str, ...]

    @classmethod
    def from_value(cls, value: Any) -> ConversationScope | None:
        if value is None:
            return None
        if not isinstance(value, Mapping):
            raise _invalid("scope", "Scope must be an object")
        space_ids = value.get("space_ids")
        document_ids = value.get("document_ids")
        if space_ids is not None:
            if not isinstance(space_ids, (list, tuple)) or any(
                not isinstance(item, str) or not item for item in space_ids
            ):
                raise _invalid("scope", "scope.space_ids must be an array of strings")
            space_ids = tuple(str(item) for item in space_ids)
        if document_ids is not None:
            if not isinstance(document_ids, (list, tuple)) or any(
                not isinstance(item, str) or not item for item in document_ids
            ):
                raise _invalid("scope", "scope.document_ids must be an array of strings")
            document_ids = tuple(str(item) for item in document_ids)
        return cls(space_ids=space_ids or (), document_ids=document_ids or ())

    def to_json(self) -> dict[str, list[str]]:
        value: dict[str, list[str]] = {}
        if self.space_ids:
            value["space_ids"] = list(self.space_ids)
        if self.document_ids:
            value["document_ids"] = list(self.document_ids)
        return value


@dataclass(frozen=True, slots=True)
class Citation:
    document_id: str
    document_version_id: str
    publication_id: str
    chunk_id: str
    locator: Mapping[str, Any]
    snippet: str | None

    def to_json(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "document_id": self.document_id,
            "document_version_id": self.document_version_id,
            "publication_id": self.publication_id,
            "chunk_id": self.chunk_id,
            "locator": dict(self.locator),
        }
        if self.snippet is not None:
            value["snippet"] = self.snippet
        return value


@dataclass(frozen=True, slots=True)
class Notice:
    kind: str
    detail: Mapping[str, Any]

    def to_json(self) -> dict[str, Any]:
        return {"kind": self.kind, "detail": dict(self.detail)}


@dataclass(frozen=True, slots=True)
class AskRequest:
    content: str
    effort_level: str
    scope: ConversationScope | None


@dataclass(frozen=True, slots=True)
class FeedbackRequest:
    vote: str
    down_reason: str | None


@dataclass(frozen=True, slots=True)
class AbVoteRequest:
    pair_id: str
    choice: str


def _invalid(field_name: str, message: str) -> PlatformError:
    return PlatformError(
        "validation_error",
        message,
        {"field": field_name},
        422,
    )


def validate_effort_level(value: Any) -> str:
    if value not in EFFORT_LEVELS:
        raise _invalid("effort_level", "effort_level must be quick, think or deep")
    return str(value)


def validate_ask_body(body: Any) -> AskRequest:
    if not isinstance(body, Mapping):
        raise _invalid("body", "Request body must be an object")
    content = body.get("content")
    if not isinstance(content, str) or not content.strip():
        raise _invalid("content", "content must be a non-empty string")
    effort_level = validate_effort_level(body.get("effort_level"))
    if "overrides" in body and body.get("overrides") is not None:
        raise _invalid("overrides", "overrides must be null")
    scope = ConversationScope.from_value(body.get("scope"))
    return AskRequest(content=content.strip(), effort_level=effort_level, scope=scope)


def validate_feedback_body(body: Any) -> FeedbackRequest:
    if not isinstance(body, Mapping):
        raise _invalid("body", "Request body must be an object")
    vote = body.get("vote")
    if vote not in FEEDBACK_VOTES:
        raise _invalid("vote", "vote must be 'up' or 'down'")
    down_reason = None
    if vote == "down":
        down_reason = body.get("reason")
        if down_reason not in FEEDBACK_DOWN_REASONS:
            raise _invalid("reason", "reason must be no_grounding or wrong_citation")
    return FeedbackRequest(vote=str(vote), down_reason=down_reason)


def validate_ab_vote_body(body: Any) -> AbVoteRequest:
    if not isinstance(body, Mapping):
        raise _invalid("body", "Request body must be an object")
    pair_id = body.get("pair_id")
    if not isinstance(pair_id, str) or not pair_id.strip():
        raise _invalid("pair_id", "pair_id must be a non-empty string")
    choice = body.get("choice")
    if choice not in AB_CHOICES:
        raise _invalid("choice", "choice must be '0', '1' or 'neither'")
    return AbVoteRequest(pair_id=str(pair_id), choice=str(choice))


def canonical_request_fingerprint(kind: str, payload: Mapping[str, Any]) -> str:
    import hashlib

    normalized = {"kind": kind, **{str(key): payload[key] for key in sorted(payload)}}
    serialized = json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class StoredEvent:
    seq: int
    event_type: str
    data: Mapping[str, Any]


def sse_frame(event_type: str, data: Mapping[str, Any], seq: int) -> str:
    payload = json.dumps(data, ensure_ascii=True, separators=(",", ":"))
    return f"id: {seq}\nevent: {event_type}\ndata: {payload}\n\n"


def sse_comment(text: str) -> str:
    return f": {text}\n\n"


def terminal_event_type(event_type: str) -> bool:
    return event_type in {"done", "error", "stopped"}


def datetime_to_rfc3339(value: datetime) -> str:
    return value.astimezone().replace(microsecond=0).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True, slots=True)
class CalibrationWindowSnapshot:
    window_id: str
    status: str
    policy_version: str
    sample_rate: float
    window_kind: str
    expires_at_utc: datetime | None = None
    close_deadline_at_utc: datetime | None = None
    pair_vote_ttl_seconds: int | None = None


# Fallback single-pair vote TTL used only when the calibration port snapshot
# does not carry the policy pair_vote_ttl_seconds (e.g. pre-evaluation-migration
# boots or fakes without the field).
AB_PAIR_OPEN_SECONDS = 3600


@dataclass(slots=True)
class ChatProviderResponse:
    content: str
    input_tokens: int
    output_tokens: int
    reasoning_tokens: int | None = None
    provider_request_id: str | None = None


@dataclass(frozen=True, slots=True)
class RetrievalHitOutcome:
    document_id: str
    document_version_id: str
    publication_id: str
    chunk_id: str
    space_id: str
    locator: Mapping[str, Any]
    snippet: str | None


@dataclass(frozen=True, slots=True)
class RetrievalOutcome:
    hits: tuple[RetrievalHitOutcome, ...]
    degradations: tuple[Mapping[str, Any], ...] = field(default_factory=tuple)
