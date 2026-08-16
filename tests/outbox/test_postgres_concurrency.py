"""I13: real two-connection PostgreSQL concurrency tests on production paths.

Each scenario drives the PRODUCTION code paths (claim, finalize, read-all,
retire, redact, fence, lifecycle reservation) over two database connections
synchronized with a barrier, so lock/serialization semantics are exercised
against a live PostgreSQL. Every test creates its own PostgreSQL schema and
drops it in a finally block so runs are independent and reliably clean; unique
usernames avoid cross-run collision. Skipped unless RAGQS_TEST_POSTGRES_URL is
configured.
"""

from __future__ import annotations

import os
import threading
import uuid
from urllib.parse import quote

import pytest
from _helpers import (
    alembic_config,
    build_identity_service,
    fixed_now,
    make_publisher,
    pg_test_schema_names,
    provision_user,
)
from sqlalchemy import create_engine, text

from app.outbox.dispatcher import OutboxDispatcher
from app.outbox.lifecycle import SqlAlchemyOutboxLifecycle
from app.outbox.metrics import SqlAlchemyOutboxMetrics
from app.outbox.notifications import NotificationMaterializer
from app.outbox.service import NotificationService

pytestmark = pytest.mark.integration


class _AcceptingArchiveVerifier:
    def verify_archive(self, *, archive_ref: str, checksum: str, **kwargs) -> bool:
        del archive_ref, checksum
        return True


def _pg_url() -> str | None:
    return os.environ.get("RAGQS_TEST_POSTGRES_URL")


def _scoped_url(url: str, schema: str) -> str:
    """A URL whose connections run with search_path set to the schema.

    The `options` libpq keyword is the standard way to scope every connection
    (including alembic's) to one schema without touching its driver.
    """
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}options=-c%20search_path%3D{quote(schema)}"


def _upgrade(url: str, schema: str) -> None:
    from alembic import command

    command.upgrade(alembic_config(_scoped_url(url, schema)), "head")


class _PgContext:
    """Per-test PostgreSQL schema: create, migrate to head, drop in finally."""

    def __init__(self) -> None:
        self.url = _pg_url()
        assert self.url is not None
        self.schema = f"outbox_it_{uuid.uuid4().hex[:16]}"
        self.engine = None  # set by __enter__; None when the context never ran

    def __enter__(self):
        admin = create_engine(self.url)
        try:
            with admin.begin() as connection:
                connection.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{self.schema}"'))
        finally:
            admin.dispose()
        try:
            _upgrade(self.url, self.schema)
        except BaseException:
            # The migration failed: the schema we just created must not leak.
            admin = create_engine(self.url)
            try:
                with admin.begin() as connection:
                    connection.execute(text(f'DROP SCHEMA IF EXISTS "{self.schema}" CASCADE'))
            finally:
                admin.dispose()
            raise
        self.engine = create_engine(_scoped_url(self.url, self.schema))
        return self.engine

    def __exit__(self, exc_type, exc, tb) -> None:
        del exc_type, exc, tb
        # Drop the scoped engine FIRST so no pooled connection to the temporary
        # schema outlives the test, then drop the schema itself.
        if self.engine is not None:
            self.engine.dispose()
        admin = create_engine(self.url)
        try:
            with admin.begin() as connection:
                connection.execute(text(f'DROP SCHEMA IF EXISTS "{self.schema}" CASCADE'))
        finally:
            admin.dispose()


class Barrier:
    def __init__(self, parties: int) -> None:
        self._parties = parties
        self._count = 0
        self._lock = threading.Lock()
        self._event = threading.Event()

    def wait(self) -> None:
        with self._lock:
            self._count += 1
            if self._count >= self._parties:
                self._event.set()
        self._event.wait(timeout=30)


class _ThreadResult:
    """Collects exceptions from worker threads so failures are never lost."""

    def __init__(self) -> None:
        self.error: BaseException | None = None

    def capture(self, fn):
        def wrapper(*args, **kwargs):
            try:
                fn(*args, **kwargs)
            except BaseException as exc:  # noqa: BLE001 - must surface in main thread
                self.error = exc

        return wrapper

    def assert_ok(self) -> None:
        if self.error is not None:
            raise AssertionError(f"worker thread failed: {self.error!r}") from self.error


def make_dispatcher(engine):
    return OutboxDispatcher(
        engine,
        consumers={"in_app_notification": NotificationMaterializer(engine)},
        now=lambda: fixed_now(),
        retention_days=30,
        notification_retention_days=90,
        metrics=SqlAlchemyOutboxMetrics(),
    )


def make_lifecycle(engine):
    return SqlAlchemyOutboxLifecycle(
        engine,
        now=lambda: fixed_now(),
        archive_verifier=_AcceptingArchiveVerifier(),
    )


def publish(engine, *, user_ids, event_id):
    from app.outbox.ports import OutboxPublishCommand, RecipientSelection

    publisher = make_publisher(engine, now=lambda: fixed_now())
    command = OutboxPublishCommand(
        event_id=event_id,
        caller_principal="ingestion",
        event_type="ingestion_completed",
        schema_version=1,
        aggregate_type="ingestion_job",
        aggregate_id=f"job_{event_id}",
        transition_version=1,
        occurred_at=fixed_now(),
        payload={
            "job_id": f"job_{event_id}",
            "document_id": f"doc_{event_id}",
            "document_version_id": f"docv_{event_id}",
            "publication_id": f"pub_{event_id}",
        },
        trace_id="t",
        recipients=tuple(RecipientSelection(recipient_user_id=u) for u in user_ids),
    )
    with engine.begin() as connection:
        publisher.publish(command, connection=connection)


