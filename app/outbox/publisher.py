"""Transactional outbox publisher for notification-producing state transitions.

The publisher enforces the producer matrix (which trusted caller may emit
which event_type/aggregate), strict per-event payload schemas (extra fields and
sensitive content are rejected), recipient rules per event type, and
database-time bookkeeping columns. The unique event key
(event_type, aggregate_type, aggregate_id, transition_version) reuses an
existing event only when the payload fingerprint matches; a fingerprint
mismatch is a high-priority invariant alert.
"""

from __future__ import annotations

import hashlib
import json
import logging
import secrets
from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import IntegrityError

from app.identity.schema import identity_user_table
from app.platform.context import current_context
from app.platform.errors import PlatformError

from .ports import (
    NOTIFICATION_EVENT_TYPES,
    OUTBOX_ONLY_EVENT_TYPES,
    V1_CONSUMER,
    OutboxPublishCommand,
    OutboxPublishReceipt,
    RecipientSelection,
)
from .schema import (
    outbox_delivery_table,
    outbox_event_table,
    outbox_recipient_table,
)

_logger = logging.getLogger(__name__)

# event_type -> (allowed caller principals, required aggregate_type)
SUPPORTED_EVENT_SCHEMA_VERSION = 1

# event_type -> (allowed caller principals, required aggregate_type)
PRODUCER_MATRIX: dict[str, tuple[frozenset[str], str]] = {
    "ingestion_completed": (frozenset({"ingestion"}), "ingestion_job"),
    "ocr_low_confidence": (frozenset({"ingestion"}), "ingestion_job"),
    "submission_approved": (frozenset({"submissions"}), "knowledge_submission"),
    "submission_rejected": (frozenset({"submissions"}), "knowledge_submission"),
    "submission_invalidated": (frozenset({"submissions"}), "knowledge_submission"),
    "quota_approved": (frozenset({"quota"}), "quota_request"),
    "quota_rejected": (frozenset({"quota"}), "quota_request"),
    "calibration_window_suggested": (
        frozenset({"calibration"}),
        "calibration_window_suggestion",
    ),
    "graph_build_completed": (frozenset({"knowledge_graph"}), "graph_build_run"),
    "evaluation_judge_configuration_missing": (
        frozenset({"startup_configuration"}),
        "startup_invocation",
    ),
    "public_graph_source_changed": (frozenset({"documents"}), "public_graph_source"),
}

# strict field -> type map per event type; None allows an explicit null value.
PAYLOAD_SCHEMAS: dict[str, dict[str, type | tuple[type, None]]] = {
    "ingestion_completed": {
        "job_id": str,
        "document_id": str,
        "document_version_id": str,
        "publication_id": str,
    },
    "ocr_low_confidence": {
        "job_id": str,
        "document_id": str,
        "document_version_id": str,
        "publication_id": str,
        "reason": str,
        "status": str,
        "machine_low_confidence_fact": dict,
    },
    "submission_approved": {"submission_id": str},
    "submission_rejected": {"submission_id": str},
    "submission_invalidated": {"submission_id": str},
    "quota_approved": {"request_id": str},
    "quota_rejected": {"request_id": str},
    "calibration_window_suggested": {"calibration_window_suggestion_id": str},
    "graph_build_completed": {
        "schema_version": int,
        "event_id": str,
        "event_type": str,
        "aggregate_type": str,
        "aggregate_id": str,
        "transition_version": int,
        "graph_build_id": str,
        "status": str,
        "source_revision": int,
        "graph_generation_id": (str, None),
        "index_generation_id": (str, None),
        "failure_class": (str, None),
        "occurred_at": str,
    },
    "evaluation_judge_configuration_missing": {"missing_variable_names": list},
    "public_graph_source_changed": {
        "source_revision": int,
        "source_manifest_id": str,
        "source_manifest_hash": str,
        "document_id": str,
        "change_type": str,
    },
}

