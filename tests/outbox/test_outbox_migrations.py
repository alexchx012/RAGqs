"""Migration contract for outbox-owned tables."""

from __future__ import annotations

from pathlib import Path

import pytest
from _helpers import alembic_config
from sqlalchemy import create_engine, inspect, select, text

from alembic import command
from app.outbox.schema import OUTBOX_TABLE_NAMES

# Migration/trigger contract tests: the PostgreSQL legs skip themselves when
# RAGQS_TEST_POSTGRES_URL is unset; the marker keeps `-m "not integration"`
# runs from paying for them at all.
pytestmark = pytest.mark.integration


def test_alembic_config_round_trips_percent_encoded_scoped_postgres_url() -> None:
    """Config.set_main_option must accept a percent-encoded, schema-scoped
    PostgreSQL URL, and the option must read back as the original
    single-percent URL.

    The `options=-c%20search_path%3D...` query percent-encodes characters;
    ConfigParser treats a bare `%` as an interpolation marker and previously
    raised `ValueError: invalid interpolation syntax` when the option was
    read (the exact failure the real-PostgreSQL outbox tests hit).
    """
    url = (
        "postgresql+psycopg://ragqs:ragqs@localhost:5432/ragqs"
        "?options=-c%20search_path%3Doutbox_it_regression"
    )
    config = alembic_config(url)
    assert config.get_main_option("sqlalchemy.url") == url


def test_head_upgrade_creates_outbox_owned_tables(tmp_path: Path) -> None:
    database_url = f"sqlite:///{tmp_path / 'outbox.sqlite3'}"
    config = alembic_config(database_url)

    command.upgrade(config, "head")
    engine = create_engine(database_url)
    tables = set(inspect(engine).get_table_names())
    engine.dispose()

    assert OUTBOX_TABLE_NAMES <= tables


def test_outbox_revision_can_downgrade_to_identity_base(tmp_path: Path) -> None:
    database_url = f"sqlite:///{tmp_path / 'outbox-downgrade.sqlite3'}"
    config = alembic_config(database_url)

    command.upgrade(config, "head")
    command.downgrade(config, "0002_identity_access")

    engine = create_engine(database_url)
    tables = set(inspect(engine).get_table_names())
    engine.dispose()

    assert not (OUTBOX_TABLE_NAMES & tables)


def test_sqlite_alembic_head_has_the_compacted_fields_check(tmp_path: Path) -> None:
    """The Alembic-migrated SQLite schema (not just metadata create_all) must
    carry `ck_outbox_event_compacted_fields_full_null` and reject illegal full
    events that carry compacted facts."""
    from sqlalchemy.exc import IntegrityError

    database_url = f"sqlite:///{tmp_path / 'outbox-check.sqlite3'}"
    config = alembic_config(database_url)

    command.upgrade(config, "head")
    engine = create_engine(database_url)
    try:
        checks = inspect(engine).get_check_constraints("outbox_event")
        names = {c["name"] for c in checks}
        assert "ck_outbox_event_compacted_fields_full_null" in names

        # A legal full event (compacted facts NULL) inserts fine.
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO outbox_event "
                    "(event_id, event_type, schema_version, aggregate_type, aggregate_id, "
                    "transition_version, occurred_at_utc, payload_json, payload_fingerprint, "
                    "trace_id, created_at_utc, storage_state, compact_after_at_utc, "
                    "compacted_at_utc, compacted_delivery_summary_json) "
                    "VALUES (:event_id, 'ingestion_completed', 1, 'ingestion_job', 'job_a', 1, "
                    "'2026-01-01 00:00:00', '{}', 'fp-a', 't', '2026-01-01 00:00:00', 'full', "
                    "NULL, NULL, NULL)"
                ),
                {"event_id": "evt_legal"},
            )
        # A full event carrying compacted_at_utc violates the CHECK.
        with pytest.raises(IntegrityError) as raised:
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "INSERT INTO outbox_event "
                        "(event_id, event_type, schema_version, aggregate_type, aggregate_id, "
                        "transition_version, occurred_at_utc, payload_json, payload_fingerprint, "
                        "trace_id, created_at_utc, storage_state, compact_after_at_utc, "
                        "compacted_at_utc, compacted_delivery_summary_json) "
                        "VALUES (:event_id, 'ingestion_completed', 1, 'ingestion_job', 'job_b', 1, "
                        "'2026-01-01 00:00:00', '{}', 'fp-b', 't', '2026-01-01 00:00:00', 'full', "
                        "NULL, '2026-02-01 00:00:00', NULL)"
                    ),
                    {"event_id": "evt_illegal"},
                )
        # The failure is the CHECK constraint, not something else.
        assert "ck_outbox_event_compacted_fields_full_null" in str(raised.value.orig)
        with engine.connect() as connection:
            assert (
                connection.execute(
                    text("SELECT COUNT(*) FROM outbox_event WHERE event_id = 'evt_illegal'")
                ).scalar_one()
                == 0
            )
    finally:
        engine.dispose()


