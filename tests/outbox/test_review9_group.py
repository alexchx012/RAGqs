"""Final review round 9: JSON-safe trigger SQL, capability master-secret
containment, strict attempt terminal summaries and suppression-receipt
materialized-time verification."""

from __future__ import annotations

import importlib.util
from datetime import UTC, datetime
from pathlib import Path
from types import FrameType, ModuleType

import pytest
from _helpers import (
    CAPABILITY_SECRET,
    build_engine,
    build_identity_service,
    cap,
    fixed_now,
    make_publisher,
    provision_user,
    retention_token,
)
from sqlalchemy import select, update

from app.identity.schema import identity_user_table
from app.outbox.dispatcher import OutboxDispatcher
from app.outbox.lifecycle import SqlAlchemyOutboxLifecycle
from app.outbox.metrics import SqlAlchemyOutboxMetrics
from app.outbox.notifications import NotificationMaterializer
from app.outbox.publisher import SqlAlchemyOutboxPublisher
from app.outbox.schema import (
    notification_delivery_receipt_table,
    notification_inbox_table,
    notification_suppression_table,
    outbox_account_retirement_tombstone_table,
)
from app.platform.errors import PlatformError


class _AcceptingArchiveVerifier:
    def verify_archive(self, *, archive_ref: str, checksum: str, **kwargs) -> bool:
        del archive_ref, checksum
        return True


def make_lifecycle(engine):
    return SqlAlchemyOutboxLifecycle(
        engine,
        now=lambda: fixed_now(),
        archive_verifier=_AcceptingArchiveVerifier(),
        capability_secret=CAPABILITY_SECRET,
    )


def publish(engine, *, user_ids, event_id="evt_1"):
    from app.outbox.ports import OutboxPublishCommand, RecipientSelection

    publisher = make_publisher(engine, now=lambda: fixed_now())
    command = OutboxPublishCommand(
        capability=cap("ingestion"),
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
        trace_id="trace_x",
        recipients=tuple(RecipientSelection(recipient_user_id=u) for u in user_ids),
    )
    with engine.begin() as connection:
        publisher.publish(command, connection=connection)


def make_dispatcher(engine):
    return OutboxDispatcher(
        engine,
        consumers={"in_app_notification": NotificationMaterializer(engine)},
        now=lambda: fixed_now(),
        retention_days=30,
        notification_retention_days=90,
        metrics=SqlAlchemyOutboxMetrics(),
    )


def deliver(engine, *, user_ids, event_id="evt_1"):
    del user_ids, event_id
    dispatcher = make_dispatcher(engine)
    claim = dispatcher.claim_one(owner="worker-1")
    assert claim is not None
    dispatcher.run_consumer_and_finalize(claim, owner="worker-1")


def retire_command(
    *,
    operation_id="op_ret_r9",
    user_id="user_x",
    deletion_id="del_ret_1",
):
    from app.outbox.ports import AccountNotificationRetirementCommand

    return AccountNotificationRetirementCommand(
        operation_id=operation_id,
        caller_principal="retention-ops",
        user_id=user_id,
        deletion_id=deletion_id,
        verified_archive_ref="archive_ref_1",
        archive_checksum="checksum_1",
        transaction_id="tx_ret_1",
        mode="inline",
        canonical_input_fingerprint="unused",
        capability_token=retention_token(),
    )


def mark_deletable(engine, user_id: str) -> None:
    with engine.begin() as connection:
        connection.execute(
            update(identity_user_table)
            .where(identity_user_table.c.id == user_id)
            .values(lifecycle_status="pending_delete")
        )


# ---------------------------------------------------------------------------
# 2. Capability master secret / issuer containment
# ---------------------------------------------------------------------------


