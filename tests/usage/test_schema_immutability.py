"""数据库级不可变性与约束行为测试（review #2/#3/#4/#5）。

所有用例都在 Alembic 升级后的文件库上执行（否则无触发器）。
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

from alembic import command
from alembic.config import Config

NOW = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)
NOW_STR = "2026-08-05T12:00:00+00:00"


def _upgraded_engine(tmp_path: Path, name: str = "immutable.sqlite3"):
    database_url = f"sqlite:///{tmp_path / name}"
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(config, "head")
    return create_engine(database_url)


def _seed(connection) -> None:
    connection.execute(
        text(
            "INSERT INTO business_calendar_version (id, version_id, timezone,"
            " effective_from_utc, created_at_utc) VALUES"
            " ('instance', 'cal_1', 'Asia/Shanghai', :now, :now)"
        ),
        {"now": NOW},
    )
    connection.execute(
        text(
            "INSERT INTO price_catalog (id, provider, model, operation, currency_code,"
            " effective_from_utc, created_at_utc) VALUES"
            " ('p1', 'dashscope', 'qwen-max', 'chat', 'CNY', :now, :now)"
        ),
        {"now": NOW},
    )
    connection.execute(
        text(
            "INSERT INTO price_catalog_line (id, price_version_id, meter, unit, rate,"
            " billing_granularity, minimum_billable_quantity, rounding_rule) VALUES"
            " ('pl1', 'p1', 'input_tokens', 'token', 0.001, 1, 0, 'half_up')"
        ),
    )
    connection.execute(
        text(
            "INSERT INTO usage_event (usage_event_id, event_kind, result, event_fingerprint,"
            " ownership_json, cost_center_key, started_at_utc, completed_at_utc,"
            " effective_calendar_version_id, effective_at_utc, effective_period,"
            " recorded_calendar_version_id, recorded_at_utc, recorded_period, created_at_utc,"
            " execution_kind, execution_id, stage, resource_kind)"
            " VALUES"
            " ('ue_local', 'local_usage', 'succeeded', 'fp_local', '{}', 'user:u1',"
            " :now, :now, 'cal_1', :now, '2026-08', 'cal_1', :now, '2026-08', :now,"
            " 'ocr', 'exec1', 'extract', 'pdf')"
        ),
        {"now": NOW},
    )
    connection.execute(
        text(
            "INSERT INTO usage_event (usage_event_id, event_kind, result, event_fingerprint,"
            " ownership_json, cost_center_key, started_at_utc, completed_at_utc,"
            " effective_calendar_version_id, effective_at_utc, effective_period,"
            " recorded_calendar_version_id, recorded_at_utc, recorded_period, created_at_utc,"
            " provider_call_id, provider, model, operation, price_version_id, currency_code,"
            " estimated_cost_status, execution_kind, execution_id, attempt_id, generation_id)"
            " VALUES"
            " ('ue_prov', 'provider_usage', 'succeeded', 'fp_prov', '{}', 'user:u1',"
            " :now, :now, 'cal_1', :now, '2026-08', 'cal_1', :now, '2026-08', :now,"
            " 'pc1', 'dashscope', 'qwen-max', 'chat', 'p1', 'CNY', 'complete',"
            " 'chat', 'exec1', 'att1', 'gen1')"
        ),
        {"now": NOW},
    )
    connection.execute(
        text(
            "INSERT INTO quota_debit (quota_debit_id, entry_kind, page_delta, entry_fingerprint,"
            " quota_operation_id, publication_id, quota_subject_user_id, quota_period,"
            " cost_center_key, ownership_json, effective_calendar_version_id, effective_at_utc,"
            " effective_period, recorded_calendar_version_id, recorded_at_utc, recorded_period,"
            " created_at_utc) VALUES"
            " ('qd_1', 'debit', 10, 'fp', 'op_1', 'pub_1', 'u1', '2026-08', 'user:u1',"
            " '{}', 'cal_1', :now, '2026-08', 'cal_1', :now, '2026-08', :now)"
        ),
        {"now": NOW},
    )


def _expect_abort(engine, sql: str, message_fragment: str, params: dict | None = None) -> None:
    bind = dict(params or {})
    if ":now" in sql and "now" not in bind:
        bind["now"] = NOW
    with pytest.raises(Exception) as exc:
        with engine.begin() as connection:
            connection.execute(text(sql), bind)
    assert message_fragment in str(exc.value)


def _count(engine, table: str) -> int:
    with engine.connect() as connection:
        return connection.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar_one()


# ---------------------------------------------------------------------------
# 账本不可变（既有 C1）
# ---------------------------------------------------------------------------


def test_update_and_delete_on_usage_event_are_rejected(tmp_path: Path) -> None:
    engine = _upgraded_engine(tmp_path)
    try:
        with engine.begin() as connection:
            _seed(connection)
        _expect_abort(
            engine,
            "UPDATE usage_event SET result = 'failed' WHERE usage_event_id = 'ue_local'",
            "immutable",
        )
        _expect_abort(
            engine,
            "DELETE FROM usage_event WHERE usage_event_id = 'ue_local'",
            "immutable",
        )
        assert _count(engine, "usage_event") == 2
    finally:
        engine.dispose()


def test_update_and_delete_on_quota_debit_are_rejected(tmp_path: Path) -> None:
    engine = _upgraded_engine(tmp_path)
    try:
        with engine.begin() as connection:
            _seed(connection)
        _expect_abort(
            engine,
            "UPDATE quota_debit SET page_delta = 99 WHERE quota_debit_id = 'qd_1'",
            "immutable",
        )
        _expect_abort(
            engine,
            "DELETE FROM quota_debit WHERE quota_debit_id = 'qd_1'",
            "immutable",
        )
        assert _count(engine, "quota_debit") == 1
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------
# business_calendar_version singleton（review #2）
# ---------------------------------------------------------------------------


def test_calendar_singleton_rejects_non_instance_insert(tmp_path: Path) -> None:
    engine = _upgraded_engine(tmp_path, "cal_insert.sqlite3")
    try:
        with engine.begin() as connection:
            _seed(connection)
        _expect_abort(
            engine,
            "INSERT INTO business_calendar_version (id, version_id, timezone,"
            " effective_from_utc, created_at_utc) VALUES"
            " ('other', 'cal_2', 'UTC', '2026-08-01T00:00:00+00:00', '2026-08-01T00:00:00+00:00')",
            "CHECK",
        )
        assert _count(engine, "business_calendar_version") == 1
    finally:
        engine.dispose()


def test_calendar_singleton_rejects_update_and_delete(tmp_path: Path) -> None:
    engine = _upgraded_engine(tmp_path, "cal_mutate.sqlite3")
    try:
        with engine.begin() as connection:
            _seed(connection)
        _expect_abort(
            engine,
            "UPDATE business_calendar_version SET version_id = 'cal_2' WHERE id = 'instance'",
            "immutable",
        )
        _expect_abort(
            engine,
            "DELETE FROM business_calendar_version WHERE id = 'instance'",
            "immutable",
        )
        # 最终恰一行
        assert _count(engine, "business_calendar_version") == 1
        with engine.connect() as connection:
            version_id = connection.execute(
                text("SELECT version_id FROM business_calendar_version WHERE id = 'instance'")
            ).scalar_one()
        assert version_id == "cal_1"
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------
# price_catalog / price_catalog_line（review #3）
# ---------------------------------------------------------------------------


def test_price_catalog_rejects_invalid_interval(tmp_path: Path) -> None:
    engine = _upgraded_engine(tmp_path, "price_interval.sqlite3")
    try:
        with engine.begin() as connection:
            _seed(connection)
        _expect_abort(
            engine,
            "INSERT INTO price_catalog (id, provider, model, operation, currency_code,"
            " effective_from_utc, effective_to_utc, created_at_utc) VALUES"
            " ('p2', 'dashscope', 'qwen-max', 'chat', 'CNY',"
            " '2026-09-01T00:00:00+00:00', '2026-08-01T00:00:00+00:00',"
            " '2026-09-01T00:00:00+00:00')",
            "CHECK",
        )
        assert _count(engine, "price_catalog") == 1
    finally:
        engine.dispose()


def test_price_catalog_line_rejects_update_and_delete(tmp_path: Path) -> None:
    engine = _upgraded_engine(tmp_path, "price_line_mutate.sqlite3")
    try:
        with engine.begin() as connection:
            _seed(connection)
        _expect_abort(
            engine,
            "UPDATE price_catalog_line SET rate = 0.5 WHERE id = 'pl1'",
            "immutable",
        )
        _expect_abort(
            engine,
            "DELETE FROM price_catalog_line WHERE id = 'pl1'",
            "immutable",
        )
        assert _count(engine, "price_catalog_line") == 1
    finally:
        engine.dispose()


def test_price_catalog_rejects_delete(tmp_path: Path) -> None:
    engine = _upgraded_engine(tmp_path, "price_delete.sqlite3")
    try:
        with engine.begin() as connection:
            _seed(connection)
        _expect_abort(
            engine,
            "DELETE FROM price_catalog WHERE id = 'p1'",
            "append-only",
        )
        assert _count(engine, "price_catalog") == 1
    finally:
        engine.dispose()


def test_price_catalog_allows_single_close_update(tmp_path: Path) -> None:
    engine = _upgraded_engine(tmp_path, "price_close.sqlite3")
    try:
        with engine.begin() as connection:
            _seed(connection)
        # 一次 close：effective_to_utc NULL→非 NULL 且其他字段不变 → 允许
        with engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE price_catalog SET effective_to_utc = '2026-09-01T00:00:00+00:00'"
                    " WHERE id = 'p1'"
                )
            )
        with engine.connect() as connection:
            effective_to = connection.execute(
                text("SELECT effective_to_utc FROM price_catalog WHERE id = 'p1'")
            ).scalar_one()
        assert effective_to == "2026-09-01T00:00:00+00:00"
        # 再次 close → 拒绝（一次性）
        _expect_abort(
            engine,
            "UPDATE price_catalog SET effective_to_utc = '2026-10-01T00:00:00+00:00'"
            " WHERE id = 'p1'",
            "append-only",
        )
        # 其他字段修改 → 拒绝（防历史重写）
        _expect_abort(
            engine,
            "UPDATE price_catalog SET currency_code = 'USD' WHERE id = 'p1'",
            "append-only",
        )
    finally:
        engine.dispose()


def test_price_catalog_close_rejects_id_change(tmp_path: Path) -> None:
    engine = _upgraded_engine(tmp_path, "price_close_id.sqlite3")
    try:
        with engine.begin() as connection:
            _seed(connection)
        # close 同时改 id → 拒绝
        _expect_abort(
            engine,
            "UPDATE price_catalog SET id = 'p1_rewritten',"
            " effective_to_utc = '2026-09-01T00:00:00+00:00' WHERE id = 'p1'",
            "append-only",
        )
        with engine.connect() as connection:
            count = connection.execute(
                text("SELECT COUNT(*) FROM price_catalog WHERE id = 'p1'")
            ).scalar_one()
        assert count == 1
    finally:
        engine.dispose()


def test_price_catalog_close_rejects_supersedes_change(tmp_path: Path) -> None:
    engine = _upgraded_engine(tmp_path, "price_close_supersedes.sqlite3")
    try:
        with engine.begin() as connection:
            _seed(connection)
        # close 同时把 supersedes_version_id NULL→非 NULL → 拒绝（null-safe 比较）
        _expect_abort(
            engine,
            "UPDATE price_catalog SET supersedes_version_id = 'p0',"
            " effective_to_utc = '2026-09-01T00:00:00+00:00' WHERE id = 'p1'",
            "append-only",
        )
        # 合法 close（supersedes 保持 NULL）仍允许
        with engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE price_catalog SET effective_to_utc = '2026-09-01T00:00:00+00:00'"
                    " WHERE id = 'p1'"
                )
            )
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------
# usage_event row-shape（review #4）
# ---------------------------------------------------------------------------


def test_usage_event_provider_shape_requires_price_and_execution_fields(tmp_path: Path) -> None:
    engine = _upgraded_engine(tmp_path, "ue_provider_shape.sqlite3")
    try:
        with engine.begin() as connection:
            _seed(connection)
        # provider_usage 缺 price_version_id → 拒绝
        _expect_abort(
            engine,
            "INSERT INTO usage_event (usage_event_id, event_kind, result, event_fingerprint,"
            " ownership_json, cost_center_key, started_at_utc, completed_at_utc,"
            " effective_calendar_version_id, effective_at_utc, effective_period,"
            " recorded_calendar_version_id, recorded_at_utc, recorded_period, created_at_utc,"
            " provider_call_id, provider, model, operation, currency_code,"
            " estimated_cost_status, execution_kind, execution_id) VALUES"
            " ('ue_bad', 'provider_usage', 'succeeded', 'fp', '{}', 'user:u1',"
            " :now, :now, 'cal_1', :now, '2026-08', 'cal_1', :now, '2026-08', :now,"
            " 'pc1', 'dashscope', 'qwen-max', 'chat', 'CNY', 'complete', 'chat', 'exec1')",
            "CHECK",
        )
        # provider_usage 缺 execution_kind → 拒绝
        _expect_abort(
            engine,
            "INSERT INTO usage_event (usage_event_id, event_kind, result, event_fingerprint,"
            " ownership_json, cost_center_key, started_at_utc, completed_at_utc,"
            " effective_calendar_version_id, effective_at_utc, effective_period,"
            " recorded_calendar_version_id, recorded_at_utc, recorded_period, created_at_utc,"
            " provider_call_id, provider, model, operation, price_version_id, currency_code,"
            " estimated_cost_status, execution_id) VALUES"
            " ('ue_bad2', 'provider_usage', 'succeeded', 'fp', '{}', 'user:u1',"
            " :now, :now, 'cal_1', :now, '2026-08', 'cal_1', :now, '2026-08', :now,"
            " 'pc1', 'dashscope', 'qwen-max', 'chat', 'p1', 'CNY', 'complete', 'exec1')",
            "CHECK",
        )
        assert _count(engine, "usage_event") == 2
    finally:
        engine.dispose()


def test_usage_event_local_shape_requires_four_tuple(tmp_path: Path) -> None:
    engine = _upgraded_engine(tmp_path, "ue_local_shape.sqlite3")
    try:
        with engine.begin() as connection:
            _seed(connection)
        # local_usage 缺 stage → 拒绝
        _expect_abort(
            engine,
            "INSERT INTO usage_event (usage_event_id, event_kind, result, event_fingerprint,"
            " ownership_json, cost_center_key, started_at_utc, completed_at_utc,"
            " effective_calendar_version_id, effective_at_utc, effective_period,"
            " recorded_calendar_version_id, recorded_at_utc, recorded_period, created_at_utc,"
            " execution_kind, execution_id, resource_kind) VALUES"
            " ('ue_bad', 'local_usage', 'succeeded', 'fp', '{}', 'user:u1',"
            " :now, :now, 'cal_1', :now, '2026-08', 'cal_1', :now, '2026-08', :now,"
            " 'ocr', 'exec1', 'pdf')",
            "CHECK",
        )
        assert _count(engine, "usage_event") == 2
    finally:
        engine.dispose()


@pytest.mark.parametrize("event_kind", ["usage_adjustment", "cost_adjustment"])
def test_usage_adjustment_requires_execution_identity(tmp_path: Path, event_kind: str) -> None:
    engine = _upgraded_engine(tmp_path, f"{event_kind}_execution_identity.sqlite3")
    try:
        with engine.begin() as connection:
            _seed(connection)
        _expect_abort(
            engine,
            "INSERT INTO usage_event (usage_event_id, event_kind, result, event_fingerprint,"
            " ownership_json, cost_center_key, started_at_utc, completed_at_utc,"
            " effective_calendar_version_id, effective_at_utc, effective_period,"
            " recorded_calendar_version_id, recorded_at_utc, recorded_period, created_at_utc,"
            " adjustment_source_namespace, adjustment_source_id, adjustment_allocation_key,"
            " referenced_usage_event_id) VALUES"
            " (:event_id, :event_kind, 'adjusted', 'fp', '{}', 'user:u1',"
            " :now, :now, 'cal_1', :now, '2026-08', 'cal_1', :now, '2026-08', :now,"
            " 'meter_recheck', 'recheck-identity', 'allocation-1', 'ue_local')",
            "ck_usage_event_execution_identity",
            {
                "event_id": f"ue-{event_kind}-no-execution",
                "event_kind": event_kind,
            },
        )
    finally:
        engine.dispose()


def test_usage_event_adjustment_requires_source_and_reference(tmp_path: Path) -> None:
    engine = _upgraded_engine(tmp_path, "ue_adjust_shape.sqlite3")
    try:
        with engine.begin() as connection:
            _seed(connection)
        # usage_adjustment 缺 referenced_usage_event_id → 拒绝
        _expect_abort(
            engine,
            "INSERT INTO usage_event (usage_event_id, event_kind, result, event_fingerprint,"
            " ownership_json, cost_center_key, started_at_utc, completed_at_utc,"
            " effective_calendar_version_id, effective_at_utc, effective_period,"
            " recorded_calendar_version_id, recorded_at_utc, recorded_period, created_at_utc,"
            " execution_kind, execution_id, adjustment_source_namespace, adjustment_source_id)"
            " VALUES ('ue_bad', 'usage_adjustment', 'adjusted', 'fp', '{}', 'user:u1',"
            " :now, :now, 'cal_1', :now, '2026-08', 'cal_1', :now, '2026-08', :now,"
            " 'adjustment', 'exec-bad', 'meter_recheck', 'recheck-1')",
            "CHECK",
        )
        # cost_adjustment 缺 adjustment_source_namespace → 拒绝
        _expect_abort(
            engine,
            "INSERT INTO usage_event (usage_event_id, event_kind, result, event_fingerprint,"
            " ownership_json, cost_center_key, started_at_utc, completed_at_utc,"
            " effective_calendar_version_id, effective_at_utc, effective_period,"
            " recorded_calendar_version_id, recorded_at_utc, recorded_period, created_at_utc,"
            " execution_kind, execution_id, adjustment_source_id, referenced_usage_event_id)"
            " VALUES ('ue_bad2', 'cost_adjustment', 'cost_adjusted', 'fp', '{}', 'user:u1',"
            " :now, :now, 'cal_1', :now, '2026-08', 'cal_1', :now, '2026-08', :now,"
            " 'adjustment', 'exec-bad2', 'adj-1', 'ue_local')",
            "CHECK",
        )
        # usage_adjustment 缺 adjustment_allocation_key → 拒绝（review 复审 #3）
        _expect_abort(
            engine,
            "INSERT INTO usage_event (usage_event_id, event_kind, result, event_fingerprint,"
            " ownership_json, cost_center_key, started_at_utc, completed_at_utc,"
            " effective_calendar_version_id, effective_at_utc, effective_period,"
            " recorded_calendar_version_id, recorded_at_utc, recorded_period, created_at_utc,"
            " execution_kind, execution_id, adjustment_source_namespace, adjustment_source_id,"
            " referenced_usage_event_id) VALUES"
            " ('ue_bad3', 'usage_adjustment', 'adjusted', 'fp', '{}', 'user:u1',"
            " :now, :now, 'cal_1', :now, '2026-08', 'cal_1', :now, '2026-08', :now,"
            " 'adjustment', 'exec-bad3', 'meter_recheck', 'recheck-1', 'ue_local')",
            "CHECK",
        )
        # 合法 adjustment（含 allocation_key）→ 允许（无 NULL 重复行为：非 NULL 全量填充）
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO usage_event (usage_event_id, event_kind, result, event_fingerprint,"
                    " ownership_json, cost_center_key, started_at_utc, completed_at_utc,"
                    " effective_calendar_version_id, effective_at_utc, effective_period,"
                    " recorded_calendar_version_id, recorded_at_utc, recorded_period, created_at_utc,"
                    " execution_kind, execution_id, adjustment_source_namespace,"
                    " adjustment_source_id, adjustment_allocation_key,"
                    " referenced_usage_event_id) VALUES"
                    " ('ue_ok_adj', 'usage_adjustment', 'adjusted', 'fp', '{}', 'user:u1',"
                    " :now, :now, 'cal_1', :now, '2026-08', 'cal_1', :now, '2026-08', :now,"
                    " 'adjustment', 'exec-ok', 'meter_recheck', 'recheck-1', 'ocr-gpu', 'ue_local')"
                ),
                {"now": NOW},
            )
        assert _count(engine, "usage_event") == 3
    finally:
        engine.dispose()


def test_usage_event_cost_center_is_required_for_all_kinds(tmp_path: Path) -> None:
    engine = _upgraded_engine(tmp_path, "ue_cost_center.sqlite3")
    try:
        with engine.begin() as connection:
            _seed(connection)
        # local_usage 缺 cost_center_key → 拒绝
        _expect_abort(
            engine,
            "INSERT INTO usage_event (usage_event_id, event_kind, result, event_fingerprint,"
            " ownership_json, started_at_utc, completed_at_utc,"
            " effective_calendar_version_id, effective_at_utc, effective_period,"
            " recorded_calendar_version_id, recorded_at_utc, recorded_period, created_at_utc,"
            " execution_kind, execution_id, stage, resource_kind) VALUES"
            " ('ue_bad', 'local_usage', 'succeeded', 'fp', '{}',"
            " :now, :now, 'cal_1', :now, '2026-08', 'cal_1', :now, '2026-08', :now,"
            " 'ocr', 'exec1', 'extract', 'pdf')",
            "CHECK",
        )
        assert _count(engine, "usage_event") == 2
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------
# quota_debit 条件约束（review #5）
# ---------------------------------------------------------------------------


def test_quota_debit_rejects_null_bypass_and_wrong_signs(tmp_path: Path) -> None:
    engine = _upgraded_engine(tmp_path, "qd_shape.sqlite3")
    try:
        with engine.begin() as connection:
            _seed(connection)
        base = (
            "INSERT INTO quota_debit (quota_debit_id, entry_kind, page_delta, entry_fingerprint,"
            " cost_center_key, ownership_json, effective_calendar_version_id, effective_at_utc,"
            " effective_period, recorded_calendar_version_id, recorded_at_utc, recorded_period,"
            " created_at_utc"
        )
        # debit 缺 subject/period（NULL 绕过投影形状）→ 拒绝
        _expect_abort(
            engine,
            base + ") VALUES ('x1', 'debit', 10, 'fp', 'user:u1', '{}', 'cal_1',"
            " :now, '2026-08', 'cal_1', :now, '2026-08', :now)",
            "CHECK",
        )
        # debit 负 delta → 拒绝
        _expect_abort(
            engine,
            base + ", quota_operation_id, publication_id, quota_subject_user_id, quota_period)"
            " VALUES ('x2', 'debit', -10, 'fp', 'user:u1', '{}', 'cal_1',"
            " :now, '2026-08', 'cal_1', :now, '2026-08', :now, 'op2', 'pub2', 'u1', '2026-08')",
            "CHECK",
        )
        # debit 缺 quota_operation_id → 拒绝
        _expect_abort(
            engine,
            base + ", publication_id, quota_subject_user_id, quota_period)"
            " VALUES ('x3', 'debit', 10, 'fp', 'user:u1', '{}', 'cal_1',"
            " :now, '2026-08', 'cal_1', :now, '2026-08', :now, 'pub3', 'u1', '2026-08')",
            "CHECK",
        )
        # reversal 正 delta → 拒绝
        _expect_abort(
            engine,
            base + ", referenced_debit_id, adjustment_source_namespace, adjustment_source_id,"
            " quota_subject_user_id, quota_period)"
            " VALUES ('x4', 'reversal', 5, 'fp', 'user:u1', '{}', 'cal_1',"
            " :now, '2026-08', 'cal_1', :now, '2026-08', :now, 'qd_1', 'billing', 'rev-1',"
            " 'u1', '2026-08')",
            "CHECK",
        )
        # reversal 缺 referenced_debit_id → 拒绝
        _expect_abort(
            engine,
            base + ", adjustment_source_namespace, adjustment_source_id, quota_subject_user_id,"
            " quota_period) VALUES ('x5', 'reversal', -5, 'fp', 'user:u1', '{}', 'cal_1',"
            " :now, '2026-08', 'cal_1', :now, '2026-08', :now, 'billing', 'rev-1',"
            " 'u1', '2026-08')",
            "CHECK",
        )
        # supplement 缺 source → 拒绝
        _expect_abort(
            engine,
            base + ", referenced_debit_id, adjustment_source_id, quota_subject_user_id,"
            " quota_period) VALUES ('x6', 'supplement', 5, 'fp', 'user:u1', '{}', 'cal_1',"
            " :now, '2026-08', 'cal_1', :now, '2026-08', :now, 'qd_1', 'sup-1',"
            " 'u1', '2026-08')",
            "CHECK",
        )
        # credit 非 quota_request namespace → 拒绝
        _expect_abort(
            engine,
            base + ", adjustment_source_namespace, adjustment_source_id, quota_subject_user_id,"
            " quota_period) VALUES ('x7', 'credit', -20, 'fp', 'user:u1', '{}', 'cal_1',"
            " :now, '2026-08', 'cal_1', :now, '2026-08', :now, 'billing', 'adj-1',"
            " 'u1', '2026-08')",
            "CHECK",
        )
        # credit 正 delta → 拒绝
        _expect_abort(
            engine,
            base + ", adjustment_source_namespace, adjustment_source_id, quota_subject_user_id,"
            " quota_period) VALUES ('x8', 'credit', 20, 'fp', 'user:u1', '{}', 'cal_1',"
            " :now, '2026-08', 'cal_1', :now, '2026-08', :now, 'quota_request', 'req-1',"
            " 'u1', '2026-08')",
            "CHECK",
        )
        # 全部失败后账本仍只有初始 debit
        assert _count(engine, "quota_debit") == 1
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------
# provider_call 状态-时间形状（复审 #4）
# ---------------------------------------------------------------------------


def test_provider_call_completed_requires_dispatching_and_no_not_sent(tmp_path: Path) -> None:
    engine = _upgraded_engine(tmp_path, "pc_completed.sqlite3")
    try:
        with engine.begin() as connection:
            _seed(connection)
        # completed 缺 dispatching_at → 拒绝
        _expect_abort(
            engine,
            "INSERT INTO provider_call (provider_call_id, provider, model, operation,"
            " execution_kind, execution_id, attempt_id, deadline_utc, request_fingerprint, status, prepared_at_utc,"
            " completed_at_utc, created_at_utc) VALUES"
            " ('pc_bad1', 'dashscope', 'qwen-max', 'chat', 'chat', 'exec1', 'attempt-raw', :now, 'fp',"
            " 'completed', :now, :now, :now)",
            "CHECK",
        )
        # completed 带 not_sent_at → 拒绝
        _expect_abort(
            engine,
            "INSERT INTO provider_call (provider_call_id, provider, model, operation,"
            " execution_kind, execution_id, attempt_id, deadline_utc, request_fingerprint, status, prepared_at_utc,"
            " dispatching_at_utc, not_sent_at_utc, completed_at_utc, created_at_utc) VALUES"
            " ('pc_bad2', 'dashscope', 'qwen-max', 'chat', 'chat', 'exec1', 'attempt-raw', :now, 'fp',"
            " 'completed', :now, :now, :now, :now, :now)",
            "CHECK",
        )
        # completed 缺 completed_at → 拒绝
        _expect_abort(
            engine,
            "INSERT INTO provider_call (provider_call_id, provider, model, operation,"
            " execution_kind, execution_id, attempt_id, deadline_utc, request_fingerprint, status, prepared_at_utc,"
            " dispatching_at_utc, created_at_utc) VALUES"
            " ('pc_bad3', 'dashscope', 'qwen-max', 'chat', 'chat', 'exec1', 'attempt-raw', :now, 'fp',"
            " 'completed', :now, :now, :now)",
            "CHECK",
        )
        assert _count(engine, "provider_call") == 0
    finally:
        engine.dispose()


def test_provider_call_unknown_requires_dispatching_and_no_completed_not_sent(
    tmp_path: Path,
) -> None:
    engine = _upgraded_engine(tmp_path, "pc_unknown.sqlite3")
    try:
        with engine.begin() as connection:
            _seed(connection)
        # unknown 缺 dispatching_at → 拒绝
        _expect_abort(
            engine,
            "INSERT INTO provider_call (provider_call_id, provider, model, operation,"
            " execution_kind, execution_id, attempt_id, deadline_utc, request_fingerprint, status, prepared_at_utc,"
            " unknown_at_utc, created_at_utc) VALUES"
            " ('pc_bad1', 'dashscope', 'qwen-max', 'chat', 'chat', 'exec1', 'attempt-raw', :now, 'fp',"
            " 'unknown', :now, :now, :now)",
            "CHECK",
        )
        # unknown 带 completed_at → 拒绝（恢复需先转 completed）
        _expect_abort(
            engine,
            "INSERT INTO provider_call (provider_call_id, provider, model, operation,"
            " execution_kind, execution_id, attempt_id, deadline_utc, request_fingerprint, status, prepared_at_utc,"
            " dispatching_at_utc, unknown_at_utc, completed_at_utc, created_at_utc) VALUES"
            " ('pc_bad2', 'dashscope', 'qwen-max', 'chat', 'chat', 'exec1', 'attempt-raw', :now, 'fp',"
            " 'unknown', :now, :now, :now, :now, :now)",
            "CHECK",
        )
        # unknown 带 not_sent_at → 拒绝
        _expect_abort(
            engine,
            "INSERT INTO provider_call (provider_call_id, provider, model, operation,"
            " execution_kind, execution_id, attempt_id, deadline_utc, request_fingerprint, status, prepared_at_utc,"
            " dispatching_at_utc, not_sent_at_utc, unknown_at_utc, created_at_utc) VALUES"
            " ('pc_bad3', 'dashscope', 'qwen-max', 'chat', 'chat', 'exec1', 'attempt-raw', :now, 'fp',"
            " 'unknown', :now, :now, :now, :now, :now)",
            "CHECK",
        )
        assert _count(engine, "provider_call") == 0
    finally:
        engine.dispose()


def test_provider_call_unknown_then_completed_recovery_is_allowed(tmp_path: Path) -> None:
    engine = _upgraded_engine(tmp_path, "pc_recovery.sqlite3")
    try:
        with engine.begin() as connection:
            _seed(connection)
        # 合法：unknown（dispatching+started+unknown）→ completed（dispatching+completed，保留 unknown）
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO provider_call (provider_call_id, provider, model, operation,"
                    " execution_kind, execution_id, attempt_id, deadline_utc, request_fingerprint, status, prepared_at_utc,"
                    " dispatching_at_utc, started_at_utc, unknown_at_utc, created_at_utc) VALUES"
                    " ('pc_ok', 'dashscope', 'qwen-max', 'chat', 'chat', 'exec1', 'attempt-raw', :now, 'fp',"
                    " 'unknown', :now, :now, :now, :now, :now)"
                ),
                {"now": NOW},
            )
        with engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE provider_call SET status = 'completed', completed_at_utc = :now,"
                    " not_sent_at_utc = NULL WHERE provider_call_id = 'pc_ok'"
                ),
                {"now": NOW},
            )
        with engine.connect() as connection:
            status = connection.execute(
                text("SELECT status FROM provider_call WHERE provider_call_id = 'pc_ok'")
            ).scalar_one()
        assert status == "completed"
    finally:
        engine.dispose()


def test_provider_call_started_at_shape(tmp_path: Path) -> None:
    """review agent-11 #3/#12：started_at_utc 只在 dispatch 后的状态出现；
    dispatching/completed/unknown 必须有 started；prepared 不得有。"""
    engine = _upgraded_engine(tmp_path, "pc_started.sqlite3")
    try:
        with engine.begin() as connection:
            _seed(connection)
        # dispatching 缺 started_at → 拒绝
        _expect_abort(
            engine,
            "INSERT INTO provider_call (provider_call_id, provider, model, operation,"
            " execution_kind, execution_id, attempt_id, deadline_utc, request_fingerprint, status, prepared_at_utc,"
            " dispatching_at_utc, created_at_utc) VALUES"
            " ('pc_bad1', 'dashscope', 'qwen-max', 'chat', 'chat', 'exec1', 'attempt-raw', :now, 'fp',"
            " 'dispatching', :now, :now, :now)",
            "CHECK",
        )
        # completed 缺 started_at → 拒绝
        _expect_abort(
            engine,
            "INSERT INTO provider_call (provider_call_id, provider, model, operation,"
            " execution_kind, execution_id, attempt_id, deadline_utc, request_fingerprint, status, prepared_at_utc,"
            " dispatching_at_utc, completed_at_utc, created_at_utc) VALUES"
            " ('pc_bad2', 'dashscope', 'qwen-max', 'chat', 'chat', 'exec1', 'attempt-raw', :now, 'fp',"
            " 'completed', :now, :now, :now, :now)",
            "CHECK",
        )
        # unknown 缺 started_at → 拒绝
        _expect_abort(
            engine,
            "INSERT INTO provider_call (provider_call_id, provider, model, operation,"
            " execution_kind, execution_id, attempt_id, deadline_utc, request_fingerprint, status, prepared_at_utc,"
            " dispatching_at_utc, unknown_at_utc, created_at_utc) VALUES"
            " ('pc_bad3', 'dashscope', 'qwen-max', 'chat', 'chat', 'exec1', 'attempt-raw', :now, 'fp',"
            " 'unknown', :now, :now, :now, :now)",
            "CHECK",
        )
        # prepared 带 started_at / last_reconcile_attempt → 拒绝
        _expect_abort(
            engine,
            "INSERT INTO provider_call (provider_call_id, provider, model, operation,"
            " execution_kind, execution_id, attempt_id, deadline_utc, request_fingerprint, status, prepared_at_utc,"
            " started_at_utc, created_at_utc) VALUES"
            " ('pc_bad4', 'dashscope', 'qwen-max', 'chat', 'chat', 'exec1', 'attempt-raw', :now, 'fp',"
            " 'prepared', :now, :now, :now)",
            "CHECK",
        )
        _expect_abort(
            engine,
            "INSERT INTO provider_call (provider_call_id, provider, model, operation,"
            " execution_kind, execution_id, attempt_id, deadline_utc, request_fingerprint, status, prepared_at_utc,"
            " last_reconcile_attempt_at_utc, created_at_utc) VALUES"
            " ('pc_bad5', 'dashscope', 'qwen-max', 'chat', 'chat', 'exec1', 'attempt-raw', :now, 'fp',"
            " 'prepared', :now, :now, :now)",
            "CHECK",
        )
        # 合法 dispatching（带 started_at）→ 允许
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO provider_call (provider_call_id, provider, model, operation,"
                    " execution_kind, execution_id, attempt_id, deadline_utc, request_fingerprint, status, prepared_at_utc,"
                    " dispatching_at_utc, started_at_utc, created_at_utc) VALUES"
                    " ('pc_ok', 'dashscope', 'qwen-max', 'chat', 'chat', 'exec1', 'attempt-raw', :now, 'fp',"
                    " 'dispatching', :now, :now, :now, :now)"
                ),
                {"now": NOW},
            )
        with engine.connect() as connection:
            count = connection.execute(
                text("SELECT COUNT(*) FROM provider_call WHERE provider_call_id = 'pc_ok'")
            ).scalar_one()
        assert count == 1
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------
# measurement_sources 合法值 INSERT trigger（复审 #5）
# ---------------------------------------------------------------------------

_MEASUREMENT_BASE = (
    "INSERT INTO usage_event (usage_event_id, event_kind, result, event_fingerprint,"
    " ownership_json, cost_center_key, started_at_utc, completed_at_utc,"
    " effective_calendar_version_id, effective_at_utc, effective_period,"
    " recorded_calendar_version_id, recorded_at_utc, recorded_period, created_at_utc,"
    " execution_kind, execution_id, stage, resource_kind, measurement_sources)"
    " VALUES ('ue_ms_%s', 'local_usage', 'succeeded', 'fp', '{}', 'user:u1',"
    " :now, :now, 'cal_1', :now, '2026-08', 'cal_1', :now, '2026-08', :now,"
    " 'ocr', 'exec_ms_%s', 'extract', 'pdf', %s)"
)


def _measurement_sql(marker: str, payload_json: str) -> str:
    """把 JSON 载荷包成 SQL 字符串字面量（转义单引号），execution_id 随 marker 唯一。"""
    return _MEASUREMENT_BASE % (marker, marker, "'" + payload_json.replace("'", "''") + "'")


def test_measurement_sources_accepts_three_legal_values(tmp_path: Path) -> None:
    engine = _upgraded_engine(tmp_path, "ms_legal.sqlite3")
    try:
        with engine.begin() as connection:
            _seed(connection)
        for marker, payload in (
            ("prov", '{"tokens": "provider_reported"}'),
            ("client", '{"pages": "client_measured"}'),
            ("est", '{"cost": "estimated"}'),
            ("mixed", '{"a": "provider_reported", "b": "estimated"}'),
            ("empty", "{}"),
        ):
            with engine.begin() as connection:
                connection.execute(
                    text(_measurement_sql(marker, payload)),
                    {"now": NOW},
                )
        # NULL 按 event_kind 规格允许（local_usage 无 measurement_sources 时）
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO usage_event (usage_event_id, event_kind, result, event_fingerprint,"
                    " ownership_json, cost_center_key, started_at_utc, completed_at_utc,"
                    " effective_calendar_version_id, effective_at_utc, effective_period,"
                    " recorded_calendar_version_id, recorded_at_utc, recorded_period, created_at_utc,"
                    " execution_kind, execution_id, stage, resource_kind) VALUES"
                    " ('ue_ms_null', 'local_usage', 'succeeded', 'fp', '{}', 'user:u1',"
                    " :now, :now, 'cal_1', :now, '2026-08', 'cal_1', :now, '2026-08', :now,"
                    " 'ocr', 'exec_ms_null', 'extract', 'pdf')"
                ),
                {"now": NOW},
            )
        assert _count(engine, "usage_event") == 2 + 5 + 1
    finally:
        engine.dispose()


def test_measurement_sources_rejects_invalid_value(tmp_path: Path) -> None:
    engine = _upgraded_engine(tmp_path, "ms_invalid.sqlite3")
    try:
        with engine.begin() as connection:
            _seed(connection)
        for marker, payload in (
            ("bad", '{"tokens": "provider_sniffed"}'),
            ("num", '{"tokens": 42}'),
            ("obj", '{"tokens": {"nested": "estimated"}}'),
        ):
            _expect_abort(
                engine,
                _measurement_sql(marker, payload),
                "measurement_sources",
            )
        assert _count(engine, "usage_event") == 2
    finally:
        engine.dispose()


def test_measurement_sources_rejects_non_object_top_level(tmp_path: Path) -> None:
    """复审 #7：measurement_sources 必须是 JSON object；顶层字符串/数组/数字/JSON null 均拒绝，
    合法 object/空 object/SQL NULL 继续通过。"""
    engine = _upgraded_engine(tmp_path, "ms_top_level.sqlite3")
    try:
        with engine.begin() as connection:
            _seed(connection)
        for marker, payload in (
            ("str", '"provider_reported"'),
            ("arr", '["provider_reported"]'),
            ("num", "42"),
            ("jnull", "null"),
        ):
            _expect_abort(
                engine,
                _measurement_sql(marker, payload),
                "must be a JSON object",
            )
        # 合法 object / 空 object 继续通过（SQL NULL 已在 accept 测试覆盖，这里再显式确认）
        with engine.begin() as connection:
            connection.execute(
                text(_measurement_sql("ok", '{"tokens": "provider_reported"}')),
                {"now": NOW},
            )
            connection.execute(text(_measurement_sql("empty", "{}")), {"now": NOW})
            connection.execute(
                text(
                    "INSERT INTO usage_event (usage_event_id, event_kind, result, event_fingerprint,"
                    " ownership_json, cost_center_key, started_at_utc, completed_at_utc,"
                    " effective_calendar_version_id, effective_at_utc, effective_period,"
                    " recorded_calendar_version_id, recorded_at_utc, recorded_period, created_at_utc,"
                    " execution_kind, execution_id, stage, resource_kind) VALUES"
                    " ('ue_ms_sql_null', 'local_usage', 'succeeded', 'fp', '{}', 'user:u1',"
                    " :now, :now, 'cal_1', :now, '2026-08', 'cal_1', :now, '2026-08', :now,"
                    " 'ocr', 'exec_ms_sql_null', 'extract', 'pdf')"
                ),
                {"now": NOW},
            )
        assert _count(engine, "usage_event") == 2 + 3
    finally:
        engine.dispose()
