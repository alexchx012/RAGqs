"""Account compaction requester tests with a fake outbox port."""

from __future__ import annotations

from retention_helpers import build_engine, fixed_now

from app.outbox.ports import EligibleAccountEventCompactionReceipt
from app.platform.database import core_metadata
from app.platform.errors import PlatformError
from app.retention.compaction import AccountCompactionRequester
from app.retention.repository import SqlAlchemyRetentionRepository
from app.retention.schema import retention_metadata


class FakeCompactionPort:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.error: PlatformError | None = None

    def request_compaction(
        self,
        *,
        operation_id: str,
        user_id: str,
        deletion_id: str,
        retirement_receipt_id: str,
    ) -> EligibleAccountEventCompactionReceipt:
        self.calls.append((operation_id, user_id))
        if self.error is not None:
            raise self.error
        return EligibleAccountEventCompactionReceipt(
            operation_id=operation_id,
            user_id=user_id,
            deletion_id=deletion_id,
            state="completed",
            eligible_count=3,
            compacted_count=3,
            blocked_count=0,
            retryable=False,
        )


def _requester(engine, port):
    repository = SqlAlchemyRetentionRepository(engine, now=lambda connection=None: fixed_now())
    return repository, AccountCompactionRequester(repository=repository, port=port)


def test_completed_receipt_is_stored_and_replayed_idempotently() -> None:
    engine = build_engine()
    core_metadata.create_all(engine)
    retention_metadata.create_all(engine)
    port = FakeCompactionPort()
    repository, requester = _requester(engine, port)
    first = requester.request_once(
        user_id="u_1",
        deletion_id="cleanup_1",
        cleanup_operation_id="cleanup_1",
        retirement_receipt_id="identity-retire:u_1:cleanup_1",
    )
    assert first["state"] == "completed"
    second = requester.request_once(
        user_id="u_1",
        deletion_id="cleanup_1",
        cleanup_operation_id="cleanup_1",
        retirement_receipt_id="identity-retire:u_1:cleanup_1",
    )
    assert second["state"] == "completed"
    assert len(port.calls) == 1
    receipt = repository.get_receipt("compact:cleanup_1")
    assert receipt is not None
    assert receipt["state"] == "completed"
    assert receipt["receipt_json"]["compacted_count"] == 3


def test_accepted_receipt_is_redriven_with_same_operation_id() -> None:
    engine = build_engine()
    core_metadata.create_all(engine)
    retention_metadata.create_all(engine)

    class AcceptedPort(FakeCompactionPort):
        def request_compaction(
            self,
            *,
            operation_id: str,
            user_id: str,
            deletion_id: str,
            retirement_receipt_id: str,
        ) -> EligibleAccountEventCompactionReceipt:
            self.calls.append((operation_id, user_id))
            blocked = len(self.calls) < 2
            return EligibleAccountEventCompactionReceipt(
                operation_id=operation_id,
                user_id=user_id,
                deletion_id=deletion_id,
                state="accepted" if blocked else "completed",
                eligible_count=3,
                compacted_count=0 if blocked else 3,
                blocked_count=3 if blocked else 0,
                retryable=blocked,
            )

    port = AcceptedPort()
    repository, requester = _requester(engine, port)
    first = requester.request_once(
        user_id="u_1",
        deletion_id="cleanup_1",
        cleanup_operation_id="cleanup_1",
        retirement_receipt_id="identity-retire:u_1:cleanup_1",
    )
    assert first["state"] == "accepted"
    second = requester.request_once(
        user_id="u_1",
        deletion_id="cleanup_1",
        cleanup_operation_id="cleanup_1",
        retirement_receipt_id="identity-retire:u_1:cleanup_1",
    )
    assert second["state"] == "completed"
    assert port.calls == [
        ("compact:cleanup_1", "u_1"),
        ("compact:cleanup_1", "u_1"),
    ]
    receipt = repository.get_receipt("compact:cleanup_1")
    assert receipt is not None
    assert receipt["state"] == "completed"


def test_forbidden_error_is_terminal_but_conflict_is_retryable() -> None:
    engine = build_engine()
    core_metadata.create_all(engine)
    retention_metadata.create_all(engine)
    port = FakeCompactionPort()
    port.error = PlatformError("forbidden", "no capability", {}, 403)
    repository, requester = _requester(engine, port)
    result = requester.request_once(
        user_id="u_1",
        deletion_id="cleanup_1",
        cleanup_operation_id="cleanup_1",
        retirement_receipt_id="identity-retire:u_1:cleanup_1",
    )
    assert result["state"] == "terminal"
    assert repository.get_receipt("compact:cleanup_1")["state"] == "terminal"

    port.error = PlatformError("compaction_prerequisite_missing", "missing", {}, 409)
    result = requester.request_once(
        user_id="u_2",
        deletion_id="cleanup_2",
        cleanup_operation_id="cleanup_2",
        retirement_receipt_id="identity-retire:u_2:cleanup_2",
    )
    assert result["state"] == "blocked"
    assert repository.get_receipt("compact:cleanup_2")["state"] == "blocked"
