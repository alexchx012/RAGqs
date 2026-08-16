"""Outbox-owned typed ports: transactional publish and lifecycle commands.

All consumers of the outbox go through these stable, typed boundaries. No
caller may query or mutate outbox tables directly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal, Protocol

from sqlalchemy.engine import Connection

RecipientKind = Literal["identity", "role_snapshot"]
EventType = Literal[
    "ingestion_completed",
    "ocr_low_confidence",
    "submission_approved",
    "submission_rejected",
    "submission_invalidated",
    "quota_approved",
    "quota_rejected",
    "calibration_window_suggested",
    "graph_build_completed",
    "evaluation_judge_configuration_missing",
    "public_graph_source_changed",
]

V1_CONSUMER = "in_app_notification"
NOTIFICATION_EVENT_TYPES: frozenset[str] = frozenset(
    {
        "ingestion_completed",
        "ocr_low_confidence",
        "submission_approved",
        "submission_rejected",
        "submission_invalidated",
        "quota_approved",
        "quota_rejected",
        "calibration_window_suggested",
        "graph_build_completed",
        "evaluation_judge_configuration_missing",
    }
)
ACKNOWLEDGEABLE_EVENT_TYPES: frozenset[str] = frozenset(
    {"ingestion_completed", "ocr_low_confidence"}
)
OUTBOX_ONLY_EVENT_TYPES: frozenset[str] = frozenset({"public_graph_source_changed"})


@dataclass(frozen=True, slots=True)
class RecipientSelection:
    recipient_user_id: str
    recipient_kind: RecipientKind = "identity"
    required_role: str | None = None
    selection_reason: str = "direct_operator"


@dataclass(frozen=True, slots=True)
class OutboxPublishCommand:
    event_id: str
    caller_principal: str
    event_type: EventType
    schema_version: int
    aggregate_type: str
    aggregate_id: str
    transition_version: int
    occurred_at: datetime
    payload: dict[str, object]
    recipients: tuple[RecipientSelection, ...]
    trace_id: str | None = None


@dataclass(frozen=True, slots=True)
class OutboxPublishReceipt:
    event_id: str
    fingerprint: str
    reused: bool


class PublicGraphSourceOutboxPort(Protocol):
    """Writes the source-change event owned by the documents transaction."""

    def publish_public_graph_source_change(
        self,
        *,
        source_revision: int,
        source_manifest_id: str,
        source_manifest_hash: str,
        document_id: str,
        change_type: str,
        occurred_at: datetime,
        connection: Connection,
    ) -> str: ...


class StartupConfigurationAlertPort(Protocol):
    """Publishes the bounded startup alert for missing evaluation judge configuration."""

    def publish_missing_evaluation_judge_configuration(
        self,
        *,
        missing_variable_names: tuple[str, ...],
        occurred_at: datetime,
        connection: Connection,
    ) -> str: ...


# ---------------------------------------------------------------------------
# Typed lifecycle commands
# ---------------------------------------------------------------------------

RedactMode = Literal["inline"]
RetirementMode = Literal["durable", "inline"]


@dataclass(frozen=True, slots=True)
class DocumentNotificationRedactionCommand:
    operation_id: str
    caller_principal: str
    deletion_id: str
    document_id: str
    document_version_ids: tuple[str, ...]
    reason: Literal["document_pending_delete"]
    transaction_id: str
    mode: Literal["inline"]
    canonical_input_fingerprint: str


@dataclass(frozen=True, slots=True)
class DocumentNotificationRedactionReceipt:
    operation_id: str
    deletion_id: str
    state: Literal["completed"]
    redacted_notification_count: int
    already_redacted_count: int
    retryable: bool = False


@dataclass(frozen=True, slots=True)
class AccountNotificationRetirementCommand:
    operation_id: str
    caller_principal: str
    user_id: str
    deletion_id: str
    verified_archive_ref: str
    archive_checksum: str
    transaction_id: str
    mode: RetirementMode
    canonical_input_fingerprint: str


@dataclass(frozen=True, slots=True)
class AccountNotificationRetirementReceipt:
    operation_id: str
    user_id: str
    deletion_id: str
    state: Literal["accepted", "completed"]
    receipt_count: int
    notification_retired_count: int
    inbox_removed: bool
    retryable: bool = False


@dataclass(frozen=True, slots=True)
class EligibleAccountEventCompactionCommand:
    operation_id: str
    caller_principal: str
    user_id: str
    deletion_id: str
    retirement_receipt_id: str
    retirement_receipt_fingerprint: str
    transaction_id: str
    canonical_input_fingerprint: str


@dataclass(frozen=True, slots=True)
class EligibleAccountEventCompactionReceipt:
    operation_id: str
    user_id: str
    deletion_id: str
    state: Literal["accepted", "completed"]
    eligible_count: int
    compacted_count: int
    blocked_count: int
    retryable: bool = False


class DocumentLifecyclePort(Protocol):
    """Boundary invoked by the documents deletion acceptance transaction."""

    def redact_document_notifications(
        self,
        command: DocumentNotificationRedactionCommand,
        *,
        connection: Connection,
    ) -> DocumentNotificationRedactionReceipt: ...


class AccountRetirementPort(Protocol):
    """Boundary invoked by retention-ops after identity-owned archive verification."""

    def retire_account_notification_state(
        self,
        command: AccountNotificationRetirementCommand,
        *,
        connection: Connection,
    ) -> AccountNotificationRetirementReceipt: ...


class EventCompactionPort(Protocol):
    """Boundary invoked by retention-ops after a completed retirement receipt."""

    def request_eligible_account_event_compaction(
        self,
        command: EligibleAccountEventCompactionCommand,
        *,
        connection: Connection,
    ) -> EligibleAccountEventCompactionReceipt: ...


# ---------------------------------------------------------------------------
# Dispatcher-facing models
# ---------------------------------------------------------------------------

DeliveryStatus = Literal["pending", "running", "retry_wait", "delivered", "dead_letter"]
AttemptStatus = Literal["running", "delivered", "failed", "expired"]
ErrorCategory = Literal["retryable", "permanent", "lease_expired"]


@dataclass(frozen=True, slots=True)
class DeliveryMaterialization:
    event_id: str
    recipient_user_id: str
    notification_id: str
    notification_type: str
    title: str
    payload: dict[str, object]
    notification_seq: int
    read_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class DeliveryOutcome:
    status: Literal["delivered", "failed", "expired", "dead_letter"]
    error_category: ErrorCategory | None = None
    error_code: str | None = None
    retry_after_seconds: int | None = None


@dataclass(frozen=True, slots=True)
class DeliveryClaim:
    event_id: str
    consumer_name: str
    version: int
    replay_generation: int
    attempt_number: int
    cycle_attempt_number: int
    fence_token: int
    lease_expires_at: datetime
    started_at: datetime
    attempt_id: str
    payload: dict[str, object] | None
    notification_type: str | None
    title: str | None
    redacted_title: str
    document_id: str | None
    document_version_id: str | None
    recipients: tuple[DeliveryMaterialization, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class OpsDeliveryView:
    event_id: str
    consumer_name: str
    status: str
    version: int
    replay_generation: int
    attempt_number: int
    error_category: str | None
    error_code: str | None
    replayable: bool
    next_attempt_at: datetime | None
    lease_expires_at: datetime | None


@dataclass(frozen=True, slots=True)
class OpsReplayReceipt:
    event_id: str
    consumer_name: str
    status: Literal["pending"]
    replay_generation: int
    version: int
