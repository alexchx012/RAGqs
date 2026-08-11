from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from typing import Any


class DocumentLifecycle(StrEnum):
    ACTIVE = "active"
    PENDING_DELETE = "pending_delete"
    DELETED = "deleted"


class DocumentVersionState(StrEnum):
    PENDING = "pending"
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    PURGING = "purging"
    PURGED = "purged"


class PublicationState(StrEnum):
    STAGED = "staged"
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    DISCARDED = "discarded"


class IngestionJobState(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    RETRY_WAIT = "retry_wait"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    DEAD_LETTER = "dead_letter"


class IngestionAttemptState(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


class SubmissionState(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    WITHDRAWN = "withdrawn"
    INVALIDATED = "invalidated"


_DOCUMENT_TRANSITIONS = {
    DocumentLifecycle.ACTIVE: frozenset({DocumentLifecycle.PENDING_DELETE}),
    DocumentLifecycle.PENDING_DELETE: frozenset({DocumentLifecycle.DELETED}),
    DocumentLifecycle.DELETED: frozenset(),
}
_PUBLICATION_TRANSITIONS = {
    PublicationState.STAGED: frozenset({PublicationState.ACTIVE, PublicationState.DISCARDED}),
    PublicationState.ACTIVE: frozenset({PublicationState.SUPERSEDED}),
    PublicationState.SUPERSEDED: frozenset(),
    PublicationState.DISCARDED: frozenset(),
}
_JOB_TRANSITIONS = {
    IngestionJobState.PENDING: frozenset(
        {IngestionJobState.RUNNING, IngestionJobState.CANCELLED}
    ),
    IngestionJobState.RUNNING: frozenset(
        {
            IngestionJobState.RETRY_WAIT,
            IngestionJobState.SUCCEEDED,
            IngestionJobState.FAILED,
            IngestionJobState.CANCELLED,
        }
    ),
    IngestionJobState.RETRY_WAIT: frozenset(
        {IngestionJobState.RUNNING, IngestionJobState.CANCELLED, IngestionJobState.DEAD_LETTER}
    ),
    IngestionJobState.SUCCEEDED: frozenset(),
    IngestionJobState.FAILED: frozenset(),
    IngestionJobState.CANCELLED: frozenset(),
    IngestionJobState.DEAD_LETTER: frozenset(),
}
_SUBMISSION_TRANSITIONS = {
    SubmissionState.PENDING: frozenset(
        {
            SubmissionState.APPROVED,
            SubmissionState.REJECTED,
            SubmissionState.WITHDRAWN,
            SubmissionState.INVALIDATED,
        }
    ),
    SubmissionState.APPROVED: frozenset(),
    SubmissionState.REJECTED: frozenset(),
    SubmissionState.WITHDRAWN: frozenset(),
    SubmissionState.INVALIDATED: frozenset(),
}


def _value(value: Any) -> str:
    return str(value.value if isinstance(value, StrEnum) else value)


def _require_transition(
    kind: str,
    transitions: dict[StrEnum, frozenset[StrEnum]],
    current: Any,
    target: Any,
) -> bool:
    current_value = _value(current)
    target_value = _value(target)
    allowed = {
        _value(source): frozenset(_value(destination) for destination in destinations)
        for source, destinations in transitions.items()
    }
    if target_value not in allowed.get(current_value, frozenset()):
        raise ValueError(f"{kind} transition {current_value!r} -> {target_value!r} is not allowed")
    return True


def require_document_lifecycle_transition(current: Any, target: Any) -> bool:
    return _require_transition("document lifecycle", _DOCUMENT_TRANSITIONS, current, target)


def require_publication_transition(current: Any, target: Any) -> bool:
    return _require_transition("publication", _PUBLICATION_TRANSITIONS, current, target)


def require_job_transition(current: Any, target: Any) -> bool:
    return _require_transition("job", _JOB_TRANSITIONS, current, target)


def require_submission_transition(current: Any, target: Any) -> bool:
    return _require_transition("submission", _SUBMISSION_TRANSITIONS, current, target)


def canonical_request_fingerprint(value: Any) -> str:
    """Create the stable idempotency fingerprint for normalized command input."""

    encoded = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


__all__ = [
    "DocumentLifecycle",
    "DocumentVersionState",
    "IngestionAttemptState",
    "IngestionJobState",
    "PublicationState",
    "SubmissionState",
    "canonical_request_fingerprint",
    "require_document_lifecycle_transition",
    "require_job_transition",
    "require_publication_transition",
    "require_submission_transition",
]