def unique(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


@pytest.mark.skipif(not _pg_url(), reason="PostgreSQL integration environment is not configured")
def test_claim_competition_serializes_on_skip_locked() -> None:
    with _PgContext() as engine:
        identity = build_identity_service(engine)
        alice = provision_user(identity, username=unique("alice"))
        event_id = unique("evt")
        publish(engine, user_ids=(alice,), event_id=event_id)
        dispatcher = make_dispatcher(engine)
        barrier = Barrier(2)
        result = _ThreadResult()
        claims: list = []

        @result.capture
        def worker_one():
            barrier.wait()
            claims.append(dispatcher.claim_one(owner="worker-1"))

        @result.capture
        def worker_two():
            barrier.wait()
            claims.append(dispatcher.claim_one(owner="worker-2"))

        t1 = threading.Thread(target=worker_one)
        t2 = threading.Thread(target=worker_two)
        t1.start()
        t2.start()
        t1.join(timeout=30)
        t2.join(timeout=30)
        result.assert_ok()
        assert not t1.is_alive() and not t2.is_alive()
        assert len([c for c in claims if c is not None]) == 1
        with engine.connect() as connection:
            status = connection.execute(
                text("SELECT status FROM outbox_delivery WHERE event_id = :eid"),
                {"eid": event_id},
            ).scalar_one()
            assert status == "running"


@pytest.mark.skipif(not _pg_url(), reason="PostgreSQL integration environment is not configured")
def _race_deterministic(*, winner_fn, loser_fn, engine=None, block_check_seconds: float = 2.0):
    """Deterministic two-connection race.

    `winner_fn(holding, release)` runs the production method inside an
    UNCOMMITTED transaction, signals `holding` once the operation lock /
    reservation row is held, then waits on `release` before committing.
    `loser_fn(holding, loser_ready, loser_engine)` waits for `holding`,
    signals `loser_ready` immediately BEFORE entering its production call,
    then runs it on the dedicated `loser_engine` (whose connections carry a
    unique `application_name`).

    The main thread waits for `loser_ready` (so the loser is provably in
    flight, not merely un-scheduled), then — when `engine` is provided —
    POLLS `pg_stat_activity` until the loser's session shows
    `wait_event_type = 'Lock'`, proving it is blocked in a database lock wait
    held by the winner. Without `engine`, the loser thread must still be
    alive after a poll window (never just a single sleep + is_alive).

    Returns (winner_outcome, loser_outcome) dicts.
    """
    import time

    holding = threading.Event()
    loser_ready = threading.Event()
    release = threading.Event()
    result = _ThreadResult()
    winner_outcome: dict = {}
    loser_outcome: dict = {}
    loser_engine = None
    if engine is not None:
        loser_app = f"race_loser_{uuid.uuid4().hex[:8]}"
        # Pass the SQLAlchemy URL OBJECT straight to create_engine: re-rendering
        # a DSN string would serialize the credential into plaintext (and
        # str(engine.url) masks it), and a URL object keeps the options query.
        loser_engine = create_engine(
            engine.url,
            connect_args={"application_name": loser_app},
        )

    t_winner: threading.Thread | None = None
    t_loser: threading.Thread | None = None
    try:

        @result.capture
        def _winner():
            winner_outcome["value"] = winner_fn(holding, release)

        @result.capture
        def _loser():
            loser_outcome["value"] = loser_fn(holding, loser_ready, loser_engine)

        t_winner = threading.Thread(target=_winner)
        t_winner.start()
        assert holding.wait(timeout=30), "winner never acquired the operation lock"
        t_loser = threading.Thread(target=_loser)
        t_loser.start()
        assert loser_ready.wait(timeout=30), "loser never entered its production call"
        if loser_engine is not None:
            # Prove the loser waits on a lock held by the winner via
            # pg_stat_activity: advisory-lock and row-lock waits report
            # wait_event_type = 'Lock'.
            deadline = time.monotonic() + block_check_seconds
            saw_lock_wait = False
            while time.monotonic() < deadline:
                with engine.connect() as connection:
                    row = connection.execute(
                        text(
                            "SELECT wait_event_type FROM pg_stat_activity "
                            "WHERE application_name = :app"
                        ),
                        {"app": loser_app},
                    ).scalar_one_or_none()
                if row == "Lock":
                    saw_lock_wait = True
                    break
                time.sleep(0.05)
            assert saw_lock_wait, (
                "loser must be observed waiting on a Lock in pg_stat_activity "
                "while the winner holds the operation lock"
            )
        else:
            time.sleep(block_check_seconds)
            assert t_loser.is_alive(), "loser must block while the winner holds the lock"
    finally:
        # Every failure path (holding/loser_ready timeout, observation
        # assertion) must still release the blocked winner, join every started
        # thread and dispose the loser engine, so no uncommitted transaction
        # or connection outlives the race and blocks the schema drop.
        release.set()
        if t_winner is not None:
            t_winner.join(timeout=60)
        if t_loser is not None:
            t_loser.join(timeout=60)
        if loser_engine is not None:
            loser_engine.dispose()
    result.assert_ok()
    assert not t_winner.is_alive() and not t_loser.is_alive()
    return winner_outcome, loser_outcome


def test_race_deterministic_captures_loser_outcome_and_exceptions() -> None:
    """Unit proof that `_race_deterministic` stores the loser's return value
    in `loser_outcome['value']` and surfaces loser exceptions — runs locally
    (no PostgreSQL) via the thread-alive fallback path."""

    def make_winner(winner_done):
        def winner_fn(holding, release):
            holding.set()
            release.wait(timeout=30)
            winner_done.set()
            return "winner-result"

        return winner_fn

    # Case 1: the loser returns a value after the winner releases; it must be
    # captured into loser_outcome["value"].
    winner_done_1 = threading.Event()

    def loser_ok(holding, loser_ready, loser_engine):
        del loser_engine
        holding.wait(timeout=30)
        loser_ready.set()
        winner_done_1.wait(timeout=30)  # blocked until the winner releases
        return "loser-result"

    _, loser = _race_deterministic(
        winner_fn=make_winner(winner_done_1),
        loser_fn=loser_ok,
        engine=None,
        block_check_seconds=0.3,
    )
    assert loser["value"] == "loser-result"

    # Case 2: a loser that raises inside its production call surfaces the
    # exception through _ThreadResult.assert_ok (never silently swallowed).
    winner_done_2 = threading.Event()

    def loser_boom(holding, loser_ready, loser_engine):
        del loser_engine
        holding.wait(timeout=30)
        loser_ready.set()
        winner_done_2.wait(timeout=30)
        raise RuntimeError("boom-loser")

    with pytest.raises(AssertionError) as raised:
        _race_deterministic(
            winner_fn=make_winner(winner_done_2),
            loser_fn=loser_boom,
            engine=None,
            block_check_seconds=0.3,
        )
    assert "boom-loser" in str(raised.value)


@pytest.mark.skipif(not _pg_url(), reason="PostgreSQL integration environment is not configured")
def test_lifecycle_same_operation_serializes_on_the_reservation_row() -> None:
    """Deterministic redaction same-op race: the winner runs the production
    method inside an uncommitted transaction and holds the reservation row;
    the loser is provably BLOCKED until the winner commits, then returns the
    winner's real receipt. Exactly one tombstone and one receipt row."""
    from app.outbox.ports import DocumentNotificationRedactionCommand

    with _PgContext() as engine:
        identity = build_identity_service(engine)
        alice = provision_user(identity, username=unique("alice"))
        event_id = unique("evt")
        publish(engine, user_ids=(alice,), event_id=event_id)
        dispatcher = make_dispatcher(engine)
        claim = dispatcher.claim_one(owner="worker-1")
        assert claim is not None
        dispatcher.run_consumer_and_finalize(claim, owner="worker-1")
        lifecycle = make_lifecycle(engine)
        deletion_id = unique("del")
        transaction_id = f"documents-delete:{unique('tx')}"
        command = DocumentNotificationRedactionCommand(
            operation_id=unique("op_race"),
            caller_principal="documents",
            deletion_id=deletion_id,
            document_id=f"doc_{event_id}",
            document_version_ids=(f"docv_{event_id}",),
            reason="document_pending_delete",
            transaction_id=transaction_id,
            mode="inline",
            canonical_input_fingerprint="unused",
        )

        def winner_fn(holding, release):
            with engine.begin() as connection:
                receipt = lifecycle.redact_document_notifications(command, connection=connection)
                holding.set()
                release.wait(timeout=30)
                return receipt

        def loser_fn(holding, loser_ready, loser_engine):
            holding.wait(timeout=30)
            loser_ready.set()
            with loser_engine.begin() as connection:
                return lifecycle.redact_document_notifications(command, connection=connection)

        _, loser = _race_deterministic(winner_fn=winner_fn, loser_fn=loser_fn, engine=engine)
        # The loser returns the WINNER's real receipt.
        assert loser["value"].redacted_notification_count == 1
        assert loser["value"].state == "completed"
        with engine.connect() as connection:
            tombstones = connection.execute(
                text("SELECT COUNT(*) FROM outbox_document_tombstone WHERE document_id = :did"),
                {"did": f"doc_{event_id}"},
            ).scalar_one()
            assert tombstones == 1
            receipt_rows = connection.execute(
                text("SELECT COUNT(*) FROM outbox_redaction_receipt")
            ).scalar_one()
            assert receipt_rows == 1


@pytest.mark.skipif(not _pg_url(), reason="PostgreSQL integration environment is not configured")
def test_lifecycle_same_operation_different_input_is_409_for_the_loser() -> None:
    """Deterministic redaction different-input race: the winner holds the
    reservation; the loser blocks until release, then receives a permanent
    409 idempotency_key_conflict."""
    from app.outbox.ports import DocumentNotificationRedactionCommand

    with _PgContext() as engine:
        identity = build_identity_service(engine)
        alice = provision_user(identity, username=unique("alice"))
        event_id = unique("evt")
        publish(engine, user_ids=(alice,), event_id=event_id)
        dispatcher = make_dispatcher(engine)
        claim = dispatcher.claim_one(owner="worker-1")
        assert claim is not None
        dispatcher.run_consumer_and_finalize(claim, owner="worker-1")
        lifecycle = make_lifecycle(engine)
        deletion_a = unique("del")
        deletion_b = unique("del")
        transaction_id = f"documents-delete:{unique('tx')}"
        operation_id = unique("op_conflict")

        def build_command(deletion_id: str):
            return DocumentNotificationRedactionCommand(
                operation_id=operation_id,
                caller_principal="documents",
                deletion_id=deletion_id,
                document_id=f"doc_{event_id}",
                document_version_ids=(f"docv_{event_id}",),
                reason="document_pending_delete",
                transaction_id=transaction_id,
                mode="inline",
                canonical_input_fingerprint="unused",
            )

        def winner_fn(holding, release):
            with engine.begin() as connection:
                receipt = lifecycle.redact_document_notifications(
                    build_command(deletion_a), connection=connection
                )
                holding.set()
                release.wait(timeout=30)
                return receipt

        def loser_fn(holding, loser_ready, loser_engine):
            holding.wait(timeout=30)
            loser_ready.set()
            with loser_engine.begin() as connection:
                try:
                    lifecycle.redact_document_notifications(
                        build_command(deletion_b), connection=connection
                    )
                    return ("ok", None)
                except BaseException as exc:  # noqa: BLE001 - surfaced to main thread
                    return ("error", exc)

        _, loser = _race_deterministic(winner_fn=winner_fn, loser_fn=loser_fn, engine=engine)
        assert loser["value"][0] == "error"
        assert getattr(loser["value"][1], "status_code", None) == 409
        with engine.connect() as connection:
            receipt_rows = connection.execute(
                text("SELECT COUNT(*) FROM outbox_redaction_receipt")
            ).scalar_one()
            assert receipt_rows == 1


@pytest.mark.skipif(not _pg_url(), reason="PostgreSQL integration environment is not configured")
def test_lifecycle_retirement_same_operation_serializes_on_the_reservation_row() -> None:
    """Deterministic retirement same-op race: winner holds the reservation
    row uncommitted; loser blocks until release and returns the winner's real
    receipt; exactly one command row and one receipt row are committed."""
    from app.outbox.ports import AccountNotificationRetirementCommand

    with _PgContext() as engine:
        identity = build_identity_service(engine)
        alice = provision_user(identity, username=unique("alice"))
        event_id = unique("evt")
        publish(engine, user_ids=(alice,), event_id=event_id)
        dispatcher = make_dispatcher(engine)
        claim = dispatcher.claim_one(owner="worker-1")
        assert claim is not None
        dispatcher.run_consumer_and_finalize(claim, owner="worker-1")
        lifecycle = make_lifecycle(engine)
        _mark_pending_delete(engine, alice)
        operation_id = unique("op_ret_race")
        deletion_id = unique("del")
        command = AccountNotificationRetirementCommand(
            operation_id=operation_id,
            caller_principal="retention-ops",
            user_id=alice,
            deletion_id=deletion_id,
            verified_archive_ref="archive_ref_race",
            archive_checksum="checksum_race",
            transaction_id="tx_race",
            mode="inline",
            canonical_input_fingerprint="unused",
        )

        def winner_fn(holding, release):
            with engine.begin() as connection:
                receipt = lifecycle.retire_account_notification_state(
                    command, connection=connection
                )
                holding.set()
                release.wait(timeout=30)
                return receipt

        def loser_fn(holding, loser_ready, loser_engine):
            holding.wait(timeout=30)
            loser_ready.set()
            with loser_engine.begin() as connection:
                return lifecycle.retire_account_notification_state(command, connection=connection)

        _, loser = _race_deterministic(winner_fn=winner_fn, loser_fn=loser_fn, engine=engine)
        assert loser["value"].notification_retired_count == 1
        assert loser["value"].state == "completed"
        with engine.connect() as connection:
            command_rows = connection.execute(
                text("SELECT COUNT(*) FROM outbox_retirement_command")
            ).scalar_one()
            assert command_rows == 1
            receipt_rows = connection.execute(
                text("SELECT COUNT(*) FROM notification_delivery_receipt")
            ).scalar_one()
            assert receipt_rows == 1


@pytest.mark.skipif(not _pg_url(), reason="PostgreSQL integration environment is not configured")
def test_lifecycle_retirement_same_operation_different_input_is_409() -> None:
    """Deterministic retirement different-input race: loser blocks until the
    winner commits, then receives a permanent 409."""
    from app.outbox.ports import AccountNotificationRetirementCommand

    with _PgContext() as engine:
        identity = build_identity_service(engine)
        alice = provision_user(identity, username=unique("alice"))
        event_id = unique("evt")
        publish(engine, user_ids=(alice,), event_id=event_id)
        dispatcher = make_dispatcher(engine)
        claim = dispatcher.claim_one(owner="worker-1")
        assert claim is not None
        dispatcher.run_consumer_and_finalize(claim, owner="worker-1")
        lifecycle = make_lifecycle(engine)
        _mark_pending_delete(engine, alice)
        operation_id = unique("op_ret_conflict")

        def build_command(deletion_id: str):
            return AccountNotificationRetirementCommand(
                operation_id=operation_id,
                caller_principal="retention-ops",
                user_id=alice,
                deletion_id=deletion_id,
                verified_archive_ref="archive_ref_race",
                archive_checksum="checksum_race",
                transaction_id="tx_race",
                mode="inline",
                canonical_input_fingerprint="unused",
            )

        def winner_fn(holding, release):
            with engine.begin() as connection:
                receipt = lifecycle.retire_account_notification_state(
                    build_command(unique("del")), connection=connection
                )
                holding.set()
                release.wait(timeout=30)
                return receipt

        def loser_fn(holding, loser_ready, loser_engine):
            holding.wait(timeout=30)
            loser_ready.set()
            with loser_engine.begin() as connection:
                try:
                    lifecycle.retire_account_notification_state(
                        build_command(unique("del")), connection=connection
                    )
                    return ("ok", None)
                except BaseException as exc:  # noqa: BLE001 - surfaced to main thread
                    return ("error", exc)

        _, loser = _race_deterministic(winner_fn=winner_fn, loser_fn=loser_fn, engine=engine)
        assert loser["value"][0] == "error"
        assert getattr(loser["value"][1], "status_code", None) == 409


@pytest.mark.skipif(not _pg_url(), reason="PostgreSQL integration environment is not configured")
def test_lifecycle_compaction_same_operation_serializes_on_the_reservation_row() -> None:
    """Deterministic compaction same-op race: the winner holds the compaction
    reservation row uncommitted; the loser blocks until release and returns
    the winner's real receipt; the event is compacted exactly once."""
    from app.outbox.lifecycle import _command_input_fingerprint
    from app.outbox.ports import (
        AccountNotificationRetirementCommand,
        EligibleAccountEventCompactionCommand,
    )

    with _PgContext() as engine:
        identity = build_identity_service(engine)
        alice = provision_user(identity, username=unique("alice"))
        event_id = unique("evt")
        publish(engine, user_ids=(alice,), event_id=event_id)
        dispatcher = make_dispatcher(engine)
        claim = dispatcher.claim_one(owner="worker-1")
        assert claim is not None
        dispatcher.run_consumer_and_finalize(claim, owner="worker-1")
        lifecycle = make_lifecycle(engine)
        _mark_pending_delete(engine, alice)
        retirement = AccountNotificationRetirementCommand(
            operation_id=unique("op_ret"),
            caller_principal="retention-ops",
            user_id=alice,
            deletion_id=unique("del"),
            verified_archive_ref="archive_ref_comp",
            archive_checksum="checksum_comp",
            transaction_id="tx_comp",
            mode="inline",
            canonical_input_fingerprint="unused",
        )
        with engine.begin() as connection:
            lifecycle.retire_account_notification_state(retirement, connection=connection)
        retirement_fingerprint = _command_input_fingerprint(retirement)
        operation_id = unique("op_comp_race")
        command = EligibleAccountEventCompactionCommand(
            operation_id=operation_id,
            caller_principal="retention-ops",
            user_id=alice,
            deletion_id=retirement.deletion_id,
            retirement_receipt_id=retirement.operation_id,
            retirement_receipt_fingerprint=retirement_fingerprint,
            transaction_id="tx_comp_race",
            canonical_input_fingerprint="unused",
        )

        def winner_fn(holding, release):
            with engine.begin() as connection:
                receipt = lifecycle.request_eligible_account_event_compaction(
                    command, connection=connection
                )
                holding.set()
                release.wait(timeout=30)
                return receipt

        def loser_fn(holding, loser_ready, loser_engine):
            holding.wait(timeout=30)
            loser_ready.set()
            with loser_engine.begin() as connection:
                return lifecycle.request_eligible_account_event_compaction(
                    command, connection=connection
                )

        _, loser = _race_deterministic(winner_fn=winner_fn, loser_fn=loser_fn, engine=engine)
        assert loser["value"].compacted_count == 1
        assert loser["value"].state == "completed"
        with engine.connect() as connection:
            command_rows = connection.execute(
                text("SELECT COUNT(*) FROM outbox_compaction_command")
            ).scalar_one()
            assert command_rows == 1
            state = connection.execute(
                text("SELECT storage_state FROM outbox_event WHERE event_id = :eid"),
                {"eid": event_id},
            ).scalar_one()
            assert state == "compacted"


@pytest.mark.skipif(not _pg_url(), reason="PostgreSQL integration environment is not configured")
def test_redaction_and_materialization_serialize_on_the_document_lock() -> None:
    """Deterministic redaction-vs-materialization race on the per-document
    advisory lock: the winner (redaction) holds the lock uncommitted; the
    loser (dispatcher materialization on its own tagged connection) blocks
    until release and then renders the DELETED-document projection — original
    text can never reappear."""
    from app.outbox.ports import DocumentNotificationRedactionCommand

    with _PgContext() as engine:
        identity = build_identity_service(engine)
        alice = provision_user(identity, username=unique("alice"))
        event_id = unique("evt")
        publish(engine, user_ids=(alice,), event_id=event_id)
        lifecycle = make_lifecycle(engine)
        deletion_id = unique("del")
        transaction_id = f"documents-delete:{unique('tx')}"
        command = DocumentNotificationRedactionCommand(
            operation_id=unique("op_red_mat"),
            caller_principal="documents",
            deletion_id=deletion_id,
            document_id=f"doc_{event_id}",
            document_version_ids=(f"docv_{event_id}",),
            reason="document_pending_delete",
            transaction_id=transaction_id,
            mode="inline",
            canonical_input_fingerprint="unused",
        )

        def winner_fn(holding, release):
            with engine.begin() as connection:
                receipt = lifecycle.redact_document_notifications(command, connection=connection)
                holding.set()
                release.wait(timeout=30)
                return receipt

        def loser_fn(holding, loser_ready, loser_engine):
            holding.wait(timeout=30)
            loser_ready.set()
            dispatcher = make_dispatcher(loser_engine)
            claim = dispatcher.claim_one(owner="worker-mat")
            if claim is None:
                return None
            return dispatcher.run_consumer_and_finalize(claim, owner="worker-mat")

        _, loser = _race_deterministic(winner_fn=winner_fn, loser_fn=loser_fn, engine=engine)
        assert loser["value"].status == "delivered"
        with engine.connect() as connection:
            row = (
                connection.execute(
                    text("SELECT title, redacted FROM notification WHERE event_id = :eid"),
                    {"eid": event_id},
                )
                .mappings()
                .one()
            )
            assert row["title"] == "Deleted document"
            assert row["redacted"] is True


@pytest.mark.skipif(not _pg_url(), reason="PostgreSQL integration environment is not configured")
def test_retirement_and_materialization_serialize_on_the_user_lock() -> None:
    """Deterministic retirement-vs-materialization race on the per-user
    advisory lock: the winner (retirement) holds the lock uncommitted; the
    loser (dispatcher materialization on its own tagged connection) blocks
    until release and then sees the retired tombstone, so it suppresses
    instead of rebuilding the inbox."""
    from app.outbox.ports import AccountNotificationRetirementCommand

    with _PgContext() as engine:
        identity = build_identity_service(engine)
        alice = provision_user(identity, username=unique("alice"))
        event_id = unique("evt")
        publish(engine, user_ids=(alice,), event_id=event_id)
        lifecycle = make_lifecycle(engine)
        _mark_pending_delete(engine, alice)
        command = AccountNotificationRetirementCommand(
            operation_id=unique("op_ret_mat"),
            caller_principal="retention-ops",
            user_id=alice,
            deletion_id=unique("del"),
            verified_archive_ref="archive_ref_race",
            archive_checksum="checksum_race",
            transaction_id="tx_race",
            mode="inline",
            canonical_input_fingerprint="unused",
        )

        def winner_fn(holding, release):
            with engine.begin() as connection:
                receipt = lifecycle.retire_account_notification_state(
                    command, connection=connection
                )
                holding.set()
                release.wait(timeout=30)
                return receipt

        def loser_fn(holding, loser_ready, loser_engine):
            holding.wait(timeout=30)
            loser_ready.set()
            dispatcher = make_dispatcher(loser_engine)
            claim = dispatcher.claim_one(owner="worker-mat")
            if claim is None:
                return None
            return dispatcher.run_consumer_and_finalize(claim, owner="worker-mat")

        _, loser = _race_deterministic(winner_fn=winner_fn, loser_fn=loser_fn, engine=engine)
        assert loser["value"].status == "delivered"
        with engine.connect() as connection:
            # The retired tombstone suppressed the later materialization.
            assert connection.execute(text("SELECT * FROM notification")).all() == []
            assert (
                connection.execute(
                    text(
                        "SELECT COUNT(*) FROM outbox_account_retirement_tombstone "
                        "WHERE recipient_user_id = :uid"
                    ),
                    {"uid": alice},
                ).scalar_one()
                == 1
            )
            assert (
                connection.execute(
                    text(
                        "SELECT COUNT(*) FROM notification_suppression "
                        "WHERE recipient_user_id = :uid"
                    ),
                    {"uid": alice},
                ).scalar_one()
                == 1
            )


@pytest.mark.skipif(not _pg_url(), reason="PostgreSQL integration environment is not configured")
def test_read_all_and_materialization_serialize_on_the_user_lock_barrier() -> None:
    """Deterministic read-all-vs-materialization race on the per-user advisory
    lock: the winner calls read_all on its OWN caller-supplied connection
    inside an uncommitted transaction, acquires the advisory lock and keeps it
    held; the loser (materialization on its own tagged connection) provably
    blocks until release. Since read-all commits before the notification
    exists, its watermark stays 0 and the notification materializes unread."""
    with _PgContext() as engine:
        identity = build_identity_service(engine)
        alice = provision_user(identity, username=unique("alice"))
        event_id = unique("evt")
        publish(engine, user_ids=(alice,), event_id=event_id)
        service = NotificationService(engine, now=lambda: fixed_now())

        def winner_fn(holding, release):
            # read_all participates in the caller's transaction: the advisory
            # lock stays held until this transaction commits/rolls back.
            connection = engine.connect()
            trans = connection.begin()
            try:
                service.read_all(alice, connection=connection)
                holding.set()
                release.wait(timeout=30)
            finally:
                trans.commit()
                connection.close()
            return None

        def loser_fn(holding, loser_ready, loser_engine):
            holding.wait(timeout=30)
            loser_ready.set()
            dispatcher = make_dispatcher(loser_engine)
            claim = dispatcher.claim_one(owner="worker-mat")
            if claim is None:
                return None
            return dispatcher.run_consumer_and_finalize(claim, owner="worker-mat")

        _, loser = _race_deterministic(winner_fn=winner_fn, loser_fn=loser_fn, engine=engine)
        assert loser["value"].status == "delivered"
        items = service.list_notifications(alice, limit=50)
        assert len(items) == 1
        assert items[0]["read"] is False
        with engine.connect() as connection:
            read_through = connection.execute(
                text(
                    "SELECT read_through_seq FROM notification_inbox "
                    "WHERE recipient_user_id = :uid"
                ),
                {"uid": alice},
            ).scalar_one()
            assert read_through == 0


def _mark_pending_delete(engine, user_id: str) -> None:
    with engine.begin() as connection:
        connection.execute(
            text("UPDATE identity_user SET lifecycle_status = 'pending_delete' WHERE id = :uid"),
            {"uid": user_id},
        )


@pytest.mark.skipif(not _pg_url(), reason="PostgreSQL integration environment is not configured")
def test_consumer_failure_finalizes_and_lease_expiry_recycles_the_attempt() -> None:
    from datetime import timedelta

    with _PgContext() as engine:
        identity = build_identity_service(engine)
        alice = provision_user(identity, username=unique("alice"))
        event_id = unique("evt")
        publish(engine, user_ids=(alice,), event_id=event_id)

        class _MutableClock:
            def __init__(self, start):
                self._current = start

            def now_utc(self, connection=None):
                del connection
                return self._current

            def advance(self, seconds):
                self._current = self._current + timedelta(seconds=seconds)

        clock = _MutableClock(fixed_now())

        class _Exploding:
            def materialize(self, connection, **kwargs):
                del connection, kwargs
                raise RuntimeError("boom")

        dispatcher = OutboxDispatcher(
            engine,
            consumers={"in_app_notification": _Exploding()},
            now=clock.now_utc,
            clock=clock,
            retention_days=30,
            notification_retention_days=90,
            metrics=SqlAlchemyOutboxMetrics(),
        )
        claim = dispatcher.claim_one(owner="worker-1")
        assert claim is not None
        outcome = dispatcher.run_consumer_and_finalize(claim, owner="worker-1")
        assert outcome.status == "failed"
        with engine.connect() as connection:
            status = connection.execute(
                text("SELECT status FROM outbox_delivery WHERE event_id = :eid"),
                {"eid": event_id},
            ).scalar_one()
            assert status == "retry_wait"
            attempt = (
                connection.execute(
                    text(
                        "SELECT status, error_category, ended_at_utc "
                        "FROM outbox_delivery_attempt WHERE event_id = :eid"
                    ),
                    {"eid": event_id},
                )
                .mappings()
                .one()
            )
            assert attempt["status"] == "failed"
            assert attempt["error_category"] == "retryable"
            assert attempt["ended_at_utc"] is not None

        # Advance past the retry delay so the retry_wait delivery is claimable
        # again; claim it so a fresh running attempt exists.
        clock.advance(seconds=300)
        claim2 = dispatcher.claim_one(owner="worker-1")
        assert claim2 is not None

        # Expire the running lease deterministically, then recycle: the expired
        # attempt is finalized with a legal error summary.
        with engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE outbox_delivery SET lease_expires_at_utc = "
                    "TIMESTAMP '2020-01-01 00:00:00' WHERE event_id = :eid"
                ),
                {"eid": event_id},
            )
        assert dispatcher.recycle_expired_running() == 1
        with engine.connect() as connection:
            # The event already carries the earlier failed attempt, so target
            # the latest attempt row.
            attempt = (
                connection.execute(
                    text(
                        "SELECT status, error_category, error_code "
                        "FROM outbox_delivery_attempt WHERE event_id = :eid "
                        "ORDER BY attempt_number DESC LIMIT 1"
                    ),
                    {"eid": event_id},
                )
                .mappings()
                .one()
            )
            assert attempt["status"] == "expired"
            assert attempt["error_category"] == "lease_expired"