def test_runtime_does_not_expose_the_master_capability_secret_or_issuer() -> None:
    from _helpers import make_settings

    from app.platform.runtime import build_runtime

    engine = build_engine()
    runtime = build_runtime(make_settings(), adapters={"database_engine": engine})
    # The master secret, issuer, retention token and the RAW lifecycle are
    # assembly-time internals: none of them may be resolvable as generic
    # runtime adapters, and the retention token must not exist in the adapter
    # map AT ALL (not even under a private key).
    assert runtime.resolve("capability_secret", None) is None
    assert runtime.resolve("capability_issuer", None) is None
    assert runtime.resolve("producer_capabilities", None) is None
    assert runtime.resolve("retention_capability_token", None) is None
    assert runtime.resolve("_retention_capability_token", None) is None
    assert runtime.resolve("outbox_lifecycle", None) is None
    assert "_retention_capability_token" not in runtime.adapters
    assert "retention_capability_token" not in runtime.adapters
    # The scoped façades are the only exposed outbox adapters — both
    # resolvable and functional.
    assert runtime.resolve("account_retirement_gateway") is not None
    assert runtime.resolve("retirement_worker") is not None
    assert runtime.resolve("compaction_worker") is not None
    runtime.close()


def test_publisher_constructed_without_a_secret_fails_closed() -> None:
    """capability_secret=None must mean NO signing authority: a token signed
    with any secret is rejected (no implicit development secret)."""
    engine = build_engine()
    identity = build_identity_service(engine)
    alice = provision_user(identity, username="alice")
    publisher = SqlAlchemyOutboxPublisher(engine, now=lambda: fixed_now())

    from app.outbox.ports import OutboxPublishCommand, RecipientSelection

    command = OutboxPublishCommand(
        capability=cap("ingestion"),
        event_id="evt_nosecret",
        caller_principal="ingestion",
        event_type="ingestion_completed",
        schema_version=1,
        aggregate_type="ingestion_job",
        aggregate_id="job_nosecret",
        transition_version=1,
        occurred_at=fixed_now(),
        payload={
            "job_id": "job_nosecret",
            "document_id": "doc_nosecret",
            "document_version_id": "docv_nosecret",
            "publication_id": "pub_nosecret",
        },
        trace_id="t",
        recipients=(RecipientSelection(recipient_user_id=alice),),
    )
    with pytest.raises(PlatformError) as raised:
        with engine.begin() as connection:
            publisher.publish(command, connection=connection)
    assert raised.value.code == "producer_not_authorized"
    assert raised.value.status_code == 403


def test_lifecycle_constructed_without_a_secret_fails_closed() -> None:
    engine = build_engine()
    identity = build_identity_service(engine)
    alice = provision_user(identity, username="alice")
    publish(engine, user_ids=(alice,), event_id="evt_1")
    deliver(engine, user_ids=(alice,), event_id="evt_1")
    lifecycle = SqlAlchemyOutboxLifecycle(
        engine,
        now=lambda: fixed_now(),
        archive_verifier=_AcceptingArchiveVerifier(),
    )
    from app.outbox.capabilities import LifecycleCapabilityIssuer
    from app.outbox.ports import DocumentNotificationRedactionCommand

    command = DocumentNotificationRedactionCommand(
        operation_id="op_1",
        caller_principal="documents",
        deletion_id="del_1",
        document_id="doc_evt_1",
        document_version_ids=("docv_evt_1",),
        reason="document_pending_delete",
        transaction_id="documents-delete:tx_1",
        mode="inline",
        canonical_input_fingerprint="unused",
        capability_token=LifecycleCapabilityIssuer(CAPABILITY_SECRET).issue_documents_redaction(
            deletion_id="del_1", transaction_id="documents-delete:tx_1"
        ),
    )
    with pytest.raises(PlatformError) as raised:
        with engine.begin() as connection:
            lifecycle.redact_document_notifications(command, connection=connection)
    assert raised.value.status_code == 403