def _pg_url() -> str | None:
    import os

    return os.environ.get("RAGQS_TEST_POSTGRES_URL")


def _scoped_url(url: str, schema: str) -> str:
    from urllib.parse import quote

    sep = "&" if "?" in url else "?"
    return f"{url}{sep}options=-c%20search_path%3D{quote(schema)}"


def _pg_trigger_installed(engine) -> bool:
    """True when the immutable-event trigger exists for outbox_event in the
    CURRENT schema.

    pg_trigger is a database-wide catalog; earlier test schemas can carry the
    same trigger name, so the lookup must be scoped to the table resolved
    through the connection's search_path, never bare tgname.
    """
    with engine.connect() as connection:
        row = connection.execute(
            text(
                "SELECT 1 FROM pg_trigger "
                "WHERE tgname = 'trg_outbox_event_immutable' "
                "AND tgrelid = 'outbox_event'::regclass"
            )
        ).scalar_one_or_none()
        return row is not None


def test_postgres_final_immutable_triggers_remain_installed_to_head_when_configured() -> None:
    """A fresh schema upgraded through 0006 -> head retains the final guards
    installed by the immutable-trigger migration."""
    import uuid

    if not _pg_url():
        pytest.skip("PostgreSQL integration environment is not configured")
    from sqlalchemy import create_engine

    from alembic import command

    database_url = _pg_url()
    schema = f"mig_refresh_{uuid.uuid4().hex[:12]}"
    admin = create_engine(database_url)
    try:
        with admin.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    finally:
        admin.dispose()
    scoped = _scoped_url(database_url, schema)
    try:
        config = alembic_config(scoped)
        command.upgrade(config, "0006_outbox_retirement_tombstone")
        engine = create_engine(scoped)
        # 0005 already installed the final trigger functions.
        assert _pg_trigger_installed(engine)
        engine.dispose()

        command.upgrade(config, "head")
        engine = create_engine(scoped)
        assert _pg_trigger_installed(engine)
        _assert_full_event_identity_guarded(engine)
        engine.dispose()
    finally:
        admin = create_engine(database_url)
        try:
            with admin.begin() as connection:
                connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        finally:
            admin.dispose()


