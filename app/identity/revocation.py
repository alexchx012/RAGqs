from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol

from sqlalchemy import select
from sqlalchemy.engine import Connection

from app.platform.errors import PlatformError

from .schema import identity_revocation_command_table


@dataclass(frozen=True, slots=True)
class GenerationRevocationCommand:
    operation_id: str
    user_id: str
    auth_session_id: str | None
    reason: str
    revoked_at: datetime
    identity_transition_version: int


@dataclass(frozen=True, slots=True)
class GenerationRevocationReceipt:
    reference: str
    state: Literal["accepted", "completed"]


class GenerationRevocationPort(Protocol):
    """Chat-owned boundary for authorization-driven generation cancellation."""

    def revoke(
        self,
        command: GenerationRevocationCommand,
        *,
        connection: Connection,
    ) -> GenerationRevocationReceipt: ...


class NoopGenerationRevocationPort:
    """Explicit test adapter that acknowledges a revocation without a chat domain."""

    def revoke(
        self,
        command: GenerationRevocationCommand,
        *,
        connection: Connection,
    ) -> GenerationRevocationReceipt:
        del connection
        return GenerationRevocationReceipt(
            reference=f"identity-accepted:{command.operation_id}",
            state="accepted",
        )


class DurableGenerationRevocationPort:
    """Persists a generation-cancellation command for the generation workload to consume."""

    def revoke(
        self,
        command: GenerationRevocationCommand,
        *,
        connection: Connection,
    ) -> GenerationRevocationReceipt:
        existing = (
            connection.execute(
                select(identity_revocation_command_table).where(
                    identity_revocation_command_table.c.operation_id == command.operation_id
                )
            )
            .mappings()
            .one_or_none()
        )
        if existing is not None:
            return self._receipt_from_existing(command, dict(existing))
        receipt = GenerationRevocationReceipt(
            reference=f"generation-outbox:{command.operation_id}",
            state="accepted",
        )
        connection.execute(
            identity_revocation_command_table.insert().values(
                operation_id=command.operation_id,
                user_id=command.user_id,
                auth_session_id=command.auth_session_id,
                reason=command.reason,
                identity_transition_version=command.identity_transition_version,
                receipt_reference=receipt.reference,
                receipt_state=receipt.state,
                created_at_utc=command.revoked_at,
            )
        )
        return receipt

    @staticmethod
    def _receipt_from_existing(
        command: GenerationRevocationCommand,
        existing: dict[str, object],
    ) -> GenerationRevocationReceipt:
        fields_match = (
            str(existing["user_id"]) == command.user_id
            and existing["auth_session_id"] == command.auth_session_id
            and str(existing["reason"]) == command.reason
            and int(str(existing["identity_transition_version"]))
            == command.identity_transition_version
        )
        if not fields_match:
            raise PlatformError(
                "generation_revocation_conflict",
                "Generation revocation command conflicts with an existing operation",
                {},
                409,
            )
        reference = str(existing["receipt_reference"])
        state = str(existing["receipt_state"])
        if not reference.strip() or state not in {"accepted", "completed"}:
            raise PlatformError(
                "generation_revocation_unverified",
                "Generation revocation receipt is invalid",
                {"retryable": True},
                503,
                True,
            )
        if state == "accepted":
            return GenerationRevocationReceipt(reference=reference, state="accepted")
        return GenerationRevocationReceipt(reference=reference, state="completed")


class UnavailableGenerationRevocationPort:
    """Fail closed until a chat-owned revocation adapter is configured."""

    def revoke(
        self,
        command: GenerationRevocationCommand,
        *,
        connection: Connection,
    ) -> GenerationRevocationReceipt:
        del command, connection
        raise PlatformError(
            "generation_revocation_unavailable",
            "Generation revocation is not configured",
            {"retryable": True},
            503,
            True,
        )