def test_runtime_retention_token_authorizes_a_scoped_retirement() -> None:
    """The runtime-assembled retirement gateway authorizes retirement through
    its privately injected retention token (end to end, no master secret
    needed by the caller)."""
    from _helpers import make_settings

    from app.identity.ports import AccountRetirementRequest
    from app.platform.runtime import build_runtime

    engine = build_engine()
    identity = build_identity_service(engine)
    alice = provision_user(identity, username="alice")
    publish(engine, user_ids=(alice,), event_id="evt_1")
    deliver(engine, user_ids=(alice,), event_id="evt_1")
    mark_deletable(engine, alice)
    runtime = build_runtime(
        make_settings(),
        adapters={
            "database_engine": engine,
            "identity_access": identity,
            "archive_verifier": _AcceptingArchiveVerifier(),
        },
    )
    gateway = runtime.resolve("account_retirement_gateway")
    request = AccountRetirementRequest(
        operation_id="op_ret_r9_gateway",
        user_id=alice,
        deletion_id="del_ret_1",
        verified_archive_ref="archive_ref_1",
        archive_checksum="checksum_1",
        transaction_id="tx_ret_1",
        mode="inline",
    )
    with engine.begin() as connection:
        confirmation = gateway.retire(request, connection=connection)
    assert confirmation.state == "completed"
    assert confirmation.receipt_count == 1
    runtime.close()


def _statically_reachable_values(obj) -> tuple[list[str], list[bytes]]:
    """Collect string AND bytes values reachable through statically-visible
    attributes (instance attrs, dataclass fields, container elements AND
    callable closures/free variables) of an object graph. Callables are NOT
    skipped: their `__closure__` cells are descended into so any bearer token
    or signing secret captured in a closure is detected — bytes secrets are
    collected too, never skipped. Modules and frames are skipped."""
    seen: set[int] = set()
    strings: list[str] = []
    byte_values: list[bytes] = []
    stack: list[object] = [obj]
    while stack:
        current = stack.pop()
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, str):
            strings.append(current)
            continue
        if isinstance(current, bytes):
            byte_values.append(current)
            continue
        if isinstance(current, (type, int, float, bool)):
            continue
        if isinstance(current, dict):
            stack.extend(current.values())
            continue
        if isinstance(current, (list, tuple, set, frozenset)):
            stack.extend(current)
            continue
        if isinstance(current, ModuleType):
            continue
        if callable(current) and not isinstance(current, type):
            # Descend into the closure's captured cells (free variables).
            closure = getattr(current, "__closure__", None)
            if closure:
                stack.extend(
                    cell.cell_contents for cell in closure if cell.cell_contents is not None
                )
            # Bound method attributes (__self__) may reach the owning object.
            self_obj = getattr(current, "__self__", None)
            if self_obj is not None:
                stack.append(self_obj)
            continue
        if isinstance(current, FrameType):
            continue
        if isinstance(current, object):
            try:
                attrs = vars(current)
            except TypeError:
                continue
            stack.extend(attrs.values())
    return strings, byte_values


def test_no_runtime_adapter_statically_reaches_the_retention_token() -> None:
    """Every object stored in runtime.adapters (including the assembled
    workers and the gateway) must NOT expose the retention token or any
    signing secret — including BYTES secrets — through any statically
    reachable path (attributes, containers and callable closures). The
    runtime production assembly does not generate or store a bearer token or
    a signing secret at all."""
    from _helpers import make_settings

    from app.platform.runtime import build_runtime

    engine = build_engine()
    runtime = build_runtime(make_settings(), adapters={"database_engine": engine})
    # The runtime never generates a token or secret: there is nothing to find
    # even in closure cell contents. Assert the well-known dev token, the dev
    # secret text AND the dev secret BYTES are all absent from the entire
    # reachable adapter graph (bytes are collected, never skipped).
    from app.outbox.capabilities import LifecycleCapabilityIssuer

    dev_token = LifecycleCapabilityIssuer(CAPABILITY_SECRET).issue_retention()
    dev_secret_text = CAPABILITY_SECRET.decode("utf-8")
    strings: list[str] = []
    byte_values: list[bytes] = []
    for _key, adapter in runtime.adapters.items():
        found_strings, found_bytes = _statically_reachable_values(adapter)
        strings.extend(found_strings)
        byte_values.extend(found_bytes)
    assert dev_token not in strings
    assert dev_secret_text not in strings
    assert CAPABILITY_SECRET not in byte_values
    # No adapter key looks like a secret/bearer carrier.
    for key in runtime.adapters:
        assert "capability" not in key
        assert "secret" not in key
        assert "token" not in key
    # The worker has no token attribute at all.
    worker = runtime.resolve("retirement_worker")
    assert hasattr(worker, "retention_capability_token") is False
    runtime.close()