# Recipient rules: active-ops alert events take all active ops as role snapshots;
# graph events notify exactly one identity (the run initiator).
ROLE_SNAPSHOT_EVENT_TYPES: frozenset[str] = frozenset(
    {"calibration_window_suggested", "evaluation_judge_configuration_missing"}
)
SINGLE_RECIPIENT_EVENT_TYPES: frozenset[str] = frozenset({"graph_build_completed"})


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _canonical_graph_payload(command: OutboxPublishCommand) -> dict[str, object]:
    envelope = {
        "schema_version": command.schema_version,
        "event_id": command.event_id,
        "event_type": command.event_type,
        "aggregate_type": command.aggregate_type,
        "aggregate_id": command.aggregate_id,
        "transition_version": command.transition_version,
        "occurred_at": _utc(command.occurred_at).isoformat(),
    }
    for field, expected in envelope.items():
        supplied = command.payload.get(field)
        if supplied is not None and supplied != expected:
            raise PlatformError(
                "invalid_event_payload",
                "Graph event payload envelope does not match the outbox command",
                {"event_type": command.event_type, "field": field},
                422,
            )
    return {**command.payload, **envelope}


def fingerprint_event(
    event_type: str,
    schema_version: int,
    payload: Mapping[str, object],
) -> str:
    encoded = json.dumps(
        dict(payload),
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    digest = hashlib.sha256(
        f"outbox-event-v1\0{event_type}\0{schema_version}\0".encode() + encoded
    ).hexdigest()
    return digest


def _validate_payload(event_type: str, schema_version: int, payload: Mapping[str, object]) -> None:
    if schema_version != SUPPORTED_EVENT_SCHEMA_VERSION:
        raise PlatformError(
            "unsupported_schema_version",
            f"Event schema version {schema_version} is not supported",
            {"event_type": event_type, "supported_version": SUPPORTED_EVENT_SCHEMA_VERSION},
            422,
        )
    schema = PAYLOAD_SCHEMAS.get(event_type)
    if schema is None:
        raise PlatformError(
            "unsupported_event_type",
            f"Event type {event_type} is not supported by the outbox",
            {},
            422,
        )
    extra = [key for key in payload if key not in schema]
    if extra:
        raise PlatformError(
            "invalid_event_payload",
            "Event payload contains fields outside the event schema",
            {"event_type": event_type, "extra_fields": extra},
            422,
        )
    _reject_sensitive_content(payload)
    for field, expected in schema.items():
        value = payload.get(field)
        if value is None:
            if expected is None or isinstance(expected, tuple):
                continue
            raise PlatformError(
                "invalid_event_payload",
                "Event payload is missing required fields",
                {"event_type": event_type, "missing_fields": [field]},
                422,
            )
        expected_type = expected[0] if isinstance(expected, tuple) else expected
        if not isinstance(value, expected_type):
            raise PlatformError(
                "invalid_event_payload",
                "Event payload field has an invalid type",
                {"event_type": event_type, "field": field},
                422,
            )
    if event_type == "ocr_low_confidence":
        fact = payload.get("machine_low_confidence_fact")
        if not isinstance(fact, dict):
            raise PlatformError(
                "invalid_event_payload",
                "ocr_low_confidence requires a machine low-confidence fact object",
                {"event_type": event_type},
                422,
            )
        _reject_sensitive_content(fact)
        allowed_fact_fields = {"confidence", "page", "region"}
        unexpected = [key for key in fact if key not in allowed_fact_fields]
        if unexpected:
            raise PlatformError(
                "invalid_event_payload",
                "OCR low-confidence fact contains fields outside the closed schema",
                {"event_type": event_type, "extra_fields": unexpected},
                422,
            )
        for fact_field in allowed_fact_fields:
            if fact.get(fact_field) is None:
                raise PlatformError(
                    "invalid_event_payload",
                    "OCR low-confidence fact is missing required fields",
                    {"event_type": event_type, "missing_fields": [fact_field]},
                    422,
                )
        if not isinstance(fact["confidence"], (int, float)) or isinstance(fact["confidence"], bool):
            raise PlatformError(
                "invalid_event_payload",
                "OCR confidence must be numeric",
                {"event_type": event_type},
                422,
            )
        confidence = fact["confidence"]
        if not __import__("math").isfinite(confidence) or not 0 <= confidence <= 1:
            raise PlatformError(
                "invalid_event_payload",
                "OCR confidence must be a finite number between 0 and 1",
                {"event_type": event_type},
                422,
            )
        if not isinstance(fact["page"], int) or isinstance(fact["page"], bool):
            raise PlatformError(
                "invalid_event_payload",
                "OCR page must be an integer",
                {"event_type": event_type},
                422,
            )
        region = fact.get("region")
        if not isinstance(region, list) or not all(
            isinstance(item, int) and not isinstance(item, bool) for item in region
        ):
            raise PlatformError(
                "invalid_event_payload",
                "OCR region must be a list of integers",
                {"event_type": event_type},
                422,
            )
    if event_type == "graph_build_completed":
        status = payload.get("status")
        if status not in {"succeeded", "failed", "cancelled"}:
            raise PlatformError(
                "invalid_event_payload",
                "graph_build_completed status is invalid",
                {"event_type": event_type},
                422,
            )
        if status == "succeeded" and (
            payload.get("graph_generation_id") is None or payload.get("index_generation_id") is None
        ):
            raise PlatformError(
                "invalid_event_payload",
                "succeeded graph build events require both generation identifiers",
                {"event_type": event_type},
                422,
            )
        if status != "succeeded" and (
            payload.get("graph_generation_id") is not None
            or payload.get("index_generation_id") is not None
        ):
            raise PlatformError(
                "invalid_event_payload",
                "non-succeeded graph build events must not carry generation identifiers",
                {"event_type": event_type},
                422,
            )
        failure_class = payload.get("failure_class")
        if (status == "succeeded" and failure_class is not None) or (
            status != "succeeded" and failure_class is None
        ):
            raise PlatformError(
                "invalid_event_payload",
                "graph_build_completed failure_class does not match status",
                {"event_type": event_type},
                422,
            )
        if failure_class is not None and failure_class not in {
            "staging_error",
            "index_error",
            "cancelled",
            "graph_source_changed",
            "graph_stage_grant_expired",
            "graph_component_stage_failed",
            "graph_release_failed",
            "graph_provider_failed",
            "graph_worker_unexpected",
            "cancel_requested",
        }:
            raise PlatformError(
                "invalid_event_payload",
                "graph_build_completed failure_class is invalid",
                {"event_type": event_type},
                422,
            )
    if event_type == "evaluation_judge_configuration_missing":
        missing = payload.get("missing_variable_names")
        allowed = {
            "RAG_EVALUATION_JUDGE_BASE_URL",
            "RAG_EVALUATION_JUDGE_API_KEY",
        }
        if (
            not isinstance(missing, list)
            or not missing
            or any(not isinstance(name, str) or name not in allowed for name in missing)
            or len(set(missing)) != len(missing)
        ):
            raise PlatformError(
                "invalid_event_payload",
                "Evaluation judge alerts may contain only unique missing variable names",
                {"event_type": event_type},
                422,
            )


_SENSITIVE_KEYS = frozenset(
    {
        "snippet",
        "filename",
        "file_name",
        "title",
        "display_name",
        "real_name",
        "username",
        "user_name",
        "body",
        "content",
        "text",
        "credentials",
        "password",
        "secret",
        "token",
        "api_key",
    }
)


def _reject_sensitive_content(value: object, *, path: str = "payload") -> None:
    """Recursively reject free text, provider content or credentials.

    All JSON containers are canonicalized to dict/list before descending, so
    tuples/sets/frozensets cannot smuggle sensitive keys.
    """
    if isinstance(value, dict):
        for key, item in value.items():
            if key.casefold() in _SENSITIVE_KEYS:
                raise PlatformError(
                    "invalid_event_payload",
                    "Event payload must not contain sensitive or free-text fields",
                    {"event_type": "payload", "field": f"{path}.{key}"},
                    422,
                )
            _reject_sensitive_content(item, path=f"{path}.{key}")
    elif isinstance(value, (list, tuple, set, frozenset)):
        for index, item in enumerate(value):
            _reject_sensitive_content(item, path=f"{path}[{index}]")


class SqlAlchemyOutboxPublisher:
    """Writes event, recipients and initial deliveries inside the caller transaction.

    # Calibration events freeze the set of active ops inside this transaction.
    # graph events verify the persisted activated receipt through the injected
    # port. Runtime-owned domain integrations use fixed caller identities.
    #
    # Producer wiring note: runtime-owned domain integrations use only narrow
    # operation-scoped facades; the quota facade below cannot choose a caller,
    # event family, aggregate family, recipient kind, or transaction.
    """

    def __init__(
        self,
        engine: Engine,
        *,
        clock: Any = None,
        now: Callable[[], datetime] | None = None,
        graph_activated_receipt_port: Any = None,
        retention_days: int = 30,
    ) -> None:
        self._engine = engine
        self._clock = clock
        self._now = now
        self._graph_activated_receipt_port = graph_activated_receipt_port
        self._retention_days = retention_days

    def _database_now(self, connection: Connection) -> datetime:
        if self._clock is not None:
            value = self._clock.now_utc(connection)
            if isinstance(value, datetime):
                return _utc(value)
        if self._now is not None:
            return _utc(self._now())
        return _utc(datetime.now(UTC))

    def _publish_authorized(
        self,
        command: OutboxPublishCommand,
        *,
        connection: Connection,
        caller: str,
    ) -> OutboxPublishReceipt:
        """Persist one already assembly-scoped command in the caller transaction."""
        if command.event_type == "graph_build_completed":
            command = replace(command, payload=_canonical_graph_payload(command))
        _validate_payload(command.event_type, command.schema_version, command.payload)
        if command.event_type not in NOTIFICATION_EVENT_TYPES | OUTBOX_ONLY_EVENT_TYPES:
            raise PlatformError(
                "unsupported_event_type",
                f"Event type {command.event_type} is not a notification event",
                {},
                422,
            )
        producers, required_aggregate = PRODUCER_MATRIX[command.event_type]
        if caller not in producers:
            raise PlatformError(
                "producer_not_authorized",
                f"Caller {caller} may not publish {command.event_type} events",
                {"event_type": command.event_type},
                403,
            )
        if command.aggregate_type != required_aggregate:
            raise PlatformError(
                "invalid_aggregate_type",
                f"{command.event_type} events require aggregate {required_aggregate}",
                {},
                422,
            )
        # Graph success events require the persisted activated receipt.
        if (
            command.event_type == "graph_build_completed"
            and command.payload.get("status") == "succeeded"
        ):
            self._assert_graph_activated_receipt(connection, command)
        command = self._freeze_active_ops_recipients(connection, command)
        self._validate_recipients(command)

        fingerprint = fingerprint_event(command.event_type, command.schema_version, command.payload)

        # Atomic conflict/re-read on the unique event key: reuse the real row.
        unique = (
            connection.execute(
                select(
                    outbox_event_table.c.event_id,
                    outbox_event_table.c.payload_fingerprint,
                ).where(
                    outbox_event_table.c.event_type == command.event_type,
                    outbox_event_table.c.aggregate_type == command.aggregate_type,
                    outbox_event_table.c.aggregate_id == command.aggregate_id,
                    outbox_event_table.c.transition_version == command.transition_version,
                )
            )
            .mappings()
            .one_or_none()
        )
        if unique is not None:
            if unique["payload_fingerprint"] != fingerprint:
                self._alert_fingerprint_conflict(command, fingerprint)
                raise PlatformError(
                    "outbox_fingerprint_conflict",
                    "Event fingerprint conflicts with the existing transition",
                    {"event_id": command.event_id},
                    409,
                )
            return OutboxPublishReceipt(
                event_id=str(unique["event_id"]),
                fingerprint=fingerprint,
                reused=True,
            )

        existing = (
            connection.execute(
                select(outbox_event_table.c.event_id).where(
                    outbox_event_table.c.event_id == command.event_id
                )
            )
            .mappings()
            .one_or_none()
        )
        if existing is not None:
            raise PlatformError(
                "event_id_conflict",
                "An event with this event_id already exists",
                {},
                409,
            )

        now = self._database_now(connection)
        occurred_at = _utc(command.occurred_at)
        recipients = [
            self._recipient_record(connection, command, selection, now=now)
            for selection in command.recipients
        ]
        try:
            with connection.begin_nested():
                self._insert_event_row(
                    connection, command, occurred_at, fingerprint, created_at=now
                )
        except IntegrityError:
            # A concurrent publisher committed the same event key between our
            # read and insert (savepoint rolled back, outer tx still usable):
            # re-read and reuse the real row. Any OTHER integrity violation is
            # re-raised unchanged.
            concurrent = (
                connection.execute(
                    select(
                        outbox_event_table.c.event_id,
                        outbox_event_table.c.payload_fingerprint,
                    ).where(
                        outbox_event_table.c.event_type == command.event_type,
                        outbox_event_table.c.aggregate_type == command.aggregate_type,
                        outbox_event_table.c.aggregate_id == command.aggregate_id,
                        outbox_event_table.c.transition_version == command.transition_version,
                    )
                )
                .mappings()
                .one_or_none()
            )
            if concurrent is not None:
                if concurrent["payload_fingerprint"] != fingerprint:
                    self._alert_fingerprint_conflict(command, fingerprint)
                    raise PlatformError(
                        "outbox_fingerprint_conflict",
                        "Event fingerprint conflicts with the existing transition",
                        {"event_id": command.event_id},
                        409,
                    ) from None
                return OutboxPublishReceipt(
                    event_id=str(concurrent["event_id"]),
                    fingerprint=fingerprint,
                    reused=True,
                )
            raise
        if recipients:
            connection.execute(outbox_recipient_table.insert().values(recipients))
        if recipients and command.event_type not in OUTBOX_ONLY_EVENT_TYPES:
            connection.execute(
                outbox_delivery_table.insert().values(
                    event_id=command.event_id,
                    consumer_name=V1_CONSUMER,
                    status="pending",
                    version=1,
                    replay_generation=1,
                    attempt_number=0,
                    cycle_attempt_number=0,
                    error_category=None,
                    error_code=None,
                    next_attempt_at_utc=now,
                    lease_owner=None,
                    lease_expires_at_utc=None,
                    fence_token=None,
                    delivered_at_utc=None,
                )
            )
        return OutboxPublishReceipt(
            event_id=command.event_id,
            fingerprint=fingerprint,
            reused=False,
        )

    def _assert_graph_activated_receipt(
        self,
        connection: Connection,
        command: OutboxPublishCommand,
    ) -> None:
        """Graph success events require the persisted activated receipt with its
        external graph_generation_id. The check goes through the injected
        indexing port (fail-closed when unconfigured)."""
        port = self._graph_activated_receipt_port
        if port is None:
            raise PlatformError(
                "graph_receipt_verification_unavailable",
                "Graph activated-receipt verification is not configured",
                {"retryable": True},
                503,
            )
        verify = getattr(port, "verify_activated_receipt", None)
        if not callable(verify):
            raise PlatformError(
                "graph_receipt_verification_unavailable",
                "Graph activated-receipt verification is not configured",
                {"retryable": True},
                503,
            )
        if not verify(
            aggregate_id=command.aggregate_id,
            graph_generation_id=str(command.payload.get("graph_generation_id") or ""),
            connection=connection,
        ):
            raise PlatformError(
                "graph_receipt_not_activated",
                "The graph run has no activated indexing receipt",
                {"aggregate_id": command.aggregate_id},
                409,
            )

    def _freeze_active_ops_recipients(
        self,
        connection: Connection,
        command: OutboxPublishCommand,
    ) -> OutboxPublishCommand:
        """Alert events freeze ALL active ops inside this transaction."""
        if command.event_type not in ROLE_SNAPSHOT_EVENT_TYPES:
            return command
        ops = (
            connection.execute(
                select(identity_user_table.c.id).where(
                    identity_user_table.c.lifecycle_status == "active",
                    identity_user_table.c.role == "ops",
                )
            )
            .scalars()
            .all()
        )
        recipients = tuple(
            RecipientSelection(
                recipient_user_id=str(user_id),
                recipient_kind="role_snapshot",
                required_role="ops",
                selection_reason="active_ops_snapshot",
            )
            for user_id in ops
        )
        return OutboxPublishCommand(
            event_id=command.event_id,
            caller_principal=command.caller_principal,
            event_type=command.event_type,
            schema_version=command.schema_version,
            aggregate_type=command.aggregate_type,
            aggregate_id=command.aggregate_id,
            transition_version=command.transition_version,
            occurred_at=command.occurred_at,
            payload=command.payload,
            recipients=recipients,
            trace_id=command.trace_id,
        )

    def _insert_event_row(
        self,
        connection: Connection,
        command: OutboxPublishCommand,
        occurred_at: datetime,
        fingerprint: str,
        created_at: datetime,
    ) -> None:
        compact_after = (
            created_at + timedelta(days=self._retention_days)
            if command.event_type in OUTBOX_ONLY_EVENT_TYPES
            else None
        )
        connection.execute(
            outbox_event_table.insert().values(
                event_id=command.event_id,
                event_type=command.event_type,
                schema_version=command.schema_version,
                aggregate_type=command.aggregate_type,
                aggregate_id=command.aggregate_id,
                transition_version=command.transition_version,
                occurred_at_utc=occurred_at,
                payload_json=dict(command.payload),
                payload_fingerprint=fingerprint,
                trace_id=command.trace_id,
                created_at_utc=created_at,
                storage_state="full",
                compact_after_at_utc=compact_after,
                compacted_at_utc=None,
                compacted_delivery_summary_json=None,
            )
        )

    def _alert_fingerprint_conflict(
        self,
        command: OutboxPublishCommand,
        fingerprint: str,
    ) -> None:
        _logger.error(
            "outbox fingerprint conflict event_id=%s fingerprint=%s event_type=%s "
            "aggregate=%s/%s transition=%s",
            command.event_id,
            fingerprint,
            command.event_type,
            command.aggregate_type,
            command.aggregate_id,
            command.transition_version,
        )

    def _validate_recipients(self, command: OutboxPublishCommand) -> None:
        if command.event_type in OUTBOX_ONLY_EVENT_TYPES:
            if command.recipients:
                raise PlatformError(
                    "invalid_recipients",
                    "Public graph source events do not have notification recipients",
                    {},
                    422,
                )
            return
        if not command.recipients and command.event_type not in ROLE_SNAPSHOT_EVENT_TYPES:
            raise PlatformError(
                "invalid_recipients",
                "At least one recipient is required",
                {},
                422,
            )
        if command.event_type in SINGLE_RECIPIENT_EVENT_TYPES and len(command.recipients) != 1:
            raise PlatformError(
                "invalid_recipients",
                "graph_build_completed events notify exactly one identity",
                {},
                422,
            )
        for selection in command.recipients:
            if command.event_type in ROLE_SNAPSHOT_EVENT_TYPES:
                if selection.recipient_kind != "role_snapshot" or selection.required_role != "ops":
                    raise PlatformError(
                        "invalid_recipients",
                        "This event requires active ops role snapshots",
                        {},
                        422,
                    )
            elif selection.recipient_kind != "identity":
                raise PlatformError(
                    "invalid_recipients",
                    f"{command.event_type} events require identity recipients",
                    {},
                    422,
                )

    @staticmethod
    def _recipient_record(
        connection: Connection,
        command: OutboxPublishCommand,
        selection: RecipientSelection,
        *,
        now: datetime,
    ) -> dict[str, object]:
        if selection.recipient_kind not in {"identity", "role_snapshot"}:
            raise PlatformError(
                "invalid_recipient_kind",
                "Recipient kind is invalid",
                {},
                422,
            )
        account = (
            connection.execute(
                select(
                    identity_user_table.c.id,
                    identity_user_table.c.role,
                    identity_user_table.c.lifecycle_status,
                ).where(identity_user_table.c.id == selection.recipient_user_id)
            )
            .mappings()
            .one_or_none()
        )
        if account is None:
            raise PlatformError(
                "recipient_account_inactive",
                "Recipient account is not active",
                {"recipient_user_id": selection.recipient_user_id},
                422,
            )
        if (
            selection.recipient_kind == "role_snapshot"
            and selection.required_role is not None
            and account["role"] != selection.required_role
        ):
            raise PlatformError(
                "recipient_role_mismatch",
                "Recipient no longer satisfies the required role",
                {"recipient_user_id": selection.recipient_user_id},
                422,
            )
        return {
            "event_id": command.event_id,
            "recipient_user_id": selection.recipient_user_id,
            "recipient_kind": selection.recipient_kind,
            "required_role": selection.required_role,
            "selection_reason": selection.selection_reason,
            "role_snapshot": account["role"],
            "lifecycle_snapshot": account["lifecycle_status"],
            "selected_at_utc": now,
        }


class SqlAlchemyQuotaOutboxEnqueueAdapter:
    """Quota-request scoped, no-token façade over the outbox-owned publisher.

    The usage service supplies its caller transaction, frozen applicant, transition
    time and payload. This façade fixes the trusted principal, event/aggregate family,
    recipient kind and schema version; callers cannot turn it into a generic producer.
    """

    _EVENT_TYPES = frozenset({"quota_approved", "quota_rejected"})

    def __init__(self, publisher: SqlAlchemyOutboxPublisher) -> None:
        self._publisher = publisher

    @staticmethod
    def _event_id(
        *,
        event_type: str,
        aggregate_id: str,
        transition_version: int,
        payload_fingerprint: str,
    ) -> str:
        canonical = json.dumps(
            {
                "aggregate_id": aggregate_id,
                "event_type": event_type,
                "payload_fingerprint": payload_fingerprint,
                "transition_version": transition_version,
            },
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        digest = hashlib.sha256(b"quota-outbox-event-v1\0" + canonical).hexdigest()
        return f"evt_{digest[:60]}"

    def enqueue(
        self,
        *,
        connection: Connection,
        event_type: Literal["quota_approved", "quota_rejected"],
        aggregate_type: Literal["quota_request"],
        aggregate_id: str,
        transition_version: int,
        recipient_user_id: str,
        occurred_at: datetime,
        payload_fingerprint: str,
        payload: dict,
    ) -> None:
        if event_type not in self._EVENT_TYPES or aggregate_type != "quota_request":
            raise PlatformError(
                "invalid_quota_outbox_event",
                "Quota outbox events must use the quota request event family",
                {},
                422,
            )
        if not isinstance(payload_fingerprint, str) or not payload_fingerprint:
            raise PlatformError(
                "invalid_quota_outbox_event",
                "Quota outbox payload fingerprint is required",
                {},
                422,
            )
        context = current_context()
        command = OutboxPublishCommand(
            event_id=self._event_id(
                event_type=event_type,
                aggregate_id=aggregate_id,
                transition_version=transition_version,
                payload_fingerprint=payload_fingerprint,
            ),
            caller_principal="quota",
            event_type=event_type,
            schema_version=SUPPORTED_EVENT_SCHEMA_VERSION,
            aggregate_type=aggregate_type,
            aggregate_id=aggregate_id,
            transition_version=transition_version,
            occurred_at=occurred_at,
            payload=dict(payload),
            recipients=(
                RecipientSelection(
                    recipient_user_id=recipient_user_id,
                    recipient_kind="identity",
                    selection_reason="quota_request_applicant",
                ),
            ),
            trace_id=context.trace_id if context is not None else None,
        )
        self._publisher._publish_authorized(
            command,
            connection=connection,
            caller="quota",
        )


class SqlAlchemySubmissionOutboxAdapter:
    """Submission-scoped, no-token facade over the outbox publisher."""

    _EVENT_TYPES = frozenset(
        {"submission_approved", "submission_rejected", "submission_invalidated"}
    )

    def __init__(self, publisher: SqlAlchemyOutboxPublisher) -> None:
        self._publisher = publisher

    @staticmethod
    def _event_id(*, event_type: str, submission_id: str, transition_version: int) -> str:
        return f"evt_submission_{event_type}_{submission_id}_{transition_version}"

    def publish_submission_event(
        self,
        *,
        event_type: Literal["submission_approved", "submission_rejected", "submission_invalidated"],
        submission_id: str,
        transition_version: int,
        recipient_user_id: str,
        occurred_at: datetime,
        connection: Connection,
    ) -> str:
        if event_type not in self._EVENT_TYPES:
            raise PlatformError(
                "invalid_submission_outbox_event",
                "Submission events must use the submission event family",
                {},
                422,
            )
        context = current_context()
        command = OutboxPublishCommand(
            event_id=self._event_id(
                event_type=event_type,
                submission_id=submission_id,
                transition_version=transition_version,
            ),
            caller_principal="submissions",
            event_type=event_type,
            schema_version=SUPPORTED_EVENT_SCHEMA_VERSION,
            aggregate_type="knowledge_submission",
            aggregate_id=submission_id,
            transition_version=transition_version,
            occurred_at=occurred_at,
            payload={"submission_id": submission_id},
            recipients=(
                RecipientSelection(
                    recipient_user_id=recipient_user_id,
                    recipient_kind="identity",
                    selection_reason="knowledge_submission_participant",
                ),
            ),
            trace_id=context.trace_id if context is not None else None,
        )
        self._publisher._publish_authorized(
            command,
            connection=connection,
            caller="submissions",
        )
        return command.event_id


class SqlAlchemyStartupConfigurationAlertAdapter:
    """Startup-scoped, no-token facade for missing evaluation judge settings."""

    _ALLOWED_VARIABLE_NAMES = frozenset(
        {"RAG_EVALUATION_JUDGE_BASE_URL", "RAG_EVALUATION_JUDGE_API_KEY"}
    )

    def __init__(self, publisher: SqlAlchemyOutboxPublisher) -> None:
        self._publisher = publisher

    def publish_missing_evaluation_judge_configuration(
        self,
        *,
        missing_variable_names: tuple[str, ...],
        occurred_at: datetime,
        connection: Connection,
    ) -> str:
        if (
            not missing_variable_names
            or len(set(missing_variable_names)) != len(missing_variable_names)
            or not set(missing_variable_names).issubset(self._ALLOWED_VARIABLE_NAMES)
        ):
            raise PlatformError(
                "invalid_startup_configuration_alert",
                "Only missing evaluation judge variable names may be published",
                {},
                422,
            )
        invocation_id = secrets.token_urlsafe(18)
        context = current_context()
        command = OutboxPublishCommand(
            event_id=f"evt_eval_judge_config_missing_{invocation_id}",
            caller_principal="startup_configuration",
            event_type="evaluation_judge_configuration_missing",
            schema_version=SUPPORTED_EVENT_SCHEMA_VERSION,
            aggregate_type="startup_invocation",
            aggregate_id=f"startup_{invocation_id}",
            transition_version=1,
            occurred_at=occurred_at,
            payload={"missing_variable_names": list(missing_variable_names)},
            recipients=(),
            trace_id=context.trace_id if context is not None else None,
        )
        self._publisher._publish_authorized(
            command,
            connection=connection,
            caller="startup_configuration",
        )
        return command.event_id


class SqlAlchemyPublicGraphSourceOutboxAdapter:
    """Documents-scoped outbox facade for immutable public source changes."""

    def __init__(self, publisher: SqlAlchemyOutboxPublisher) -> None:
        self._publisher = publisher

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
    ) -> str:
        if source_revision < 1:
            raise PlatformError("validation_error", "source_revision must be positive", {}, 422)
        context = current_context()
        command = OutboxPublishCommand(
            event_id=f"evt_public_graph_source_{source_revision}",
            caller_principal="documents",
            event_type="public_graph_source_changed",
            schema_version=SUPPORTED_EVENT_SCHEMA_VERSION,
            aggregate_type="public_graph_source",
            aggregate_id="public",
            transition_version=source_revision,
            occurred_at=occurred_at,
            payload={
                "source_revision": source_revision,
                "source_manifest_id": source_manifest_id,
                "source_manifest_hash": source_manifest_hash,
                "document_id": document_id,
                "change_type": change_type,
            },
            recipients=(),
            trace_id=context.trace_id if context is not None else None,
        )
        self._publisher._publish_authorized(
            command,
            connection=connection,
            caller="documents",
        )
        return command.event_id


class SqlAlchemyIngestionOutboxAdapter:
    """Ingestion-completion scoped, no-token facade over the outbox publisher."""

    def __init__(self, publisher: SqlAlchemyOutboxPublisher) -> None:
        self._publisher = publisher

    @staticmethod
    def _event_id(*, event_type: str, job_id: str, transition_version: int) -> str:
        return f"evt_ingestion_{event_type}_{job_id}_{transition_version}"

    def publish_ingestion_events(
        self,
        *,
        job_id: str,
        document_id: str,
        document_version_id: str,
        publication_id: str,
        transition_version: int,
        recipient_user_id: str,
        occurred_at: datetime,
        ocr_low_confidence: bool,
        ocr_low_confidence_fact: Mapping[str, object] | None,
        connection: Connection,
    ) -> tuple[str, ...]:
        base_payload = {
            "job_id": job_id,
            "document_id": document_id,
            "document_version_id": document_version_id,
            "publication_id": publication_id,
        }
        events: list[tuple[str, dict[str, object]]] = [("ingestion_completed", base_payload)]
        if ocr_low_confidence:
            if not isinstance(ocr_low_confidence_fact, Mapping):
                raise PlatformError(
                    "invalid_processing_receipt",
                    "Low-confidence processing receipts require a machine fact",
                    {},
                    422,
                )
            events.append(
                (
                    "ocr_low_confidence",
                    {
                        **base_payload,
                        "reason": "low_confidence",
                        "status": "degraded",
                        "machine_low_confidence_fact": dict(ocr_low_confidence_fact),
                    },
                )
            )
        context = current_context()
        event_ids: list[str] = []
        for event_type, payload in events:
            command = OutboxPublishCommand(
                event_id=self._event_id(
                    event_type=event_type,
                    job_id=job_id,
                    transition_version=transition_version,
                ),
                caller_principal="ingestion",
                event_type=event_type,
                schema_version=SUPPORTED_EVENT_SCHEMA_VERSION,
                aggregate_type="ingestion_job",
                aggregate_id=job_id,
                transition_version=transition_version,
                occurred_at=occurred_at,
                payload=payload,
                recipients=(
                    RecipientSelection(
                        recipient_user_id=recipient_user_id,
                        recipient_kind="identity",
                        selection_reason="ingestion_job_creator",
                    ),
                ),
                trace_id=context.trace_id if context is not None else None,
            )
            try:
                self._publisher._publish_authorized(
                    command,
                    connection=connection,
                    caller="ingestion",
                )
            except PlatformError as error:
                if error.code == "recipient_account_inactive":
                    return ()
                raise
            event_ids.append(command.event_id)
        return tuple(event_ids)