def _assert_full_event_identity_guarded(engine) -> None:
    """Publish one event and verify the installed guards reject a trace_id change."""
    import uuid

    from _helpers import build_identity_service, fixed_now, make_publisher, provision_user
    from sqlalchemy.exc import ProgrammingError

    from app.outbox.ports import OutboxPublishCommand, RecipientSelection

    identity = build_identity_service(engine)
    user_id = provision_user(identity, username=f"trigger_user_{uuid.uuid4().hex[:8]}")
    event_id = f"evt_refresh_{uuid.uuid4().hex[:8]}"
    publisher = make_publisher(engine)
    with engine.begin() as connection:
        publisher.publish(
            OutboxPublishCommand(
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
                recipients=(RecipientSelection(recipient_user_id=user_id),),
            ),
            connection=connection,
        )
    with pytest.raises(ProgrammingError):
        with engine.begin() as connection:
            connection.execute(
                text("UPDATE outbox_event SET trace_id = 'other' WHERE event_id = :eid"),
                {"eid": event_id},
            )
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE outbox_event SET occurred_at_utc = occurred_at_utc " "WHERE event_id = :eid"
            ),
            {"eid": event_id},
        )


def test_postgres_backfills_inboxes_and_compact_due_index_serves_the_scan() -> None:
    """0040（A1/A7/A11）：存量用户逐一补建 inbox（退休账号除外、已有 inbox 不
    覆盖）；compact 候选查询在 PostgreSQL 下走 partial index；metric prune 走
    observed 索引。"""
    import uuid

    if not _pg_url():
        pytest.skip("PostgreSQL integration environment is not configured")
    from _helpers import build_identity_service
    from sqlalchemy import create_engine

    from app.identity.schema import identity_user_table

    database_url = _pg_url()
    schema = f"mig_backfill_{uuid.uuid4().hex[:12]}"
    admin = create_engine(database_url)
    try:
        with admin.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    finally:
        admin.dispose()
    scoped = _scoped_url(database_url, schema)
    engine = create_engine(scoped)
    try:
        config = alembic_config(scoped)
        command.upgrade(config, "0039_chat_release_merge")

        identity = build_identity_service(engine)
        for name in ("plain-user", "inbox-user", "retired-user"):
            identity.provision_user(
                username=name,
                password="Password1",
                real_name=name.title(),
                display_name=name.title(),
                role="user",
                department_id=None,
            )
        with engine.begin() as connection:
            fetched = dict(
                connection.execute(
                    select(identity_user_table.c.username, identity_user_table.c.id)
                ).all()
            )
        plain = fetched["plain-user"]
        inbox_keeper = fetched["inbox-user"]
        retired = fetched["retired-user"]

        # 模拟存量：清空 inbox；退休账号留墓碑；一个用户保留非默认 inbox 值。
        with engine.begin() as connection:
            connection.execute(text("DELETE FROM notification_inbox"))
            connection.execute(
                text(
                    "INSERT INTO outbox_account_retirement_tombstone "
                    "(recipient_user_id, next_notification_seq, read_through_seq, retired_at_utc) "
                    "VALUES (:uid, 1, 0, now())"
                ),
                {"uid": retired},
            )
            connection.execute(
                text(
                    "INSERT INTO notification_inbox "
                    "(recipient_user_id, next_notification_seq, read_through_seq, "
                    "read_all_at_utc, version, retired) VALUES (:uid, 5, 3, NULL, 7, FALSE)"
                ),
                {"uid": inbox_keeper},
            )

        command.upgrade(config, "head")

        with engine.connect() as connection:
            rows = (
                connection.execute(
                    text(
                        "SELECT recipient_user_id, next_notification_seq, read_through_seq, "
                        "version FROM notification_inbox"
                    )
                )
                .mappings()
                .all()
            )
            by_user = {row["recipient_user_id"]: row for row in rows}
            assert set(by_user) == {plain, inbox_keeper}
            assert by_user[plain]["next_notification_seq"] == 1
            assert by_user[plain]["read_through_seq"] == 0
            assert by_user[plain]["version"] == 1
            assert by_user[inbox_keeper]["next_notification_seq"] == 5
            assert by_user[inbox_keeper]["read_through_seq"] == 3
            assert by_user[inbox_keeper]["version"] == 7

            # compact 候选扫描必须走 partial index：墓碑行不进索引，少量到期
            # 行可被索引命中（大表使规划器自然选择索引路径）。
            connection.execute(
                text(
                    "INSERT INTO outbox_event (event_id, event_type, schema_version, "
                    "aggregate_type, aggregate_id, transition_version, occurred_at_utc, "
                    "payload_json, payload_fingerprint, trace_id, created_at_utc, storage_state, "
                    "compact_after_at_utc, compacted_at_utc, compacted_delivery_summary_json) "
                    "SELECT 'evtc_' || g, 'ingestion_completed', NULL, 'ingestion_job', "
                    "'job_' || g, g, now(), NULL, 'fp_' || g, NULL, now(), 'compacted', "
                    "NULL, now(), '[]' FROM generate_series(1, 20000) g"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO outbox_event (event_id, event_type, schema_version, "
                    "aggregate_type, aggregate_id, transition_version, occurred_at_utc, "
                    "payload_json, payload_fingerprint, trace_id, created_at_utc, storage_state, "
                    "compact_after_at_utc, compacted_at_utc, compacted_delivery_summary_json) "
                    "VALUES ('evt_due_1', 'ingestion_completed', 1, 'ingestion_job', 'job_due', 1, "
                    "now(), '{}', 'fp_due', 'trace', now(), 'full', now() - interval '1 hour', "
                    "NULL, NULL)"
                )
            )
            connection.execute(text("ANALYZE outbox_event"))
            plan = "\n".join(
                connection.execute(
                    text(
                        "EXPLAIN (COSTS OFF) SELECT event_id FROM outbox_event "
                        "WHERE storage_state = 'full' AND compact_after_at_utc IS NOT NULL "
                        "AND compact_after_at_utc <= now()"
                    )
                ).scalars()
            )
            assert "ix_outbox_event_compact_due" in plan

            connection.execute(
                text(
                    "INSERT INTO outbox_metric (metric_name, observed_at_utc, value, event_id) "
                    "SELECT 'outbox.deliveries.delivered', now(), 1.0, NULL "
                    "FROM generate_series(1, 20000)"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO outbox_metric (metric_name, observed_at_utc, value, event_id) "
                    "VALUES ('outbox.deliveries.delivered', now() - interval '31 days', 1.0, NULL)"
                )
            )
            connection.execute(text("ANALYZE outbox_metric"))
            metric_plan = "\n".join(
                connection.execute(
                    text(
                        "EXPLAIN (COSTS OFF) SELECT id FROM outbox_metric "
                        "WHERE observed_at_utc < now() - interval '30 days'"
                    )
                ).scalars()
            )
            assert "ix_outbox_metric_observed" in metric_plan
    finally:
        engine.dispose()
        admin = create_engine(database_url)
        try:
            with admin.begin() as connection:
                connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        finally:
            admin.dispose()


def test_head_keeps_single_recipient_seq_index_and_downgrade_restores_it(
    tmp_path: Path,
) -> None:
    """0047 去重：升级后 notification 只剩唯一约束自带的索引，downgrade 可恢复。"""

    database_url = f"sqlite:///{tmp_path / 'notification-index.sqlite3'}"
    config = alembic_config(database_url)

    command.upgrade(config, "head")
    engine = create_engine(database_url)
    index_names = {index["name"] for index in inspect(engine).get_indexes("notification")}
    unique_constraints = {
        constraint["name"]: tuple(constraint["column_names"])
        for constraint in inspect(engine).get_unique_constraints("notification")
    }
    engine.dispose()

    assert "ix_notification_recipient_seq" not in index_names
    assert unique_constraints["uq_notification_recipient_seq"] == (
        "recipient_user_id",
        "notification_seq",
    )

    command.downgrade(config, "0046_quota_approver_department")
    engine = create_engine(database_url)
    index_names = {index["name"] for index in inspect(engine).get_indexes("notification")}
    engine.dispose()

    assert "ix_notification_recipient_seq" in index_names


def test_notification_retire_due_index_round_trips(tmp_path: Path) -> None:
    """0048：notification 表由 0003 的冻结定义建成，upgrade 到 head 时由迁移补建
    retire due 索引，downgrade 再对称摘除，往返回到终态一致。"""
    database_url = f"sqlite:///{tmp_path / 'notification-retire-index.sqlite3'}"
    config = alembic_config(database_url)

    command.upgrade(config, "head")
    engine = create_engine(database_url)
    assert "ix_notification_retire_due" in {
        index["name"] for index in inspect(engine).get_indexes("notification")
    }
    engine.dispose()

    command.downgrade(config, "0047_drop_notification_seq_idx")
    engine = create_engine(database_url)
    assert "ix_notification_retire_due" not in {
        index["name"] for index in inspect(engine).get_indexes("notification")
    }
    engine.dispose()

    # 摘除后的库再次升级（存量库形态）由迁移补建。
    command.upgrade(config, "head")
    engine = create_engine(database_url)
    assert "ix_notification_retire_due" in {
        index["name"] for index in inspect(engine).get_indexes("notification")
    }
    engine.dispose()


def test_postgres_retire_due_index_serves_the_scan() -> None:
    """0048（review #14）：与 outbox compact 扫描同型——notification 的保留期
    到期扫描在 PostgreSQL 下必须走索引；0003 用冻结表定义建 notification，
    upgrade 到 head 由 0048 补建该索引。"""
    import uuid

    if not _pg_url():
        pytest.skip("PostgreSQL integration environment is not configured")
    from sqlalchemy import create_engine

    database_url = _pg_url()
    schema = f"mig_retire_{uuid.uuid4().hex[:12]}"
    admin = create_engine(database_url)
    try:
        with admin.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    finally:
        admin.dispose()
    scoped = _scoped_url(database_url, schema)
    engine = create_engine(scoped)
    try:
        config = alembic_config(scoped)
        # 0047 时点 notification 尚无 retire due 索引（新装与存量库同形）。
        command.upgrade(config, "0047_drop_notification_seq_idx")
        with engine.connect() as connection:
            index_names = {
                index["name"] for index in inspect(connection).get_indexes("notification")
            }
        assert "ix_notification_retire_due" not in index_names

        command.upgrade(config, "head")

        with engine.begin() as connection:
            # 20000 行远期保留 + 1 行到期：大表使规划器自然选择索引路径
            # （与 compact 扫描断言同一构造）。
            connection.execute(
                text(
                    "INSERT INTO notification (id, event_id, recipient_user_id, "
                    "notification_type, title, payload_json, event_occurred_at_utc, "
                    "materialized_at_utc, notification_seq, read_at_utc, "
                    "retire_after_at_utc, redacted) "
                    "SELECT 'ntf_r_' || g, 'evt_r_' || g, 'retire_user', "
                    "'ingestion_completed', 't', '{}', now(), now(), g, NULL, "
                    "now() + interval '90 days', FALSE FROM generate_series(1, 20000) g"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO notification (id, event_id, recipient_user_id, "
                    "notification_type, title, payload_json, event_occurred_at_utc, "
                    "materialized_at_utc, notification_seq, read_at_utc, "
                    "retire_after_at_utc, redacted) "
                    "VALUES ('ntf_due_1', 'evt_due_1', 'retire_user', "
                    "'ingestion_completed', 't', '{}', now(), now(), 20001, NULL, "
                    "now() - interval '1 hour', FALSE)"
                )
            )
            connection.execute(text("ANALYZE notification"))
            plan = "\n".join(
                connection.execute(
                    text(
                        "EXPLAIN (COSTS OFF) SELECT id FROM notification "
                        "WHERE retire_after_at_utc <= now()"
                    )
                ).scalars()
            )
            assert "ix_notification_retire_due" in plan
    finally:
        engine.dispose()
        admin = create_engine(database_url)
        try:
            with admin.begin() as connection:
                connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        finally:
            admin.dispose()
