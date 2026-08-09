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
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import IntegrityError

from app.identity.schema import identity_user_table
from app.platform.context import current_context
from app.platform.database import platform_audit_table
from app.platform.errors import PlatformError

from .capabilities import verify_token
from .ports import (
    NOTIFICATION_EVENT_TYPES,
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
        "machine_low_confidence_fact": dict,
    },
    "submission_approved": {"submission_id": str},
    "submission_rejected": {"submission_id": str},
    "submission_invalidated": {"submission_id": str},
    "quota_approved": {"request_id": str},
    "quota_rejected": {"request_id": str},
    "calibration_window_suggested": {"calibration_window_suggestion_id": str},
    "graph_build_completed": {
        "graph_build_id": str,
        "status": str,
        "source_revision": str,
        "graph_generation_id": (str, None),
        "index_generation_id": (str, None),
        "failure_class": (str, None),
    },
}

# Recipient rules: calibration events take all active ops as role snapshots;
# graph events notify exactly one identity (the run initiator).
ROLE_SNAPSHOT_EVENT_TYPES: frozenset[str] = frozenset({"calibration_window_suggested"})
SINGLE_RECIPIENT_EVENT_TYPES: frozenset[str] = frozenset({"graph_build_completed"})


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


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
        if status == "succeeded" and payload.get("graph_generation_id") is None:
            raise PlatformError(
                "invalid_event_payload",
                "succeeded graph build events require the external graph_generation_id",
                {"event_type": event_type},
                422,
            )
        if status != "succeeded" and payload.get("graph_generation_id") is not None:
            raise PlatformError(
                "invalid_event_payload",
                "non-succeeded graph build events must not carry a graph_generation_id",
                {"event_type": event_type},
                422,
            )
        failure_class = payload.get("failure_class")
        if failure_class is not None and failure_class not in {
            "staging_error",
            "index_error",
            "cancelled",
        }:
            raise PlatformError(
                "invalid_event_payload",
                "graph_build_completed failure_class is invalid",
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
    # port. The producer capabilities come from the assembly-time registry; the
    # command's caller_principal is only a label, never an authority.
    #
    # Producer wiring note: the ingestion/submission/quota/calibration/graph
    # business domains are not part of this repository (they arrive in later
    # business changes). Their terminal-state transactions must call
    # `outbox_publisher.publish(command, connection=connection)` inside their
    # own transaction; the typed `OutboxPublishPort` is that formal boundary.
    """

    def __init__(
        self,
        engine: Engine,
        *,
        clock: Any = None,
        now: Callable[[], datetime] | None = None,
        capabilities: Mapping[str, frozenset[str]] | None = None,
        graph_activated_receipt_port: Any = None,
        capability_secret: bytes | None = None,
    ) -> None:
        self._engine = engine
        self._clock = clock
        self._now = now
        self._graph_activated_receipt_port = graph_activated_receipt_port
        # Assembly-time registry: caller -> allowed event types. `None` builds
        # the production matrix; an EXPLICIT empty mapping is deny-all and is
        # never replaced by the default.
        self._capabilities = (
            capabilities if capabilities is not None else self._default_capabilities()
        )
        # The signing secret is supplied by the runtime at assembly time. A
        # direct construction without one has NO signing authority: every
        # publish fails closed with 403 (there is no implicit development
        # secret in production code).
        self._capability_secret = capability_secret

    @staticmethod
    def _default_capabilities() -> dict[str, frozenset[str]]:
        registry: dict[str, set[str]] = {}
        for event_type, (callers, _aggregate) in PRODUCER_MATRIX.items():
            for caller in callers:
                registry.setdefault(caller, set()).add(event_type)
        return {caller: frozenset(types) for caller, types in registry.items()}

    def _database_now(self, connection: Connection) -> datetime:
        if self._clock is not None:
            value = self._clock.now_utc(connection)
            if isinstance(value, datetime):
                return _utc(value)
        if self._now is not None:
            return _utc(self._now())
        return _utc(datetime.now(UTC))

    def publish(
        self,
        command: OutboxPublishCommand,
        *,
        connection: Connection,
    ) -> OutboxPublishReceipt:
        # Authority comes ONLY from the signed capability token issued at
        # assembly time. The caller_principal string is an audit label; a
        # missing, forged or unverifiable token is fail-closed 403.
        caller = self._verify_capability(command)
        _validate_payload(command.event_type, command.schema_version, command.payload)
        if command.event_type not in NOTIFICATION_EVENT_TYPES:
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
        command = self._freeze_calibration_recipients(connection, command)
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
                self._alert_fingerprint_conflict(connection, command, fingerprint)
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
                    self._alert_fingerprint_conflict(connection, command, fingerprint)
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

    def _verify_capability(self, command: OutboxPublishCommand) -> str:
        """Verify the signed producer token; returns the trusted principal.

        The token is an opaque capability issued at assembly time with an
        HMAC signature. The assembly-time registry is the scope of truth on
        top of the signature: even a validly signed token is cut down to the
        event types the runtime registered for its principal. Without a
        configured secret the publisher fails closed.
        """
        token = command.capability
        if not token:
            raise PlatformError(
                "producer_not_authorized",
                "A signed producer capability token is required",
                {},
                403,
            )
        secret = self._capability_secret
        if not secret:
            raise PlatformError(
                "producer_not_authorized",
                "Producer capability verification is not configured",
                {},
                403,
            )
        claims = verify_token(secret, token)
        if claims is None or claims.get("kind") != "producer":
            raise PlatformError(
                "producer_not_authorized",
                "The producer capability token is invalid or forged",
                {},
                403,
            )
        caller = str(claims.get("principal") or "").strip()
        scope = claims.get("scope")
        event_types = scope.get("event_types") if isinstance(scope, dict) else None
        if not caller or not isinstance(event_types, list) or not event_types:
            raise PlatformError(
                "producer_not_authorized",
                "The producer capability token claims are invalid",
                {},
                403,
            )
        audit_label = command.caller_principal.strip()
        if audit_label and audit_label != caller:
            raise PlatformError(
                "producer_not_authorized",
                "The caller audit label does not match the capability",
                {},
                403,
            )
        if command.event_type not in event_types:
            raise PlatformError(
                "producer_not_authorized",
                f"Caller {caller} may not publish {command.event_type} events",
                {"event_type": command.event_type},
                403,
            )
        allowed = self._capabilities.get(caller)
        if allowed is None or command.event_type not in allowed:
            raise PlatformError(
                "producer_not_authorized",
                f"Caller {caller} has no assembly-time scope for {command.event_type} events",
                {"event_type": command.event_type},
                403,
            )
        return caller

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

    def _freeze_calibration_recipients(
        self,
        connection: Connection,
        command: OutboxPublishCommand,
    ) -> OutboxPublishCommand:
        """Calibration events freeze ALL active ops inside this transaction."""
        if command.event_type != "calibration_window_suggested":
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
        if not ops:
            raise PlatformError(
                "no_active_ops",
                "calibration_window_suggested requires at least one active ops",
                {},
                422,
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
                compact_after_at_utc=None,
                compacted_at_utc=None,
                compacted_delivery_summary_json=None,
            )
        )

    def _alert_fingerprint_conflict(
        self,
        connection: Connection,
        command: OutboxPublishCommand,
        fingerprint: str,
    ) -> None:
        context = current_context()
        if connection.dialect.name == "postgresql":
            # Write the audit row through a SEPARATE connection/transaction so
            # the high-priority invariant alert survives the business
            # transaction rollback that always accompanies a fingerprint
            # conflict.
            try:
                with self._engine.begin() as audit_connection:
                    audit_connection.execute(
                        platform_audit_table.insert().values(
                            actor_id=f"producer:{command.caller_principal}",
                            resource_type="outbox_event",
                            resource_id=command.event_id,
                            request_id=(
                                context.request_id if context is not None else "req_outbox"
                            ),
                            occurred_at_utc=self._database_now(audit_connection),
                            result="fingerprint_conflict",
                            details_json={
                                "event_type": command.event_type,
                                "aggregate_type": command.aggregate_type,
                                "aggregate_id": command.aggregate_id,
                                "transition_version": command.transition_version,
                                "fingerprint": fingerprint,
                            },
                        )
                    )
            except Exception:
                # The alert must never mask the original conflict error.
                _logger.exception("outbox fingerprint conflict alert failed")
        # Non-PostgreSQL dialects have no independent transaction; the log is
        # the durable alert signal there.
        _logger.error(
            "outbox fingerprint conflict event_type=%s aggregate=%s/%s transition=%s",
            command.event_type,
            command.aggregate_type,
            command.aggregate_id,
            command.transition_version,
        )

    def _validate_recipients(self, command: OutboxPublishCommand) -> None:
        if not command.recipients:
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
                        "calibration events require active ops role snapshots",
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
        if account is None or account["lifecycle_status"] != "active":
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
