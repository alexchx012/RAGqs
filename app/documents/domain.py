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
]
