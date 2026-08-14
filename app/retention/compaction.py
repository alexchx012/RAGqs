"""Account event compaction requests driven after completed notification retirement.

Identity owns the deletion workflow; retention's only active step in the
account flow is requesting eligible event compaction through the outbox hook
once the retirement receipt exists, replaying the same stable operation ID
until outbox returns completed.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from app.platform.errors import PlatformError

from .ports import AccountCompactionPort
from .repository import SqlAlchemyRetentionRepository

# Caller/validation failures are terminal; conflicts and transient failures are
# replayed with the same operation ID so outbox remains the receipt authority.
_TERMINAL_CODES = {
    "forbidden",
    "validation_error",
    "unauthorized",
}


def compaction_operation_id(cleanup_operation_id: str) -> str:
    return f"compact:{cleanup_operation_id}"


class AccountCompactionRequester:
    def __init__(
        self,
        *,
        repository: SqlAlchemyRetentionRepository,
        port: AccountCompactionPort,
    ) -> None:
        self._repository = repository
        self._port = port

    def request_once(
        self,
        *,
        user_id: str,
        deletion_id: str,
        cleanup_operation_id: str,
        retirement_receipt_id: str,
    ) -> Mapping[str, Any]:
        operation_id = compaction_operation_id(cleanup_operation_id)
        existing = self._repository.get_receipt(operation_id)
        if existing is not None and existing["state"] in ("completed", "terminal"):
            return {
                "operation_id": operation_id,
                "state": existing["state"],
                "receipt_json": existing["receipt_json"],
            }
        try:
            receipt = self._port.request_compaction(
                operation_id=operation_id,
                user_id=user_id,
                deletion_id=deletion_id,
                retirement_receipt_id=retirement_receipt_id,
            )
        except PlatformError as error:
            state = "terminal" if error.code in _TERMINAL_CODES else "blocked"
            self._repository.store_receipt(
                operation_id=operation_id,
                kind="account_compaction",
                target_id=user_id,
                receipt_json={"operation_id": operation_id, "state": state},
                state=state,
                error=error.code,
            )
            return {"operation_id": operation_id, "state": state, "error": error.code}
        receipt_json = {
            "operation_id": receipt.operation_id,
            "user_id": receipt.user_id,
            "deletion_id": receipt.deletion_id,
            "state": receipt.state,
            "eligible_count": receipt.eligible_count,
            "compacted_count": receipt.compacted_count,
            "blocked_count": receipt.blocked_count,
            "retryable": receipt.retryable,
        }
        state = "completed" if receipt.state == "completed" else "accepted"
        self._repository.store_receipt(
            operation_id=operation_id,
            kind="account_compaction",
            target_id=user_id,
            receipt_json=receipt_json,
            state=state,
        )
        return {"operation_id": operation_id, "state": state, "receipt_json": receipt_json}
