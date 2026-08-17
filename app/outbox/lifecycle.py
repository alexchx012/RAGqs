"""Typed lifecycle ports implementation.

Three non-HTTP commands own document redaction, account retirement and
eligible-event compaction. Every command is idempotent by operation_id and
participates in the caller's transaction; the caller may retry after a lost
response because the command/receipt rows are keyed by operation_id.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, func, select, text, update
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import IntegrityError

from app.identity.schema import identity_user_table
from app.platform.errors import PlatformError

from .compaction import canonical_receipt_fingerprint, compact_event
from .notifications import DELETED_DOCUMENT_TITLE
from .ports import (
    AccountNotificationRetirementCommand,
    AccountNotificationRetirementReceipt,
    DocumentNotificationRedactionCommand,
    DocumentNotificationRedactionReceipt,
    EligibleAccountEventCompactionCommand,
    EligibleAccountEventCompactionReceipt,
)
from .schema import (
    notification_context_ack_table,
    notification_delivery_receipt_table,
    notification_inbox_table,
    notification_suppression_table,
    notification_table,
    outbox_account_retirement_tombstone_table,
    outbox_compaction_command_table,
    outbox_delivery_table,
    outbox_document_tombstone_table,
    outbox_event_table,
    outbox_recipient_table,
    outbox_redaction_receipt_table,
    outbox_retirement_command_table,
)

DOCUMENTS_CALLER = "documents"
RETENTION_CALLER = "retention-ops"


class UnauthorizedLifecycleCaller(PlatformError):
    """The caller principal is not authorized to invoke this lifecycle command."""

    def __init__(self, caller: str) -> None:
        super().__init__(
            "forbidden",
            "The lifecycle caller is not authorized",
            {"caller": caller},
            403,
        )


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _reserve_row(connection: Connection, table: Any, values: dict[str, object]) -> bool:
    """Atomically reserve an operation-scoped row.

    Uses the dialect-native INSERT ... ON CONFLICT DO NOTHING: the winner
    inserts the row, every loser blocks on the winner's uncommitted row
    (PostgreSQL) and then detects it did not insert (rowcount 0 on SQLite;
    no RETURNING row on PostgreSQL, whose psycopg3 driver reports rowcount
    -1 even for a successful insert). No savepoint is involved, so the
    reservation participates in the caller transaction and rolls back with it
    on every dialect. Dialects without ON CONFLICT support fall back to a
    savepoint + IntegrityError probe. Returns True only for the winner.
    """
    if connection.dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert as pg_insert

        # psycopg3 reports rowcount -1 for INSERT ... ON CONFLICT DO NOTHING
        # even on a successful insert, so rowcount is not a usable winner
        # signal; RETURNING is.
        stmt: Any = (
            pg_insert(table)
            .values(**values)
            .on_conflict_do_nothing()
            .returning(table.c.operation_id)
        )
        return connection.execute(stmt).scalar_one_or_none() is not None
    elif connection.dialect.name == "sqlite":
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert

        stmt = sqlite_insert(table).values(**values).on_conflict_do_nothing()
    else:
        stmt = table.insert().values(**values)
        try:
            with connection.begin_nested():
                connection.execute(stmt)
        except IntegrityError:
            return False
        return True
    result = connection.execute(stmt)
    return result.rowcount == 1


def _canonical_fingerprint(parts: dict[str, object]) -> str:
    encoded = json.dumps(parts, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(b"outbox-lifecycle-v1\0" + encoded.encode("utf-8")).hexdigest()


def _command_input_fingerprint(command: object) -> str:
    """Server-side canonical input fingerprint over the FULL command input.

    The client-supplied canonical_input_fingerprint is never trusted; the
    server recomputes it from every input field so a same-ID different-input
    replay is a permanent 409 idempotency_key_conflict.
    """
    if isinstance(command, DocumentNotificationRedactionCommand):
        return _canonical_fingerprint(
            {
                "kind": "redact",
                "deletion_id": command.deletion_id,
                "document_id": command.document_id,
                "document_version_ids": list(command.document_version_ids),
                "reason": command.reason,
                "transaction_id": command.transaction_id,
                "mode": command.mode,
            }
        )
    if isinstance(command, AccountNotificationRetirementCommand):
        return _canonical_fingerprint(
            {
                "kind": "retire",
                "user_id": command.user_id,
                "deletion_id": command.deletion_id,
                "verified_archive_ref": command.verified_archive_ref,
                "archive_checksum": command.archive_checksum,
                "transaction_id": command.transaction_id,
                "mode": command.mode,
            }
        )
    if isinstance(command, EligibleAccountEventCompactionCommand):
        return _canonical_fingerprint(
            {
                "kind": "compact",
                "user_id": command.user_id,
                "deletion_id": command.deletion_id,
                "retirement_receipt_id": command.retirement_receipt_id,
                "retirement_receipt_fingerprint": command.retirement_receipt_fingerprint,
                "transaction_id": command.transaction_id,
            }
        )
    raise TypeError(f"unknown lifecycle command: {type(command).__name__}")


class SqlAlchemyOutboxLifecycle:
    """Database implementation of the three outbox-owned lifecycle ports."""

    def __init__(
        self,
        engine: Engine,
        *,
        now: Callable[[], datetime],
        clock: Any = None,
        archive_verifier: Any = None,
    ) -> None:
        del engine
        self._now = now
        self._clock = clock
        self._archive_verifier = archive_verifier

    def _current_time(self, connection: Connection | None = None) -> datetime:
        if self._clock is not None and connection is not None:
            value = self._clock.now_utc(connection)
            return value if isinstance(value, datetime) else _utc(self._now())
        return _utc(self._now())

    def _verify_archive_proof(self, command: AccountNotificationRetirementCommand) -> None:
        """Verify the opaque archive reference against a completed archive proof.

        The verifier is injected at assembly time (identity-owned archive proof
        service); a caller may never self-declare an archive reference. The
        proof is validated against the command's user and deletion so a proof
        issued for one account/deletion can never be replayed elsewhere.
        Without a configured verifier the outbox fails closed.
        """
        verifier = self._archive_verifier
        if verifier is None:
            raise PlatformError(
                "archive_verifier_unavailable",
                "Archive proof verification is not configured",
                {"retryable": True},
                503,
            )
        verify = getattr(verifier, "verify_archive", None)
        if not callable(verify):
            raise PlatformError(
                "archive_verifier_unavailable",
                "Archive proof verification is not configured",
                {"retryable": True},
                503,
            )
        if not verify(
            archive_ref=command.verified_archive_ref,
            checksum=command.archive_checksum,
            user_id=command.user_id,
            deletion_id=command.deletion_id,
        ):
            raise PlatformError(
                "archive_proof_mismatch",
                "Archive reference does not match the verified archive proof",
                {},
                422,
            )

    def _check_caller(self, command: object) -> None:
        """Keep the typed lifecycle ports bound to their owning domain."""
        principal = str(getattr(command, "caller_principal", ""))
        if isinstance(command, DocumentNotificationRedactionCommand):
            if principal == DOCUMENTS_CALLER:
                return
        elif isinstance(
            command, (AccountNotificationRetirementCommand, EligibleAccountEventCompactionCommand)
        ) and principal == RETENTION_CALLER:
            return
        raise UnauthorizedLifecycleCaller(principal)

    # ------------------------------------------------------------------
    # RedactDocumentNotifications
    # ------------------------------------------------------------------

    def redact_document_notifications(
        self,
        command: DocumentNotificationRedactionCommand,
        *,
        connection: Connection,
    ) -> DocumentNotificationRedactionReceipt:
        self._check_caller(command)
        return self._redact_document_notifications(command, connection=connection)

    def redact_document_notifications_for_documents(
        self,
        *,
        operation_id: str,
        deletion_id: str,
        document_id: str,
        document_version_ids: tuple[str, ...],
        transaction_id: str,
        connection: Connection,
    ) -> DocumentNotificationRedactionReceipt:
        """Internal Documents-only redaction entry assembled behind a gateway."""
        command = DocumentNotificationRedactionCommand(
            operation_id=operation_id,
            caller_principal="documents",
            deletion_id=deletion_id,
            document_id=document_id,
            document_version_ids=document_version_ids,
            reason="document_pending_delete",
            transaction_id=transaction_id,
            mode="inline",
            canonical_input_fingerprint=operation_id,
        )
        return self._redact_document_notifications(command, connection=connection)

    def _redact_document_notifications(
        self,
        command: DocumentNotificationRedactionCommand,
        *,
        connection: Connection,
    ) -> DocumentNotificationRedactionReceipt:
        if command.mode != "inline" or command.reason != "document_pending_delete":
            raise PlatformError(
                "validation_error",
                "Redaction command mode or reason is invalid",
                {},
                422,
            )
        if not command.deletion_id.strip() or not command.document_id.strip():
            raise PlatformError("validation_error", "Redaction scope is invalid", {}, 422)
        if not command.document_version_ids:
            raise PlatformError(
                "validation_error",
                "Redaction requires at least one document version",
                {},
                422,
            )
        now = self._current_time(connection)
        input_fingerprint = _command_input_fingerprint(command)

        # Fast path: an already committed operation returns its receipt
        # unchanged (or raises a permanent 409 for a conflicting input).
        existing = (
            connection.execute(
                select(outbox_redaction_receipt_table).where(
                    outbox_redaction_receipt_table.c.operation_id == command.operation_id
                )
            )
            .mappings()
            .one_or_none()
        )
        if existing is not None:
            return self._redaction_receipt_from_row(command, dict(existing))

        # Step 1: reserve the operation BEFORE any side effect. The atomic
        # reservation row is the operation-scoped lock: the first caller to
        # insert the row is the winner; every loser (rowcount 0) re-reads the
        # winner's committed row and returns its real receipt (or a permanent
        # 409 for a conflicting input) WITHOUT touching notifications or
        # tombstones. The reservation row participates in the caller
        # transaction, so an outer rollback undoes it on every dialect.
        reserved = _reserve_row(
            connection,
            outbox_redaction_receipt_table,
            {
                "operation_id": command.operation_id,
                "deletion_id": command.deletion_id,
                "document_id": command.document_id,
                "document_version_ids_json": list(command.document_version_ids),
                "input_fingerprint": input_fingerprint,
                "state": "pending",
                "redacted_notification_count": 0,
                "already_redacted_count": 0,
                "created_at_utc": now,
            },
        )
        if not reserved:
            # A concurrent caller committed the same operation first: return
            # the winner's real receipt (or a 409 for a conflicting input).
            # Never leak IntegrityError and never apply any redaction side
            # effect.
            concurrent = (
                connection.execute(
                    select(outbox_redaction_receipt_table).where(
                        outbox_redaction_receipt_table.c.operation_id == command.operation_id
                    )
                )
                .mappings()
                .one_or_none()
            )
            if concurrent is None:
                raise PlatformError(
                    "operation_reservation_lost",
                    "The redaction operation reservation was lost without a committed receipt",
                    {"operation_id": command.operation_id},
                    409,
                )
            return self._redaction_receipt_from_row(command, dict(concurrent))

        # Take the per-document advisory lock so a concurrent materialization
        # cannot insert an unredacted projection after we commit.
        if connection.dialect.name == "postgresql":
            connection.execute(
                text("SELECT pg_advisory_xact_lock(hashtext(:lock))"),
                {"lock": f"ragqs:documents:redact:{command.document_id}"},
            )

        # Lock matching projections inside the deletion transaction.
        matching = (
            connection.execute(
                select(
                    notification_table.c.id,
                    notification_table.c.redacted,
                )
                .where(
                    notification_table.c.document_id == command.document_id,
                    notification_table.c.document_version_id.in_(command.document_version_ids),
                )
                .with_for_update()
            )
            .mappings()
            .all()
        )
        redacted_count = 0
        already_redacted_count = 0
        for row in matching:
            if bool(row["redacted"]):
                already_redacted_count += 1
                continue
            connection.execute(
                update(notification_table)
                .where(notification_table.c.id == row["id"])
                .values(
                    title=DELETED_DOCUMENT_TITLE,
                    payload_json={},
                    redacted=True,
                )
            )
            redacted_count += 1

        # Finalize the reservation row with the real counts; the whole
        # transaction commits atomically so the intermediate pending state is
        # never visible to other transactions.
        connection.execute(
            update(outbox_redaction_receipt_table)
            .where(outbox_redaction_receipt_table.c.operation_id == command.operation_id)
            .values(
                state="completed",
                redacted_notification_count=redacted_count,
                already_redacted_count=already_redacted_count,
            )
        )
        # Permanent tombstone per (document, version): survives compaction and
        # blocks any later materialization or replay from restoring original
        # text. Multiple version scopes coexist.
        for version_id in command.document_version_ids:
            connection.execute(
                outbox_document_tombstone_table.insert().values(
                    document_id=command.document_id,
                    document_version_id=version_id,
                    deletion_id=command.deletion_id,
                    created_at_utc=now,
                )
            )
        return DocumentNotificationRedactionReceipt(
            operation_id=command.operation_id,
            deletion_id=command.deletion_id,
            state="completed",
            redacted_notification_count=redacted_count,
            already_redacted_count=already_redacted_count,
            retryable=False,
        )

    @staticmethod
    def _redaction_receipt_from_row(
        command: DocumentNotificationRedactionCommand,
        row: dict[str, object],
    ) -> DocumentNotificationRedactionReceipt:
        stored_versions = row.get("document_version_ids_json")
        stored_versions_tuple = tuple(stored_versions) if isinstance(stored_versions, list) else ()
        if (
            str(row["deletion_id"]) != command.deletion_id
            or str(row["document_id"]) != command.document_id
            or stored_versions_tuple != tuple(command.document_version_ids)
            or str(row.get("input_fingerprint") or "") != _command_input_fingerprint(command)
        ):
            raise PlatformError(
                "idempotency_key_conflict",
                "Redaction operation conflicts with an existing operation",
                {},
                409,
            )
        return DocumentNotificationRedactionReceipt(
            operation_id=command.operation_id,
            deletion_id=str(row["deletion_id"]),
            state="completed",
            redacted_notification_count=int(str(row["redacted_notification_count"])),
            already_redacted_count=int(str(row["already_redacted_count"])),
            retryable=False,
        )

    # ------------------------------------------------------------------
    # RetireAccountNotificationState
    # ------------------------------------------------------------------

    def retire_account_notification_state(
        self,
        command: AccountNotificationRetirementCommand,
        *,
        connection: Connection,
    ) -> AccountNotificationRetirementReceipt:
        if command.caller_principal != RETENTION_CALLER:
            raise UnauthorizedLifecycleCaller(command.caller_principal)
        self._check_caller(command)
        if command.mode not in {"durable", "inline"}:
            raise PlatformError("validation_error", "Retirement mode is invalid", {}, 422)
        now = self._current_time(connection)

        if command.mode == "durable":
            # Durable retirement: the accepted command row IS the operation
            # reservation. The retirement worker applies the durable work and
            # advances it to completed. Retention may safely retry with the
            # same operation id after a lost response because the accepted row
            # is already committed. A concurrent same-operation commit returns
            # the winner's real receipt; no side effect ever runs twice.
            existing = (
                connection.execute(
                    select(outbox_retirement_command_table).where(
                        outbox_retirement_command_table.c.operation_id == command.operation_id
                    )
                )
                .mappings()
                .one_or_none()
            )
            if existing is not None:
                return self._retirement_receipt_from_row(command, dict(existing))

            account = (
                connection.execute(
                    select(
                        identity_user_table.c.id,
                        identity_user_table.c.lifecycle_status,
                    ).where(identity_user_table.c.id == command.user_id)
                )
                .mappings()
                .one_or_none()
            )
            if account is None:
                raise PlatformError("not_found", "Lifecycle target account was not found", {}, 404)
            lifecycle_status = str(account["lifecycle_status"])
            if lifecycle_status == "deleted":
                raise PlatformError(
                    "account_already_deleted",
                    "The account is already deleted; retirement cannot be applied",
                    {},
                    409,
                )
            if lifecycle_status != "pending_delete":
                raise PlatformError(
                    "account_not_deletable",
                    "Retirement requires a pending-delete lifecycle target",
                    {},
                    422,
                )

            self._verify_archive_proof(command)

            input_fingerprint = _command_input_fingerprint(command)
            archive_fingerprint = _canonical_fingerprint(
                {
                    "archive_ref": command.verified_archive_ref,
                    "checksum": command.archive_checksum,
                }
            )
            winner_receipt = self._reserve_retirement_command(
                connection,
                command=command,
                input_fingerprint=input_fingerprint,
                archive_fingerprint=archive_fingerprint,
                mode="durable",
                state="accepted",
                receipt_json={
                    "receipt_count": 0,
                    "notification_retired_count": 0,
                    "inbox_removed": False,
                },
                created_at=now,
                completed_at=None,
            )
            if winner_receipt is not None:
                return winner_receipt
            return AccountNotificationRetirementReceipt(
                operation_id=command.operation_id,
                user_id=command.user_id,
                deletion_id=command.deletion_id,
                state="accepted",
                receipt_count=0,
                notification_retired_count=0,
                inbox_removed=False,
                retryable=True,
            )

        # Inline retirement participates in the caller transaction and completes
        # immediately; it rolls back with the caller transaction on failure.
        return self._retire_inline(command, connection)

    def retire_account_for_identity_deletion(
        self,
        *,
        operation_id: str,
        user_id: str,
        deletion_id: str,
        verified_archive_ref: str,
        archive_checksum: str,
        transaction_id: str,
        connection: Connection,
    ) -> AccountNotificationRetirementReceipt:
        """Internal identity-deletion scoped entry: retire exactly the account
        named by the identity deletion workflow.

        This entry can only be reached through the runtime-assembled gateway
        façade, accepts only a pending-delete account whose archive proof
        verifies, and fixes the caller principal. A caller cannot choose a
        different operation type.
        """
        command = AccountNotificationRetirementCommand(
            operation_id=operation_id,
            caller_principal="retention-ops",
            user_id=user_id,
            deletion_id=deletion_id,
            verified_archive_ref=verified_archive_ref,
            archive_checksum=archive_checksum,
            transaction_id=transaction_id,
            mode="inline",
            canonical_input_fingerprint=operation_id,
        )
        return self._retire_inline(command, connection)

    def _retire_inline(
        self,
        command: AccountNotificationRetirementCommand,
        connection: Connection,
    ) -> AccountNotificationRetirementReceipt:
        """Shared inline retirement: validate, reserve, apply and finalize."""
        now = self._current_time(connection)

        existing = (
            connection.execute(
                select(outbox_retirement_command_table).where(
                    outbox_retirement_command_table.c.operation_id == command.operation_id
                )
            )
            .mappings()
            .one_or_none()
        )
        if existing is not None:
            return self._retirement_receipt_from_row(command, dict(existing))

        account = (
            connection.execute(
                select(
                    identity_user_table.c.id,
                    identity_user_table.c.lifecycle_status,
                ).where(identity_user_table.c.id == command.user_id)
            )
            .mappings()
            .one_or_none()
        )
        if account is None:
            raise PlatformError("not_found", "Lifecycle target account was not found", {}, 404)
        lifecycle_status = str(account["lifecycle_status"])
        if lifecycle_status == "deleted":
            raise PlatformError(
                "account_already_deleted",
                "The account is already deleted; retirement cannot be applied",
                {},
                409,
            )
        if lifecycle_status != "pending_delete":
            raise PlatformError(
                "account_not_deletable",
                "Retirement requires a pending-delete lifecycle target",
                {},
                422,
            )

        self._verify_archive_proof(command)

        input_fingerprint = _command_input_fingerprint(command)
        archive_fingerprint = _canonical_fingerprint(
            {"archive_ref": command.verified_archive_ref, "checksum": command.archive_checksum}
        )
        # Reserve the operation FIRST (pending row), then apply the side
        # effects, then finalize the row with the real counts. A concurrent
        # same-operation commit returns the winner's real receipt and never
        # performs any retirement side effect.
        winner_receipt = self._reserve_retirement_command(
            connection,
            command=command,
            input_fingerprint=input_fingerprint,
            archive_fingerprint=archive_fingerprint,
            mode="inline",
            state="pending",
            receipt_json={
                "receipt_count": 0,
                "notification_retired_count": 0,
                "inbox_removed": False,
            },
            created_at=now,
            completed_at=None,
        )
        if winner_receipt is not None:
            return winner_receipt
        receipt = self._apply_retirement_work(connection, command, now=now)
        connection.execute(
            update(outbox_retirement_command_table)
            .where(outbox_retirement_command_table.c.operation_id == command.operation_id)
            .values(
                state="completed",
                receipt_json={
                    "receipt_count": receipt.receipt_count,
                    "notification_retired_count": receipt.notification_retired_count,
                    "inbox_removed": receipt.inbox_removed,
                },
                completed_at_utc=now,
            )
        )
        return receipt

    def _reserve_retirement_command(
        self,
        connection: Connection,
        *,
        command: AccountNotificationRetirementCommand,
        input_fingerprint: str,
        archive_fingerprint: str,
        mode: str,
        state: str,
        receipt_json: dict[str, object],
        created_at: datetime,
        completed_at: datetime | None,
    ) -> AccountNotificationRetirementReceipt | None:
        """Atomically reserve the retirement operation row under a savepoint.

        Returns the winner's real receipt when a concurrent caller already
        committed the same operation (the caller must NOT perform any side
        effect), or None when THIS caller is the reservation winner. A
        conflicting input raises a permanent 409 idempotency_key_conflict;
        IntegrityError is never leaked.
        """
        reserved = _reserve_row(
            connection,
            outbox_retirement_command_table,
            {
                "operation_id": command.operation_id,
                "user_id": command.user_id,
                "deletion_id": command.deletion_id,
                "input_fingerprint": input_fingerprint,
                "archive_ref": command.verified_archive_ref,
                "archive_checksum": command.archive_checksum,
                "archive_ref_fingerprint": archive_fingerprint,
                "mode": mode,
                "state": state,
                "receipt_json": receipt_json,
                "created_at_utc": created_at,
                "completed_at_utc": completed_at,
            },
        )
        if reserved:
            return None
        concurrent = (
            connection.execute(
                select(outbox_retirement_command_table).where(
                    outbox_retirement_command_table.c.operation_id == command.operation_id
                )
            )
            .mappings()
            .one_or_none()
        )
        if concurrent is None:
            raise PlatformError(
                "operation_reservation_lost",
                "The retirement operation reservation was lost without a committed receipt",
                {"operation_id": command.operation_id},
                409,
            )
        return self._retirement_receipt_from_row(command, dict(concurrent))

    def apply_accepted_durable_retirement(
        self,
        operation_id: str,
        *,
        connection: Connection,
    ) -> AccountNotificationRetirementReceipt:
        """Internal safe entry: apply the durable work for an ALREADY-ACCEPTED
        command row.

        The only commands this entry can process are rows previously accepted
        by the retirement port. A caller cannot fabricate a command because
        the operation scope, user, deletion and archive facts all come from
        the stored accepted row.
        """
        now = self._current_time(connection)
        stored = (
            connection.execute(
                select(outbox_retirement_command_table).where(
                    outbox_retirement_command_table.c.operation_id == operation_id
                )
            )
            .mappings()
            .one_or_none()
        )
        if stored is None or stored["mode"] != "durable" or stored["state"] != "accepted":
            raise PlatformError(
                "retirement_not_pending",
                "Retirement operation is not an accepted durable command",
                {},
                409,
            )
        command = AccountNotificationRetirementCommand(
            operation_id=operation_id,
            caller_principal="retention-ops",
            user_id=str(stored["user_id"]),
            deletion_id=str(stored["deletion_id"]),
            verified_archive_ref=str(stored["archive_ref"]),
            archive_checksum=str(stored["archive_checksum"]),
            transaction_id="",
            mode="durable",
            canonical_input_fingerprint=str(stored["input_fingerprint"]),
        )
        # Re-validate the identity lifecycle and deletion workflow inside the
        # worker transaction: the account must still be pending_delete.
        account = (
            connection.execute(
                select(identity_user_table.c.id, identity_user_table.c.lifecycle_status).where(
                    identity_user_table.c.id == command.user_id
                )
            )
            .mappings()
            .one_or_none()
        )
        if account is None:
            raise PlatformError("not_found", "Lifecycle target account was not found", {}, 404)
        if str(account["lifecycle_status"]) == "deleted":
            raise PlatformError(
                "account_already_deleted",
                "The account is already deleted; durable retirement cannot apply",
                {},
                409,
            )
        if str(account["lifecycle_status"]) != "pending_delete":
            raise PlatformError(
                "account_not_deletable",
                "Durable retirement requires a pending-delete lifecycle target",
                {},
                422,
            )
        # Validate the opaque archive reference against the completed archive
        # proof bound to user/deletion (stored facts came from the
        # already-verified accepted row), then against the stored row.
        self._verify_archive_proof(command)
        return self._apply_durable_work(command, stored, connection, now=now)

    def apply_durable_retirement(
        self,
        command: AccountNotificationRetirementCommand,
        *,
        connection: Connection,
    ) -> AccountNotificationRetirementReceipt:
        """Apply the durable retirement work and advance the command to completed.

        The archive reference is validated against the stored accepted command
        so a caller cannot substitute an unverified client value.
        """
        self._check_caller(command)
        now = self._current_time(connection)
        stored = (
            connection.execute(
                select(outbox_retirement_command_table).where(
                    outbox_retirement_command_table.c.operation_id == command.operation_id
                )
            )
            .mappings()
            .one_or_none()
        )
        if stored is None or stored["mode"] != "durable" or stored["state"] != "accepted":
            raise PlatformError(
                "retirement_not_pending",
                "Retirement operation is not an accepted durable command",
                {},
                409,
            )
        if str(stored["user_id"]) != command.user_id:
            raise PlatformError(
                "idempotency_key_conflict",
                "Retirement operation conflicts with the stored command",
                {},
                409,
            )
        # Re-validate the identity lifecycle and deletion workflow inside the
        # worker transaction: the account must still be pending_delete.
        account = (
            connection.execute(
                select(identity_user_table.c.id, identity_user_table.c.lifecycle_status).where(
                    identity_user_table.c.id == command.user_id
                )
            )
            .mappings()
            .one_or_none()
        )
        if account is None:
            raise PlatformError("not_found", "Lifecycle target account was not found", {}, 404)
        if str(account["lifecycle_status"]) == "deleted":
            raise PlatformError(
                "account_already_deleted",
                "The account is already deleted; durable retirement cannot apply",
                {},
                409,
            )
        if str(account["lifecycle_status"]) != "pending_delete":
            raise PlatformError(
                "account_not_deletable",
                "Durable retirement requires a pending-delete lifecycle target",
                {},
                422,
            )
        # Validate the opaque archive reference against the completed archive
        # proof bound to user/deletion, then against the stored accepted row.
        self._verify_archive_proof(command)
        return self._apply_durable_work(command, stored, connection, now=now)

    def _apply_durable_work(
        self,
        command: AccountNotificationRetirementCommand,
        stored: Any,
        connection: Connection,
        *,
        now: datetime,
    ) -> AccountNotificationRetirementReceipt:
        """Apply the durable retirement work and advance the stored command."""
        archive_proof = _canonical_fingerprint(
            {"archive_ref": command.verified_archive_ref, "checksum": command.archive_checksum}
        )
        if archive_proof != str(stored["archive_ref_fingerprint"]):
            raise PlatformError(
                "archive_proof_mismatch",
                "Archive reference does not match the verified archive proof",
                {},
                422,
            )
        receipt = self._apply_retirement_work(connection, command, now=now)
        updated = connection.execute(
            update(outbox_retirement_command_table)
            .where(
                outbox_retirement_command_table.c.operation_id == command.operation_id,
                outbox_retirement_command_table.c.state == "accepted",
            )
            .values(
                state="completed",
                receipt_json={
                    "receipt_count": receipt.receipt_count,
                    "notification_retired_count": receipt.notification_retired_count,
                    "inbox_removed": receipt.inbox_removed,
                },
                completed_at_utc=now,
            )
        ).rowcount
        if updated != 1:
            raise PlatformError(
                "retirement_conflict",
                "Retirement command state changed before completion",
                {},
                409,
            )
        return receipt

    def _apply_retirement_work(
        self,
        connection: Connection,
        command: AccountNotificationRetirementCommand,
        *,
        now: datetime,
    ) -> AccountNotificationRetirementReceipt:
        """Write receipts, remove notifications/acks and mark the inbox retired."""
        # Serialize with materialization/read-all on the same per-user lock.
        if connection.dialect.name == "postgresql":
            connection.execute(
                text("SELECT pg_advisory_xact_lock(hashtext(:lock))"),
                {"lock": f"ragqs:notifications:read-all:{command.user_id}"},
            )
        # 1. Write permanent delivery receipts for every remaining projection.
        receipt_count = self._write_permanent_receipts(connection, user_id=command.user_id, now=now)
        # 2. Remove notification and context-ack rows.
        retired = connection.execute(
            select(func.count())
            .select_from(notification_table)
            .where(notification_table.c.recipient_user_id == command.user_id)
        ).scalar_one()
        connection.execute(
            delete(notification_table).where(
                notification_table.c.recipient_user_id == command.user_id
            )
        )
        connection.execute(
            delete(notification_context_ack_table).where(
                notification_context_ack_table.c.recipient_user_id == command.user_id
            )
        )
        # 3. Record the permanent tombstone (survives the inbox removal) and
        #    then truly delete the inbox row. The tombstone prevents any later
        #    materialization from rebuilding notifications or reusing
        #    sequences for this account.
        inbox = (
            connection.execute(
                select(
                    notification_inbox_table.c.next_notification_seq,
                    notification_inbox_table.c.read_through_seq,
                    notification_inbox_table.c.version,
                ).where(notification_inbox_table.c.recipient_user_id == command.user_id)
            )
            .mappings()
            .one_or_none()
        )
        inbox_removed = False
        next_seq = 1
        read_through = 0
        if inbox is not None:
            next_seq = int(inbox["next_notification_seq"])
            read_through = int(inbox["read_through_seq"])
            connection.execute(
                update(notification_inbox_table)
                .where(
                    notification_inbox_table.c.recipient_user_id == command.user_id,
                    notification_inbox_table.c.version == int(inbox["version"]),
                )
                .values(retired=True, version=int(inbox["version"]) + 1)
            )
            deleted = connection.execute(
                delete(notification_inbox_table).where(
                    notification_inbox_table.c.recipient_user_id == command.user_id
                )
            ).rowcount
            inbox_removed = deleted == 1
        connection.execute(
            outbox_account_retirement_tombstone_table.insert().values(
                recipient_user_id=command.user_id,
                next_notification_seq=next_seq,
                read_through_seq=read_through,
                retired_at_utc=now,
            )
        )

        return AccountNotificationRetirementReceipt(
            operation_id=command.operation_id,
            user_id=command.user_id,
            deletion_id=command.deletion_id,
            state="completed",
            receipt_count=receipt_count,
            notification_retired_count=int(retired),
            inbox_removed=inbox_removed,
            retryable=False,
        )

    @staticmethod
    def _write_permanent_receipts(
        connection: Connection,
        *,
        user_id: str,
        now: datetime,
    ) -> int:
        rows = (
            connection.execute(
                select(
                    notification_table.c.event_id,
                    notification_table.c.event_occurred_at_utc,
                    notification_table.c.materialized_at_utc,
                    notification_table.c.notification_seq,
                ).where(notification_table.c.recipient_user_id == user_id)
            )
            .mappings()
            .all()
        )
        for row in rows:
            event_id = str(row["event_id"])
            occurred_at = row["event_occurred_at_utc"]
            materialized_at = row["materialized_at_utc"]
            seq = int(row["notification_seq"])
            fingerprint = canonical_receipt_fingerprint(event_id, user_id, "materialized", seq)
            existing = (
                connection.execute(
                    select(notification_delivery_receipt_table.c.event_id).where(
                        notification_delivery_receipt_table.c.event_id == event_id,
                        notification_delivery_receipt_table.c.recipient_user_id == user_id,
                    )
                ).scalar_one_or_none()
            )
            # Receipt 唯一写入者就是本套代码；已存在即此前 retirement 的幂等
            # 重放，按 PK 存在直接跳过（fingerprint 列保留作审计事实）。
            if existing is not None:
                continue
            connection.execute(
                notification_delivery_receipt_table.insert().values(
                    event_id=event_id,
                    recipient_user_id=user_id,
                    outcome="materialized",
                    original_notification_seq=seq,
                    occurred_at_utc=occurred_at,
                    materialized_at_utc=materialized_at,
                    retired_at_utc=now,
                    fingerprint=fingerprint,
                )
            )
        # Suppressed deliveries also receive a permanent receipt. The receipt
        # occurred-at fact MUST come from outbox_event.occurred_at_utc, never
        # from the suppression time: the immutable event time is the only
        # authoritative occurrence fact for receipts.
        suppressions = (
            connection.execute(
                select(
                    notification_suppression_table.c.event_id,
                    notification_suppression_table.c.reason,
                ).where(notification_suppression_table.c.recipient_user_id == user_id)
            )
            .mappings()
            .all()
        )
        for row in suppressions:
            event_id = str(row["event_id"])
            outcome = (
                "recipient_inactive"
                if row["reason"] == "recipient_inactive"
                else "recipient_unauthorized"
            )
            event_occurred_at = connection.execute(
                select(outbox_event_table.c.occurred_at_utc).where(
                    outbox_event_table.c.event_id == event_id
                )
            ).scalar_one_or_none()
            if event_occurred_at is None:
                raise PlatformError(
                    "retirement_event_missing",
                    "Retired suppression references an unknown event",
                    {"event_id": event_id},
                    409,
                )
            fingerprint = canonical_receipt_fingerprint(event_id, user_id, outcome, None)
            existing = (
                connection.execute(
                    select(notification_delivery_receipt_table.c.event_id).where(
                        notification_delivery_receipt_table.c.event_id == event_id,
                        notification_delivery_receipt_table.c.recipient_user_id == user_id,
                    )
                ).scalar_one_or_none()
            )
            if existing is not None:
                continue
            connection.execute(
                notification_delivery_receipt_table.insert().values(
                    event_id=event_id,
                    recipient_user_id=user_id,
                    outcome=outcome,
                    original_notification_seq=None,
                    occurred_at_utc=_utc(event_occurred_at),
                    materialized_at_utc=None,
                    retired_at_utc=now,
                    fingerprint=fingerprint,
                )
            )
        return len(rows) + len(suppressions)

    @staticmethod
    def _retirement_receipt_from_row(
        command: AccountNotificationRetirementCommand,
        row: dict[str, object],
    ) -> AccountNotificationRetirementReceipt:
        if (
            str(row["user_id"]) != command.user_id
            or str(row["deletion_id"]) != command.deletion_id
            or str(row.get("input_fingerprint") or "") != _command_input_fingerprint(command)
        ):
            raise PlatformError(
                "idempotency_key_conflict",
                "Retirement operation conflicts with an existing operation",
                {},
                409,
            )
        state = str(row["state"])
        if state not in {"accepted", "completed"}:
            raise PlatformError(
                "retirement_unverified", "Retirement receipt is invalid", {"retryable": True}, 503
            )
        raw_receipt = row.get("receipt_json")
        stored = dict(raw_receipt) if isinstance(raw_receipt, dict) else {}
        return AccountNotificationRetirementReceipt(
            operation_id=command.operation_id,
            user_id=str(row["user_id"]),
            deletion_id=str(row["deletion_id"]),
            state=state,  # type: ignore[arg-type]
            receipt_count=int(stored.get("receipt_count", 0)),
            notification_retired_count=int(stored.get("notification_retired_count", 0)),
            inbox_removed=bool(stored.get("inbox_removed", False)),
            retryable=state == "accepted",
        )

    # ------------------------------------------------------------------
    # RequestEligibleAccountEventCompaction
    # ------------------------------------------------------------------

    def request_eligible_account_event_compaction(
        self,
        command: EligibleAccountEventCompactionCommand,
        *,
        connection: Connection,
    ) -> EligibleAccountEventCompactionReceipt:
        if command.caller_principal != RETENTION_CALLER:
            raise UnauthorizedLifecycleCaller(command.caller_principal)
        self._check_caller(command)
        return self._request_compaction(command, connection=connection)

    def request_compaction_for_identity_deletion(
        self,
        *,
        operation_id: str,
        user_id: str,
        deletion_id: str,
        retirement_receipt_id: str,
        connection: Connection,
    ) -> EligibleAccountEventCompactionReceipt:
        """Internal identity-deletion scoped entry: request eligible event
        compaction for exactly the completed retirement named by the identity
        deletion workflow.

        This entry can only be reached through the runtime-assembled gateway
        façade. The retirement row is re-read server-side and its receipt
        fingerprint is taken from that row instead of caller input. A caller
        cannot choose another retirement.
        """
        retirement = (
            connection.execute(
                select(outbox_retirement_command_table).where(
                    outbox_retirement_command_table.c.operation_id == retirement_receipt_id
                )
            )
            .mappings()
            .one_or_none()
        )
        if (
            retirement is None
            or str(retirement["user_id"]) != user_id
            or str(retirement["deletion_id"]) != deletion_id
            or str(retirement["state"]) != "completed"
        ):
            raise PlatformError(
                "compaction_prerequisite_missing",
                "A completed retirement receipt for this user and deletion is required",
                {},
                409,
            )
        command = EligibleAccountEventCompactionCommand(
            operation_id=operation_id,
            caller_principal="retention-ops",
            user_id=user_id,
            deletion_id=deletion_id,
            retirement_receipt_id=retirement_receipt_id,
            retirement_receipt_fingerprint=str(retirement["input_fingerprint"] or ""),
            transaction_id=f"retention:{operation_id}",
            canonical_input_fingerprint="",
        )
        return self._request_compaction(command, connection=connection)

    def _request_compaction(
        self,
        command: EligibleAccountEventCompactionCommand,
        *,
        connection: Connection,
    ) -> EligibleAccountEventCompactionReceipt:
        now = self._current_time(connection)
        input_fingerprint = _command_input_fingerprint(command)

        existing = (
            connection.execute(
                select(outbox_compaction_command_table).where(
                    outbox_compaction_command_table.c.operation_id == command.operation_id
                )
            )
            .mappings()
            .one_or_none()
        )
        if existing is not None:
            return self._compaction_receipt_from_row(command, dict(existing))

        retirement = (
            connection.execute(
                select(outbox_retirement_command_table).where(
                    outbox_retirement_command_table.c.operation_id == command.retirement_receipt_id
                )
            )
            .mappings()
            .one_or_none()
        )
        # The completed retirement prerequisite must match user_id, deletion_id,
        # completed state AND the retirement command's canonical input
        # fingerprint: an old deletion's retirement receipt can never authorize
        # compaction of a newer deletion's events.
        if (
            retirement is None
            or str(retirement["user_id"]) != command.user_id
            or str(retirement["deletion_id"]) != command.deletion_id
            or str(retirement["state"]) != "completed"
            or str(retirement.get("input_fingerprint") or "")
            != command.retirement_receipt_fingerprint
        ):
            raise PlatformError(
                "compaction_prerequisite_missing",
                "A completed retirement receipt for this user, deletion and fingerprint is required",
                {},
                409,
            )

        # Reserve the compaction operation BEFORE any compaction side effect:
        # the atomic reservation row is the operation-scoped lock. A
        # concurrent same-operation commit returns the winner's real receipt
        # and never compacts any event twice.
        reserved = _reserve_row(
            connection,
            outbox_compaction_command_table,
            {
                "operation_id": command.operation_id,
                "user_id": command.user_id,
                "deletion_id": command.deletion_id,
                "retirement_receipt_id": command.retirement_receipt_id,
                "input_fingerprint": input_fingerprint,
                "state": "pending",
                "receipt_json": {
                    "eligible_count": 0,
                    "compacted_count": 0,
                    "blocked_count": 0,
                },
                "created_at_utc": now,
                "completed_at_utc": None,
            },
        )
        if not reserved:
            # A concurrent same-operation commit: re-read and return the
            # original receipt (or a 409 for conflicting input). Never leak
            # IntegrityError and never compact any event.
            concurrent = (
                connection.execute(
                    select(outbox_compaction_command_table).where(
                        outbox_compaction_command_table.c.operation_id == command.operation_id
                    )
                )
                .mappings()
                .one_or_none()
            )
            if concurrent is None:
                raise PlatformError(
                    "operation_reservation_lost",
                    "The compaction operation reservation was lost without a committed receipt",
                    {"operation_id": command.operation_id},
                    409,
                )
            return self._compaction_receipt_from_row(command, dict(concurrent))

        # Causal evidence: every full event that ever notified this account,
        # including events whose delivery is still pending/dead-lettered.
        # Receipts alone are not enough: an event with no notification (all
        # recipients suppressed) must still be examined.
        event_ids = (
            connection.execute(
                select(outbox_recipient_table.c.event_id).where(
                    outbox_recipient_table.c.recipient_user_id == command.user_id
                )
            )
            .scalars()
            .all()
        )
        eligible_count = 0
        compacted_count = 0
        blocked_count = 0
        for event_id in event_ids:
            event = (
                connection.execute(
                    select(
                        outbox_event_table.c.storage_state,
                        outbox_event_table.c.compacted_at_utc,
                    ).where(outbox_event_table.c.event_id == event_id)
                )
                .mappings()
                .one_or_none()
            )
            if event is None or event["storage_state"] != "full":
                continue
            if event["compacted_at_utc"] is not None:
                continue
            non_delivered = connection.execute(
                select(func.count())
                .select_from(outbox_delivery_table)
                .where(
                    outbox_delivery_table.c.event_id == event_id,
                    outbox_delivery_table.c.status != "delivered",
                )
            ).scalar_one()
            if int(non_delivered) != 0:
                blocked_count += 1
                continue
            eligible_count += 1
            if compact_event(connection, event_id, now):
                compacted_count += 1
            else:
                blocked_count += 1

        receipt = EligibleAccountEventCompactionReceipt(
            operation_id=command.operation_id,
            user_id=command.user_id,
            deletion_id=command.deletion_id,
            state="accepted" if blocked_count > 0 else "completed",
            eligible_count=eligible_count,
            compacted_count=compacted_count,
            blocked_count=blocked_count,
            retryable=blocked_count > 0,
        )
        completed_at = now if blocked_count == 0 else None
        connection.execute(
            update(outbox_compaction_command_table)
            .where(outbox_compaction_command_table.c.operation_id == command.operation_id)
            .values(
                state="accepted" if blocked_count > 0 else "completed",
                receipt_json={
                    "eligible_count": eligible_count,
                    "compacted_count": compacted_count,
                    "blocked_count": blocked_count,
                },
                completed_at_utc=completed_at,
            )
        )
        return receipt

    def apply_compaction_command(
        self,
        operation_id: str,
        *,
        connection: Connection,
    ) -> dict[str, object]:
        """Re-evaluate one accepted compaction command under a fence.

        Compacts every causally-associated full event whose deliveries are all
        delivered; blocked events keep the command accepted. When no event is
        blocked anymore, the command advances to completed with cumulative
        counts and its completion time.
        """
        now = self._current_time(connection)
        stored = (
            connection.execute(
                select(outbox_compaction_command_table).where(
                    outbox_compaction_command_table.c.operation_id == operation_id
                )
            )
            .mappings()
            .one_or_none()
        )
        if stored is None:
            raise PlatformError(
                "compaction_command_not_found",
                "Compaction command was not found",
                {},
                404,
            )
        if stored["state"] == "completed":
            return {"operation_id": operation_id, "state": "completed", "processed": False}
        if stored["state"] != "accepted":
            raise PlatformError(
                "compaction_command_invalid",
                "Compaction command is not accepted",
                {},
                409,
            )
        user_id = str(stored["user_id"])
        previous = dict(stored["receipt_json"] or {})
        previous_compacted = int(previous.get("compacted_count", 0))
        previous_eligible = int(previous.get("eligible_count", 0))
        event_ids = (
            connection.execute(
                select(outbox_recipient_table.c.event_id).where(
                    outbox_recipient_table.c.recipient_user_id == user_id
                )
            )
            .scalars()
            .all()
        )
        compacted_count = previous_compacted
        eligible_count = previous_eligible
        blocked_count = 0
        for event_id in event_ids:
            event = (
                connection.execute(
                    select(
                        outbox_event_table.c.storage_state,
                        outbox_event_table.c.compacted_at_utc,
                    ).where(outbox_event_table.c.event_id == event_id)
                )
                .mappings()
                .one_or_none()
            )
            if event is None or event["storage_state"] != "full":
                continue
            if event["compacted_at_utc"] is not None:
                continue
            non_delivered = connection.execute(
                select(func.count())
                .select_from(outbox_delivery_table)
                .where(
                    outbox_delivery_table.c.event_id == event_id,
                    outbox_delivery_table.c.status != "delivered",
                )
            ).scalar_one()
            if int(non_delivered) != 0:
                blocked_count += 1
                continue
            eligible_count += 1
            if compact_event(connection, event_id, now):
                compacted_count += 1
            else:
                blocked_count += 1
        completed = blocked_count == 0
        updated = connection.execute(
            update(outbox_compaction_command_table)
            .where(
                outbox_compaction_command_table.c.operation_id == operation_id,
                outbox_compaction_command_table.c.state == "accepted",
            )
            .values(
                state="completed" if completed else "accepted",
                receipt_json={
                    "eligible_count": eligible_count,
                    "compacted_count": compacted_count,
                    "blocked_count": blocked_count,
                },
                completed_at_utc=now if completed else stored["completed_at_utc"],
            )
        ).rowcount
        if updated != 1:
            raise PlatformError(
                "compaction_command_conflict",
                "Compaction command state changed before processing",
                {},
                409,
            )
        return {
            "operation_id": operation_id,
            "state": "completed" if completed else "accepted",
            "processed": True,
            "compacted_count": compacted_count,
            "blocked_count": blocked_count,
        }

    @staticmethod
    def _compaction_receipt_from_row(
        command: EligibleAccountEventCompactionCommand,
        row: dict[str, object],
    ) -> EligibleAccountEventCompactionReceipt:
        if (
            str(row["user_id"]) != command.user_id
            or str(row["deletion_id"]) != command.deletion_id
            or str(row["retirement_receipt_id"]) != command.retirement_receipt_id
            or str(row.get("input_fingerprint") or "") != _command_input_fingerprint(command)
        ):
            raise PlatformError(
                "idempotency_key_conflict",
                "Compaction operation conflicts with an existing operation",
                {},
                409,
            )
        raw_receipt = row.get("receipt_json")
        stored = dict(raw_receipt) if isinstance(raw_receipt, dict) else {}
        state = str(row["state"])
        return EligibleAccountEventCompactionReceipt(
            operation_id=command.operation_id,
            user_id=str(row["user_id"]),
            deletion_id=str(row["deletion_id"]),
            state=state,  # type: ignore[arg-type]
            eligible_count=int(stored.get("eligible_count", 0)),
            compacted_count=int(stored.get("compacted_count", 0)),
            blocked_count=int(stored.get("blocked_count", 0)),
            retryable=state == "accepted",
        )