def test_runtime_pops_assembly_only_sensitive_overrides() -> None:
    """build_runtime must consume every assembly-only sensitive/raw override
    with pop so it never lands in PlatformRuntime.adapters. The injected raw
    lifecycle is still used to assemble the scoped workers/gateway, and the
    injected BYTES secret is never resolvable nor statically reachable."""
    from _helpers import make_settings

    from app.outbox.lifecycle import SqlAlchemyOutboxLifecycle
    from app.platform.runtime import build_runtime

    engine = build_engine()
    sentinel_secret = b"sentinel-outbox-capability-secret-0123456789"
    sentinel_token = "sentinel-retention-token"
    raw_lifecycle = SqlAlchemyOutboxLifecycle(
        engine,
        now=lambda: fixed_now(),
        capability_secret=None,
    )
    runtime = build_runtime(
        make_settings(),
        adapters={
            "database_engine": engine,
            "outbox_lifecycle": raw_lifecycle,
            "outbox_capability_secret": sentinel_secret,
            "retention_capability_token": sentinel_token,
            "_retention_capability_token": sentinel_token,
            "capability_secret": sentinel_secret,
            "capability_issuer": object(),
            "producer_capabilities": {"ingestion": frozenset({"ingestion_completed"})},
        },
    )
    # Every assembly-only override is popped: not resolvable, not in adapters.
    for key in (
        "outbox_lifecycle",
        "outbox_capability_secret",
        "retention_capability_token",
        "_retention_capability_token",
        "capability_secret",
        "capability_issuer",
        "producer_capabilities",
    ):
        assert runtime.resolve(key, None) is None
        assert key not in runtime.adapters
    # The popped raw lifecycle still assembled the scoped façades.
    assert runtime.resolve("retirement_worker") is not None
    assert runtime.resolve("compaction_worker") is not None
    assert runtime.resolve("account_retirement_gateway") is not None
    # The sentinel bytes secret and token are NOT statically reachable from
    # the adapter object graph (including closures), proving the bytes
    # override was consumed, not stored.
    strings: list[str] = []
    byte_values: list[bytes] = []
    for _key, adapter in runtime.adapters.items():
        found_strings, found_bytes = _statically_reachable_values(adapter)
        strings.extend(found_strings)
        byte_values.extend(found_bytes)
    assert sentinel_secret not in byte_values
    assert sentinel_token not in strings
    runtime.close()


def test_runtime_rejects_secret_bearing_lifecycle_override() -> None:
    """A raw `outbox_lifecycle` override whose capability authorizer holds a
    signing secret must be REJECTED at assembly time: the runtime only accepts
    capability_secret=None lifecycles as internal no-token scoped assembly
    input, so the signing secret can never become reachable through the
    worker/gateway object graphs. Default runtime and a no-secret override
    still succeed."""
    from _helpers import make_settings

    from app.outbox.lifecycle import SqlAlchemyOutboxLifecycle
    from app.platform.runtime import build_runtime

    engine = build_engine()
    sentinel = b"sentinel-outbox-capability-secret-0123456789"
    secret_lifecycle = SqlAlchemyOutboxLifecycle(
        engine,
        now=lambda: fixed_now(),
        capability_secret=sentinel,
    )
    with pytest.raises(RuntimeError) as raised:
        build_runtime(
            make_settings(),
            adapters={"database_engine": engine, "outbox_lifecycle": secret_lifecycle},
        )
    assert "secret" in str(raised.value).lower()
    # The sentinel is never copied or stored anywhere: nothing was assembled.
    # Default runtime (no override) succeeds.
    runtime_default = build_runtime(make_settings(), adapters={"database_engine": engine})
    assert runtime_default.resolve("retirement_worker") is not None
    assert runtime_default.resolve("compaction_worker") is not None
    runtime_default.close()
    # A no-secret override is the only accepted lifecycle input.
    no_secret_lifecycle = SqlAlchemyOutboxLifecycle(
        engine,
        now=lambda: fixed_now(),
        capability_secret=None,
    )
    runtime_ok = build_runtime(
        make_settings(),
        adapters={"database_engine": engine, "outbox_lifecycle": no_secret_lifecycle},
    )
    assert runtime_ok.resolve("retirement_worker") is not None
    assert runtime_ok.resolve("compaction_worker") is not None
    runtime_ok.close()


