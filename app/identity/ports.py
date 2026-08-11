from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol

from sqlalchemy.engine import Connection


@dataclass(frozen=True, slots=True)
class DepartmentWorkState:
    nonterminal_job_count: int = 0
    pending_submission_count: int = 0


class DepartmentWorkCheckPort(Protocol):
    """Read-only boundary for work that prevents a department from deactivation."""

    def inspect(self, department_id: str, *, connection: Connection) -> DepartmentWorkState: ...


class NoopDepartmentWorkCheckPort:
    """Explicit test adapter for a deployment with verified empty work state."""

    def inspect(self, department_id: str, *, connection: Connection) -> DepartmentWorkState:
        del department_id, connection
        return DepartmentWorkState()


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