@pytest.mark.skipif(not _pg_url(), reason="PostgreSQL integration environment is not configured")
def test_trigger_guards_reject_and_allow_on_postgres() -> None:
    """The live trigger guards enforce the complete invariant set: real
    identity changes are rejected, no-op updates are allowed, and the attempt
    terminal transition requires a legal summary."""
    from sqlalchemy.exc import ProgrammingError

    with _PgContext() as engine:
        identity = build_identity_service(engine)
        alice = provision_user(identity, username=unique("alice"))
        event_id = unique("evt")
        publish(engine, user_ids=(alice,), event_id=event_id)

        # trace_id is immutable on a full event.
        with pytest.raises(ProgrammingError):
            with engine.begin() as connection:
                connection.execute(
                    text("UPDATE outbox_event SET trace_id = 'other' WHERE event_id = :eid"),
                    {"eid": event_id},
                )
        # A no-op update is allowed.
        with engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE outbox_event SET occurred_at_utc = occurred_at_utc "
                    "WHERE event_id = :eid"
                ),
                {"eid": event_id},
            )
        # Recipient rows cannot be deleted while full.
        with pytest.raises(ProgrammingError):
            with engine.begin() as connection:
                connection.execute(
                    text("DELETE FROM outbox_recipient WHERE event_id = :eid"),
                    {"eid": event_id},
                )
        # An attempt insert followed by a delivered transition WITHOUT an
        # error summary is fine; a delivered transition WITH one is rejected.
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO outbox_delivery_attempt "
                    "(delivery_attempt_id, event_id, consumer_name, replay_generation, "
                    "attempt_number, cycle_attempt_number, fence_token, started_at_utc, status) "
                    "VALUES (:aid, :eid, 'in_app_notification', 1, 1, 1, 1, now(), 'running')"
                ),
                {"aid": unique("attempt"), "eid": event_id},
            )
        with pytest.raises(ProgrammingError):
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "UPDATE outbox_delivery_attempt SET status = 'delivered', "
                        "ended_at_utc = now(), error_category = 'retryable' "
                        "WHERE event_id = :eid"
                    ),
                    {"eid": event_id},
                )
        # The legal delivered transition succeeds.
        with engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE outbox_delivery_attempt SET status = 'delivered', ended_at_utc = now() "
                    "WHERE event_id = :eid"
                ),
                {"eid": event_id},
            )