def test_assembled_retirement_worker_completes_accepted_durable_command() -> None:
    """The runtime-assembled worker (holding only its narrow processor around
    the lifecycle's internal no-token entry) completes an accepted durable
    retirement command end to end — even though the raw lifecycle and any
    bearer token are NOT resolvable from the runtime."""
    from _helpers import make_settings

    from app.outbox.lifecycle import SqlAlchemyOutboxLifecycle
    from app.platform.runtime import build_runtime

    engine = build_engine()
    identity = build_identity_service(engine)
    alice = provision_user(identity, username="alice")
    publish(engine, user_ids=(alice,), event_id="evt_1")
    deliver(engine, user_ids=(alice,), event_id="evt_1")
    mark_deletable(engine, alice)
    runtime = build_runtime(
        make_settings(),
        adapters={
            "database_engine": engine,
            "identity_access": identity,
            "archive_verifier": _AcceptingArchiveVerifier(),
        },
    )
    # 1. The accepted durable command is created through the EXPLICIT external
    #    token boundary (direct lifecycle construction with the test secret) —
    #    the runtime itself never carries a secret.
    from app.outbox.capabilities import LifecycleCapabilityIssuer
    from app.outbox.ports import AccountNotificationRetirementCommand

    external_lifecycle = SqlAlchemyOutboxLifecycle(
        engine,
        now=lambda: fixed_now(),
        archive_verifier=_AcceptingArchiveVerifier(),
        capability_secret=CAPABILITY_SECRET,
    )
    durable = AccountNotificationRetirementCommand(
        operation_id="op_ret_durable_r9",
        caller_principal="retention-ops",
        user_id=alice,
        deletion_id="del_ret_1",
        verified_archive_ref="archive_ref_1",
        archive_checksum="checksum_1",
        transaction_id="tx_ret_1",
        mode="durable",
        canonical_input_fingerprint="unused",
        capability_token=LifecycleCapabilityIssuer(CAPABILITY_SECRET).issue_retention(),
    )
    with engine.begin() as connection:
        accepted = external_lifecycle.retire_account_notification_state(
            durable, connection=connection
        )
    assert accepted.state == "accepted"
    # 2. The assembled worker completes the accepted command through its
    #    internal no-token entry.
    worker = runtime.resolve("retirement_worker")
    stats = worker.run_once(owner="worker-r9")
    assert stats.completed == 1
    assert stats.deferred == 0
    from app.outbox.schema import outbox_retirement_command_table

    with engine.connect() as connection:
        state = connection.execute(
            select(outbox_retirement_command_table.c.state).where(
                outbox_retirement_command_table.c.operation_id == "op_ret_durable_r9"
            )
        ).scalar_one()
        assert state == "completed"
    runtime.close()


# ---------------------------------------------------------------------------
# 4. Suppression receipt materialized-time verification
# ---------------------------------------------------------------------------


def _suppress(engine, *, user_id: str, event_id: str) -> None:
    """Make the recipient inactive so materialization writes a suppression."""
    with engine.begin() as connection:
        connection.execute(
            update(identity_user_table)
            .where(identity_user_table.c.id == user_id)
            .values(lifecycle_status="pending_delete")
        )
    dispatcher = make_dispatcher(engine)
    claim = dispatcher.claim_one(owner="worker-1")
    assert claim is not None
    dispatcher.run_consumer_and_finalize(claim, owner="worker-1")
    with engine.connect() as connection:
        assert (
            connection.execute(
                select(notification_suppression_table).where(
                    notification_suppression_table.c.event_id == event_id
                )
            ).all()
            != []
        )


