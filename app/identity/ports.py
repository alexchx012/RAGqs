from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol

from sqlalchemy.engine import Connection


@dataclass(frozen=True, slots=True)
class DepartmentWorkState:
    document_count: int = 0
    nonterminal_job_count: int = 0
    pending_submission_count: int = 0


class DepartmentWorkCheckPort(Protocol):
    """Read-only boundary for work that prevents a department from deactivation."""

    def inspect(self, department_id: str, *, connection: Connection) -> DepartmentWorkState: ...

    def directory_counts(
        self, department_id: str, *, connection: Connection
    ) -> DepartmentWorkState: ...

    def user_document_counts(
        self, user_ids: Sequence[str], *, connection: Connection
    ) -> dict[str, int]: ...


class NoopDepartmentWorkCheckPort:
    """Explicit test adapter for a deployment with verified empty work state."""

    def inspect(self, department_id: str, *, connection: Connection) -> DepartmentWorkState:
        del department_id, connection
        return DepartmentWorkState()

    def directory_counts(
        self, department_id: str, *, connection: Connection
    ) -> DepartmentWorkState:
        del department_id, connection
        return DepartmentWorkState()

    def user_document_counts(
        self, user_ids: Sequence[str], *, connection: Connection
    ) -> dict[str, int]:
        del user_ids, connection
        return {}


class UnavailableDepartmentWorkCheckPort:
    """Fail closed until the document and ingestion domains provide their adapter."""

    def inspect(self, department_id: str, *, connection: Connection) -> DepartmentWorkState:
        del department_id, connection
        raise RuntimeError("Department work check is not configured")


@dataclass(frozen=True, slots=True)
class PendingSubmissionInvalidationCommand:
    """Identity-owned authorization facts for pending document submissions."""

    user_id: str
    role: str
    department_id: str | None
    lifecycle_status: Literal["active", "pending_delete", "deleted"]
    reason: str


class PendingSubmissionInvalidationPort(Protocol):
    """Invalidate submissions that the supplied identity may no longer contribute."""

    def invalidate_pending_submissions(
        self,
        command: PendingSubmissionInvalidationCommand,
        *,
        connection: Connection,
    ) -> int: ...


@dataclass(frozen=True, slots=True)
class AccountDeletionCleanupCommand:
    operation_id: str
    user_id: str
    requested_at: datetime
    purge_after: datetime


@dataclass(frozen=True, slots=True)
class AccountDeletionCleanupReceipt:
    reference: str
    state: Literal["completed"]


class AccountDeletionCleanupPort(Protocol):
    """Cleanup-owner boundary that confirms an expired account can be tombstoned."""

    def confirm_cleanup(
        self,
        command: AccountDeletionCleanupCommand,
        *,
        connection: Connection,
    ) -> AccountDeletionCleanupReceipt: ...


class NoopAccountDeletionCleanupPort:
    """Explicit test adapter that records a completed account cleanup."""

    def confirm_cleanup(
        self,
        command: AccountDeletionCleanupCommand,
        *,
        connection: Connection,
    ) -> AccountDeletionCleanupReceipt:
        del connection
        return AccountDeletionCleanupReceipt(
            reference=f"identity-cleanup:{command.operation_id}",
            state="completed",
        )


class UnavailableAccountDeletionCleanupPort:
    """Fail closed until the cleanup owner is configured in the runtime."""

    def confirm_cleanup(
        self,
        command: AccountDeletionCleanupCommand,
        *,
        connection: Connection,
    ) -> AccountDeletionCleanupReceipt:
        del command, connection
        raise RuntimeError("Account deletion cleanup is not configured")


@dataclass(frozen=True, slots=True)
class AccountRetirementRequest:
    operation_id: str
    user_id: str
    deletion_id: str
    verified_archive_ref: str
    archive_checksum: str
    transaction_id: str
    mode: str


class PersonalDocumentDeletionPort(Protocol):
    """Documents-owned boundary for §9.2.1 personal-space document deletion.

    Creates or reuses one document permanent-deletion workflow per personal
    document with the deterministic key ``(user_deletion_id, document_id)``
    and reports how many personal documents are not yet in the ``deleted``
    tombstone state.
    """

    def pending_personal_documents(
        self,
        connection: Connection,
        *,
        user_id: str,
        user_deletion_id: str,
    ) -> int: ...


class UnavailablePersonalDocumentDeletionPort:
    """Fail closed until the documents domain is wired into the runtime."""

    def pending_personal_documents(
        self,
        connection: Connection,
        *,
        user_id: str,
        user_deletion_id: str,
    ) -> int:
        del connection, user_id, user_deletion_id
        raise RuntimeError("Personal document deletion is not configured")


class NoopPersonalDocumentDeletionPort:
    """Explicit test adapter for a deployment with no personal documents."""

    def pending_personal_documents(
        self,
        connection: Connection,
        *,
        user_id: str,
        user_deletion_id: str,
    ) -> int:
        del connection, user_id, user_deletion_id
        return 0


@dataclass(frozen=True, slots=True)
class AccountRetirementConfirmation:
    state: str
    receipt_count: int


class AccountRetirementGateway(Protocol):
    """Outbox-owned boundary: obtain a completed account-retirement receipt."""

    def retire(
        self,
        request: AccountRetirementRequest,
        *,
        connection: Connection,
    ) -> AccountRetirementConfirmation: ...


class NoopAccountRetirementGateway:
    """Explicit test adapter that confirms retirement without an outbox."""

    def retire(
        self,
        request: AccountRetirementRequest,
        *,
        connection: Connection,
    ) -> AccountRetirementConfirmation:
        del connection
        return AccountRetirementConfirmation(state="completed", receipt_count=0)


class UnavailableAccountRetirementGateway:
    """Fail closed until the runtime bridges the outbox lifecycle."""

    def retire(
        self,
        request: AccountRetirementRequest,
        *,
        connection: Connection,
    ) -> AccountRetirementConfirmation:
        del request, connection
        raise RuntimeError("Account retirement gateway is not configured")