@pytest.mark.skipif(not _pg_url(), reason="PostgreSQL integration environment is not configured")
def test_pg_context_disposes_scoped_engine_before_dropping_schema() -> None:
    """__exit__ must dispose the scoped engine (before dropping the schema) so
    no pooled connection to the temporary schema outlives the test."""
    context = _PgContext()
    engine = context.__enter__()
    try:
        # Return a checked-out connection to the pool so dispose is observable.
        with engine.connect() as connection:
            connection.close()
    finally:
        context.__exit__(None, None, None)
    assert engine.pool.checkedin() == 0
    assert context.schema not in pg_test_schema_names()


@pytest.mark.skipif(not _pg_url(), reason="PostgreSQL integration environment is not configured")
def test_race_deterministic_passes_url_object_to_loser_engine(monkeypatch) -> None:
    """The loser engine must be constructed from the SQLAlchemy URL OBJECT —
    never from a re-rendered DSN string — so the credential is not serialized
    into plaintext and the loser session can actually authenticate."""
    import sys

    captured: dict = {}
    real_create_engine = create_engine

    def tracking_create_engine(url, **kwargs):
        captured["url"] = url
        captured["kwargs"] = kwargs
        return real_create_engine(url, **kwargs)

    # `_race_deterministic` resolves `create_engine` from this module's
    # namespace (imported at module top), so patch it there.
    monkeypatch.setattr(sys.modules[__name__], "create_engine", tracking_create_engine)

    engine = real_create_engine(_pg_url() or "")
    try:
        lock_id = uuid.uuid4().int % (2**31)

        def winner_fn(holding, release):
            with engine.begin() as connection:
                connection.execute(text(f"SELECT pg_advisory_lock({lock_id})"))
                holding.set()
                release.wait(timeout=30)
                connection.execute(text(f"SELECT pg_advisory_unlock({lock_id})"))

        def loser_fn(holding, loser_ready, loser_engine):
            holding.wait(timeout=30)
            loser_ready.set()
            with loser_engine.begin() as connection:
                connection.execute(text(f"SELECT pg_advisory_lock({lock_id})"))

        _race_deterministic(
            winner_fn=winner_fn,
            loser_fn=loser_fn,
            engine=engine,
            block_check_seconds=2.0,
        )
    finally:
        engine.dispose()

    from sqlalchemy.engine import URL

    assert isinstance(captured["url"], URL), "loser engine must be built from the URL object"
    # Object identity: the loser engine must receive the caller's own URL
    # object — never a re-rendered DSN string (which would serialize the
    # credential into plaintext). No credential is inspected or asserted.
    assert captured["url"] is engine.url, "loser engine must receive the caller's URL object"
    assert captured["kwargs"]["connect_args"]["application_name"].startswith("race_loser_")