def test_retirement_tampered_suppression_receipt_with_materialized_time_rolls_back() -> None:
    """A suppression receipt whose materialized_at_utc is non-NULL is a
    contradiction: retirement must 409 and delete NOTHING."""
    engine = build_engine()
    identity = build_identity_service(engine)
    alice = provision_user(identity, username="alice")
    publish(engine, user_ids=(alice,), event_id="evt_1")
    _suppress(engine, user_id=alice, event_id="evt_1")
    with engine.begin() as connection:
        connection.execute(
            notification_delivery_receipt_table.insert().values(
                event_id="evt_1",
                recipient_user_id=alice,
                outcome="recipient_inactive",
                original_notification_seq=None,
                occurred_at_utc=fixed_now(),
                materialized_at_utc=fixed_now(),  # contradictory for a suppression
                retired_at_utc=fixed_now(),
                fingerprint="tampered",
            )
        )
    lifecycle = make_lifecycle(engine)
    with pytest.raises(PlatformError) as raised:
        with engine.begin() as connection:
            mark_deletable(engine, alice)
            lifecycle.retire_account_notification_state(
                retire_command(user_id=alice), connection=connection
            )
    assert raised.value.code == "receipt_fingerprint_mismatch"
    assert raised.value.status_code == 409
    with engine.connect() as connection:
        # Nothing was deleted: suppression stays, no tombstone is written and
        # no inbox was ever created for the suppressed recipient.
        assert (
            connection.execute(
                select(notification_suppression_table).where(
                    notification_suppression_table.c.event_id == "evt_1"
                )
            ).all()
            != []
        )
        assert (
            connection.execute(
                select(outbox_account_retirement_tombstone_table).where(
                    outbox_account_retirement_tombstone_table.c.recipient_user_id == alice
                )
            ).all()
            == []
        )
        assert (
            connection.execute(
                select(notification_inbox_table).where(
                    notification_inbox_table.c.recipient_user_id == alice
                )
            ).all()
            == []
        )


# ---------------------------------------------------------------------------
# 3. Compaction suppression-receipt materialized-time verification
# ---------------------------------------------------------------------------


def test_compaction_tampered_suppression_receipt_with_materialized_time_409() -> None:
    """Compaction must verify an existing suppression receipt's
    materialized_at_utc IS NULL. A receipt with the CORRECT canonical
    fingerprint but a tampered (non-NULL) materialized time is a 409 and the
    suppression row / event state must not be deleted."""
    from app.outbox.compaction import canonical_receipt_fingerprint
    from app.outbox.schema import outbox_event_table

    engine = build_engine()
    identity = build_identity_service(engine)
    alice = provision_user(identity, username="alice")
    publish(engine, user_ids=(alice,), event_id="evt_1")
    _suppress(engine, user_id=alice, event_id="evt_1")
    with engine.begin() as connection:
        connection.execute(
            update(outbox_event_table)
            .where(outbox_event_table.c.event_id == "evt_1")
            .values(compact_after_at_utc=datetime(2000, 1, 1, tzinfo=UTC))
        )
    # Insert a suppression receipt with the CORRECT canonical fingerprint but
    # a contradictory non-NULL materialized time.
    correct = canonical_receipt_fingerprint("evt_1", alice, "recipient_inactive", None)
    with engine.begin() as connection:
        connection.execute(
            notification_delivery_receipt_table.insert().values(
                event_id="evt_1",
                recipient_user_id=alice,
                outcome="recipient_inactive",
                original_notification_seq=None,
                occurred_at_utc=fixed_now(),
                materialized_at_utc=fixed_now(),  # contradiction for suppression
                retired_at_utc=fixed_now(),
                fingerprint=correct,
            )
        )
    dispatcher = make_dispatcher(engine)
    with pytest.raises(PlatformError) as raised:
        dispatcher.compact_due_events(now=datetime(2026, 8, 5, tzinfo=UTC))
    assert raised.value.code == "receipt_fingerprint_mismatch"
    assert raised.value.status_code == 409
    with engine.connect() as connection:
        # Nothing was deleted: suppression stays and the event is still full.
        assert (
            connection.execute(
                select(notification_suppression_table).where(
                    notification_suppression_table.c.event_id == "evt_1"
                )
            ).all()
            != []
        )
        assert (
            connection.execute(
                select(outbox_event_table.c.storage_state).where(
                    outbox_event_table.c.event_id == "evt_1"
                )
            ).scalar_one()
            == "full"
        )