@pytest.mark.skipif(not _pg_url(), reason="PostgreSQL integration environment is not configured")
def test_race_deterministic_cleans_up_threads_and_loser_engine_on_failure(
    monkeypatch,
) -> None:
    """When the lock-observation assertion fails, `_race_deterministic` must
    still release the blocked winner, join both started threads and dispose
    the loser engine — no transaction or connection may outlive the failure
    and block the schema drop."""
    import sys

    captured: dict = {}
    real_create_engine = create_engine

    def tracking_create_engine(url, **kwargs):
        eng = real_create_engine(url, **kwargs)
        if "application_name" in kwargs.get("connect_args", {}):
            captured["loser_engine"] = eng
        return eng

    # `_race_deterministic` resolves `create_engine` from this module's
    # namespace (imported at module top), so patch it there.
    monkeypatch.setattr(sys.modules[__name__], "create_engine", tracking_create_engine)

    engine = real_create_engine(_pg_url() or "")
    try:
        lock_id = uuid.uuid4().int % (2**31)
        winner_done = threading.Event()
        loser_done = threading.Event()
        released = threading.Event()

        def winner_fn(holding, release):
            with engine.begin() as connection:
                connection.execute(text("SELECT pg_advisory_lock(:lid)"), {"lid": lock_id})
                holding.set()
                release.wait(timeout=30)
                released.set()
                connection.execute(text("SELECT pg_advisory_unlock(:lid)"), {"lid": lock_id})
            winner_done.set()

        def loser_fn(holding, loser_ready, loser_engine):
            import time

            holding.wait(timeout=30)
            loser_ready.set()
            # Hold a DIFFERENT advisory lock and sleep: this session never
            # reports wait_event_type='Lock' for the winner's lock, so the
            # observation poll fails fast (block_check_seconds=0.3).
            with loser_engine.begin() as connection:
                connection.execute(text("SELECT pg_advisory_lock(:lid)"), {"lid": lock_id + 1})
                time.sleep(1.5)
            loser_done.set()

        with pytest.raises(AssertionError):
            _race_deterministic(
                winner_fn=winner_fn,
                loser_fn=loser_fn,
                engine=engine,
                block_check_seconds=0.3,
            )
    finally:
        engine.dispose()

    # The failed observation must not leave the winner/loser threads blocked
    # or the loser engine with live connections.
    assert released.is_set(), "winner must have been released after the failure"
    assert winner_done.is_set(), "winner thread must have been joined"
    assert loser_done.is_set(), "loser thread must have been joined"
    assert captured["loser_engine"].pool.checkedin() == 0
    assert captured["loser_engine"].pool.checkedout() == 0