def test_compaction_suppression_receipt_clean_with_materialized_null_succeeds() -> None:
    """A suppression receipt with the correct fingerprint AND materialized_at
    NULL is accepted: compaction proceeds and the suppression is replaced by
    the permanent receipt."""
    from app.outbox.compaction import canonical_receipt_fingerprint
    from app.outbox.schema import outbox_event_table

    engine = build_engine()
    identity = build_identity_service(engine)
    alice = provision_user(identity, username="alice")
    publish(engine, user_ids=(alice,), event_id="evt_1")
    _suppress(engine, user_id=alice, event_id="evt_1")
    with engine.begin() as connection:
        connection.execute(
            update(outbox_event_table)
            .where(outbox_event_table.c.event_id == "evt_1")
            .values(compact_after_at_utc=datetime(2000, 1, 1, tzinfo=UTC))
        )
    dispatcher = make_dispatcher(engine)
    assert dispatcher.compact_due_events(now=datetime(2026, 8, 5, tzinfo=UTC)) == 1
    with engine.connect() as connection:
        receipt = (
            connection.execute(
                select(
                    notification_delivery_receipt_table.c.fingerprint,
                    notification_delivery_receipt_table.c.materialized_at_utc,
                ).where(
                    notification_delivery_receipt_table.c.event_id == "evt_1",
                    notification_delivery_receipt_table.c.recipient_user_id == alice,
                )
            )
            .mappings()
            .one()
        )
        assert receipt["materialized_at_utc"] is None
        assert receipt["fingerprint"] == canonical_receipt_fingerprint(
            "evt_1", alice, "recipient_inactive", None
        )
        assert (
            connection.execute(
                select(outbox_event_table.c.storage_state).where(
                    outbox_event_table.c.event_id == "evt_1"
                )
            ).scalar_one()
            == "compacted"
        )


# ---------------------------------------------------------------------------
# 1+3. Trigger builder SQL content (runs without PostgreSQL)
# ---------------------------------------------------------------------------


def _load_0005_helpers():
    path = Path("alembic/versions/0005_outbox_immutable_triggers.py")
    spec = importlib.util.spec_from_file_location("m0005_helpers_r9", str(path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_trigger_builder_uses_json_safe_payload_comparison() -> None:
    helpers = _load_0005_helpers()
    body = helpers._event_guard_body(helpers._IMMUTABLE_EVENT_COLUMNS)
    # JSON has no = operator: payload_json must be compared via ::text, never
    # IS DISTINCT FROM on the raw JSON value.
    assert "NEW.payload_json::text IS DISTINCT FROM OLD.payload_json::text" in body
    assert "NEW.payload_json IS DISTINCT FROM OLD.payload_json" not in body
    # The compacted branch rejects ANY update without a whole-row comparison
    # (whole-row comparison on a JSON column fails with json = json).
    assert "NEW IS NOT DISTINCT FROM OLD" not in body


def test_trigger_builder_attempt_terminal_summaries_are_strict() -> None:
    helpers = _load_0005_helpers()
    body = helpers._attempt_guard_body(helpers._ATTEMPT_IMMUTABLE_COLUMNS)
    # delivered must carry NO error summary at all (both fields NULL).
    assert (
        "NEW.status = 'delivered' AND (NEW.error_category IS NOT NULL OR NEW.error_code IS NOT NULL)"
        in body
    )
    # failed/expired must carry a complete legal summary (both fields set).
    assert (
        "NEW.status IN ('failed', 'expired') AND (NEW.error_category IS NULL OR NEW.error_code IS NULL)"
        in body
    )
