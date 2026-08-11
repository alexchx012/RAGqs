from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import CheckConstraint, UniqueConstraint, create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from alembic import command
from alembic.config import Config
from app.usage import USAGE_TABLE_NAMES


def alembic_config(database_url: str) -> Config:
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", database_url)
    return config


def _upgraded_engine(tmp_path: Path, name: str):
    database_url = f"sqlite:///{tmp_path / name}"
    config = alembic_config(database_url)
    command.upgrade(config, "head")
    return create_engine(database_url)


def test_head_upgrade_creates_all_usage_tables(tmp_path: Path) -> None:
    engine = _upgraded_engine(tmp_path, "usage.sqlite3")
    try:
        tables = set(inspect(engine).get_table_names())
    finally:
        engine.dispose()
    assert USAGE_TABLE_NAMES <= tables
    # 不创建 usage_idempotency 表（预审 #5）
    assert "usage_idempotency" not in tables


def test_usage_revision_downgrade_removes_usage_tables(tmp_path: Path) -> None:
    database_url = f"sqlite:///{tmp_path / 'rollback.sqlite3'}"
    config = alembic_config(database_url)
    command.upgrade(config, "head")
    command.downgrade(config, "0002_identity_access")
    engine = create_engine(database_url)
    try:
        tables = set(inspect(engine).get_table_names())
    finally:
        engine.dispose()
    assert USAGE_TABLE_NAMES.isdisjoint(tables)
    assert "identity_user" in tables


def test_migration_creates_immutability_triggers(tmp_path: Path) -> None:
    """M2：迁移产物必须含账本与引用数据表的不可变/append-only 触发器（sqlite sqlite_master）。"""
    engine = _upgraded_engine(tmp_path, "triggers.sqlite3")
    try:
        with engine.connect() as connection:
            rows = (
                connection.execute(
                    text("SELECT name FROM sqlite_master WHERE type='trigger' ORDER BY name")
                )
                .scalars()
                .all()
            )
    finally:
        engine.dispose()
    assert "trg_usage_event_no_update" in rows
    assert "trg_usage_event_no_delete" in rows
    assert "trg_usage_event_measurement_sources" in rows
    assert "trg_quota_debit_no_update" in rows
    assert "trg_quota_debit_no_delete" in rows
    assert "trg_calendar_version_no_update" in rows
    assert "trg_calendar_version_no_delete" in rows
    assert "trg_price_line_no_update" in rows
    assert "trg_price_line_no_delete" in rows
    assert "trg_price_catalog_no_update" in rows
    assert "trg_price_catalog_no_delete" in rows


def _normalize_check_tokens(sql: str) -> tuple[str, ...]:
    """稳健的 CHECK 方言规范化：去 CHECK() 外壳、PG 双引号标识符与 <> 运算、空白归一。

    SQLite 反射的是 CREATE TABLE 原文（无外层 CHECK 括号），PostgreSQL 反射的是
    pg_get_constraintdef 输出（标识符带双引号、!= 渲染为 <>、可能多一层括号）。
    返回有序 token tuple（review #6：保留 token 顺序以便名称级比对）。
    """
    text = sql.strip()
    if text.upper().startswith("CHECK"):
        text = text[len("CHECK") :].strip()
        if text.startswith("(") and text.endswith(")"):
            text = text[1:-1]
    text = text.replace('"', "")
    text = text.replace("<>", "!=")
    text = text.replace("(", " ( ").replace(")", " ) ").replace(",", " , ")
    return tuple(text.split())


def _declared_check_sets(declared) -> set[tuple[str, tuple[str, ...]]]:
    return {
        (c.name, _normalize_check_tokens(c.sqltext.text))
        for c in declared.constraints
        if isinstance(c, CheckConstraint)
    }


def test_upgraded_schema_matches_metadata(tmp_path: Path) -> None:
    """M2：列（column.name/type/nullable）、PK、Unique、Check（含名称）、Index 与 metadata parity。"""
    from app.usage.schema import usage_metadata as meta

    engine = _upgraded_engine(tmp_path, "parity.sqlite3")
    try:
        inspector = inspect(engine)
        for table_name in sorted(meta.tables):
            declared = meta.tables[table_name]
            actual_cols = {
                (c["name"], str(c["type"]), bool(c["nullable"]))
                for c in inspector.get_columns(table_name)
            }
            declared_cols = {(c.name, str(c.type), c.nullable) for c in declared.columns}
            assert actual_cols == declared_cols, table_name
            actual_pk = set(inspector.get_pk_constraint(table_name)["constrained_columns"])
            declared_pk = {c.name for c in declared.primary_key.columns}
            assert actual_pk == declared_pk, table_name
            actual_uc = {
                tuple(sorted(u["column_names"]))
                for u in inspector.get_unique_constraints(table_name)
            }
            declared_uc = {
                tuple(sorted(col.name for col in c.columns))
                for c in declared.constraints
                if isinstance(c, UniqueConstraint)
            }
            assert actual_uc == declared_uc, table_name
            actual_ck = {
                (ck["name"], _normalize_check_tokens(ck["sqltext"]))
                for ck in inspector.get_check_constraints(table_name)
                if ck.get("sqltext")
            }
            declared_ck = _declared_check_sets(declared)
            assert actual_ck == declared_ck, table_name
        # index parity：至少含两个 partial unique index，且 predicate 与 metadata 一致
        quota_indexes = {ix["name"]: ix for ix in inspector.get_indexes("quota_request")}
        price_indexes = {ix["name"]: ix for ix in inspector.get_indexes("price_catalog")}
        assert quota_indexes["uq_quota_request_pending"]["unique"] == 1
        assert tuple(quota_indexes["uq_quota_request_pending"]["column_names"]) == (
            "applicant_user_id",
            "quota_period",
        )
        assert price_indexes["uq_price_open_interval"]["unique"] == 1
        assert tuple(price_indexes["uq_price_open_interval"]["column_names"]) == (
            "provider",
            "model",
            "operation",
        )
        from sqlalchemy.dialects import sqlite as sqlite_dialect

        from app.usage.schema import (
            price_open_interval_index,
            quota_request_pending_unique,
        )

        for reflected_name, declared_index in (
            ("uq_quota_request_pending", quota_request_pending_unique),
            ("uq_price_open_interval", price_open_interval_index),
        ):
            table_name = (
                "quota_request" if reflected_name == "uq_quota_request_pending" else "price_catalog"
            )
            indexes_by_name = {ix["name"]: ix for ix in inspector.get_indexes(table_name)}
            declared_sql = str(
                declared_index.dialect_options["sqlite"]["where"].compile(
                    dialect=sqlite_dialect.dialect(), compile_kwargs={"literal_binds": True}
                )
            ).replace(f"{table_name}.", "")
            reflected_sql = str(indexes_by_name[reflected_name]["dialect_options"]["sqlite_where"])
            assert _normalize_check_tokens(reflected_sql) == _normalize_check_tokens(
                declared_sql
            ), reflected_name
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    ("rate", "granularity", "minimum_quantity", "rounding_rule", "constraint_name"),
    [
        ("-0.0000000001", 1, 0, "floor", "ck_price_line_rate_nonnegative"),
        ("NaN", 1, 0, "floor", "ck_price_line_rate_numeric"),
        ("1e21", 1, 0, "floor", "ck_price_line_rate_max"),
        ("0", 0, 0, "floor", "ck_price_line_granularity_positive"),
        ("0", 1.5, 0, "floor", "ck_price_line_granularity_integer"),
        ("0", "1.5", 0, "floor", "ck_price_line_granularity_integer"),
        ("0", "abc", 0, "floor", "ck_price_line_granularity_integer"),
        ("0", 2147483648, 0, "floor", "ck_price_line_granularity_int32"),
        ("0", 1, -1, "floor", "ck_price_line_minimum_nonnegative"),
        ("0", 1, 0.5, "floor", "ck_price_line_minimum_integer"),
        ("0", 1, "0.5", "floor", "ck_price_line_minimum_integer"),
        ("0", 1, "abc", "floor", "ck_price_line_minimum_integer"),
        ("0", 1, 2147483648, "floor", "ck_price_line_minimum_int32"),
        ("0", 1, 0, "round_half_up", "ck_price_line_rounding_rule"),
    ],
)
def test_migrated_price_line_rejects_invalid_raw_insert(
    tmp_path: Path,
    rate: object,
    granularity: object,
    minimum_quantity: object,
    rounding_rule: str,
    constraint_name: str,
) -> None:
    """Alembic SQLite 真实表必须拒绝非法、非整数或超存储域 price line。"""
    engine = _upgraded_engine(tmp_path, f"price_line_{constraint_name}.sqlite3")
    try:
        with pytest.raises(IntegrityError, match=constraint_name):
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "INSERT INTO price_catalog_line (id, price_version_id, meter, unit, rate,"
                        " billing_granularity, minimum_billable_quantity, rounding_rule) VALUES"
                        " (:id, 'price-raw', 'input_tokens', 'token', :rate, :granularity,"
                        " :minimum_quantity, :rounding_rule)"
                    ),
                    {
                        "id": f"line-{constraint_name}",
                        "rate": rate,
                        "granularity": granularity,
                        "minimum_quantity": minimum_quantity,
                        "rounding_rule": rounding_rule,
                    },
                )
    finally:
        engine.dispose()


def test_migrated_provider_call_requires_attempt_or_generation_identity(tmp_path: Path) -> None:
    engine = _upgraded_engine(tmp_path, "provider_call_identity.sqlite3")
    try:
        with pytest.raises(IntegrityError, match="ck_provider_call_attempt_or_generation"):
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "INSERT INTO provider_call (provider_call_id, provider, model, operation,"
                        " execution_kind, execution_id, request_fingerprint, deadline_utc, status,"
                        " prepared_at_utc, created_at_utc) VALUES"
                        " ('pc-no-identity', 'p', 'm', 'o', 'generation', 'gen-1', 'fp-1',"
                        " :deadline, 'prepared', :now, :now)"
                    ),
                    {
                        "deadline": "2026-08-01T00:05:00+00:00",
                        "now": "2026-08-01T00:00:00+00:00",
                    },
                )
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    (
        "status",
        "dispatching_at_utc",
        "started_at_utc",
        "completed_at_utc",
        "not_sent_at_utc",
        "unknown_at_utc",
        "constraint_name",
    ),
    [
        pytest.param(
            "not_sent",
            None,
            "2026-08-01T00:00:01+00:00",
            None,
            "2026-08-01T00:00:02+00:00",
            None,
            "ck_provider_call_not_sent_at",
            id="not-sent-carries-started",
        ),
        pytest.param(
            "completed",
            "2026-08-01T00:00:01+00:00",
            "2026-08-01T00:00:01+00:00",
            None,
            None,
            None,
            "ck_provider_call_completed_at",
            id="completed-missing-completed-at",
        ),
        pytest.param(
            "unknown",
            "2026-08-01T00:00:01+00:00",
            "2026-08-01T00:00:01+00:00",
            None,
            None,
            None,
            "ck_provider_call_unknown_at",
            id="unknown-missing-unknown-at",
        ),
    ],
)
def test_migrated_provider_call_rejects_single_state_timestamp_violation(
    tmp_path: Path,
    status: str,
    dispatching_at_utc: str | None,
    started_at_utc: str | None,
    completed_at_utc: str | None,
    not_sent_at_utc: str | None,
    unknown_at_utc: str | None,
    constraint_name: str,
) -> None:
    """R5：Alembic 真实表 raw SQL 每次只违反一个命名状态时间约束。"""
    engine = _upgraded_engine(tmp_path, f"provider_call_{status}.sqlite3")
    values = {
        "provider_call_id": f"pc-invalid-{status}",
        "status": status,
        "prepared_at_utc": "2026-08-01T00:00:00+00:00",
        "dispatching_at_utc": dispatching_at_utc,
        "started_at_utc": started_at_utc,
        "completed_at_utc": completed_at_utc,
        "not_sent_at_utc": not_sent_at_utc,
        "unknown_at_utc": unknown_at_utc,
        "created_at_utc": "2026-08-01T00:00:00+00:00",
    }
    try:
        with pytest.raises(IntegrityError, match=constraint_name):
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "INSERT INTO provider_call (provider_call_id, provider, model, operation,"
                        " execution_kind, execution_id, attempt_id, deadline_utc,"
                        " request_fingerprint, status, prepared_at_utc, dispatching_at_utc,"
                        " started_at_utc, completed_at_utc, not_sent_at_utc, unknown_at_utc,"
                        " created_at_utc) VALUES"
                        " (:provider_call_id, 'p', 'm', 'o', 'generation', 'gen-1', 'attempt-raw',"
                        " '2026-08-01T00:05:00+00:00', 'fp-1', :status, :prepared_at_utc,"
                        " :dispatching_at_utc, :started_at_utc, :completed_at_utc,"
                        " :not_sent_at_utc, :unknown_at_utc, :created_at_utc)"
                    ),
                    values,
                )
        with engine.connect() as connection:
            assert connection.execute(text("SELECT COUNT(*) FROM provider_call")).scalar_one() == 0
    finally:
        engine.dispose()


def test_no_usage_idempotency_table_in_metadata() -> None:
    """预审 #5：schema 不声明 usage_idempotency 表。"""
    from app.usage.schema import usage_metadata as meta

    assert "usage_idempotency" not in meta.tables
    assert (
        len(meta.tables) == 10
    )  # calendar + price_scope + price_catalog + price_line + provider_call
    # + usage_event + quota_debit + quota_projection + quota_request + reconciliation


# ---------------------------------------------------------------------------
# partial unique index 行为（review #6）：predicate 必须在数据库层面生效
# ---------------------------------------------------------------------------


def test_price_open_interval_index_rejects_second_open_version(tmp_path: Path) -> None:
    engine = _upgraded_engine(tmp_path, "price_open.sqlite3")
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO price_catalog (id, provider, model, operation, currency_code,"
                    " effective_from_utc, created_at_utc) VALUES"
                    " ('p1', 'dashscope', 'qwen-max', 'chat', 'CNY', :now, :now)"
                ),
                {"now": "2026-08-01T00:00:00+00:00"},
            )
        # 同 scope 第二个 open 版本必须被拒绝
        with pytest.raises(Exception) as exc:
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "INSERT INTO price_catalog (id, provider, model, operation,"
                        " currency_code, effective_from_utc, created_at_utc) VALUES"
                        " ('p2', 'dashscope', 'qwen-max', 'chat', 'CNY', :now, :now)"
                    ),
                    {"now": "2026-08-15T00:00:00+00:00"},
                )
        assert "UNIQUE" in str(exc.value)
        with engine.connect() as connection:
            count = connection.execute(text("SELECT COUNT(*) FROM price_catalog")).scalar_one()
        assert count == 1
        # closed + open 允许（同 scope）
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO price_catalog (id, provider, model, operation, currency_code,"
                    " effective_from_utc, effective_to_utc, created_at_utc) VALUES"
                    " ('p2', 'dashscope', 'qwen-max', 'chat', 'CNY', :from, :to, :now)"
                ),
                {
                    "from": "2026-08-15T00:00:00+00:00",
                    "to": "2026-08-31T00:00:00+00:00",
                    "now": "2026-08-15T00:00:00+00:00",
                },
            )
        with engine.connect() as connection:
            count = connection.execute(text("SELECT COUNT(*) FROM price_catalog")).scalar_one()
        assert count == 2
    finally:
        engine.dispose()


def test_price_open_interval_index_allows_closed_and_open_versions(tmp_path: Path) -> None:
    engine = _upgraded_engine(tmp_path, "price_closed.sqlite3")
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO price_catalog (id, provider, model, operation, currency_code,"
                    " effective_from_utc, effective_to_utc, created_at_utc) VALUES"
                    " ('p1', 'dashscope', 'qwen-max', 'chat', 'CNY', :from, :to, :now)"
                ),
                {
                    "from": "2026-08-01T00:00:00+00:00",
                    "to": "2026-08-31T00:00:00+00:00",
                    "now": "2026-08-01T00:00:00+00:00",
                },
            )
        # closed + open 允许（同一 scope）
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO price_catalog (id, provider, model, operation, currency_code,"
                    " effective_from_utc, created_at_utc) VALUES"
                    " ('p2', 'dashscope', 'qwen-max', 'chat', 'CNY', :now, :now)"
                ),
                {"now": "2026-09-01T00:00:00+00:00"},
            )
        with engine.connect() as connection:
            count = connection.execute(text("SELECT COUNT(*) FROM price_catalog")).scalar_one()
        assert count == 2
    finally:
        engine.dispose()


def test_quota_request_pending_index_rejects_second_pending(tmp_path: Path) -> None:
    engine = _upgraded_engine(tmp_path, "req_pending.sqlite3")
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO business_calendar_version (id, version_id, timezone,"
                    " effective_from_utc, created_at_utc) VALUES"
                    " ('instance', 'cal_1', 'UTC', :now, :now)"
                ),
                {"now": "2026-08-01T00:00:00+00:00"},
            )
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO quota_request (quota_request_id, version, applicant_user_id,"
                    " applicant_role_snapshot, quota_period, business_calendar_version_id,"
                    " requested_pages, status, idempotency_fingerprint, created_at_utc,"
                    " updated_at_utc) VALUES"
                    " ('r1', 1, 'u1', 'user', '2026-08', 'cal_1', 100, 'pending',"
                    " 'fp1', :now, :now)"
                ),
                {"now": "2026-08-01T00:00:00+00:00"},
            )
        # 仅第二次 INSERT 期望失败（partial unique predicate 生效）
        with pytest.raises(IntegrityError, match="UNIQUE"):
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "INSERT INTO quota_request (quota_request_id, version, applicant_user_id,"
                        " applicant_role_snapshot, quota_period, business_calendar_version_id,"
                        " requested_pages, status, idempotency_fingerprint, created_at_utc,"
                        " updated_at_utc) VALUES"
                        " ('r2', 1, 'u1', 'user', '2026-08', 'cal_1', 200, 'pending',"
                        " 'fp2', :now, :now)"
                    ),
                    {"now": "2026-08-02T00:00:00+00:00"},
                )
        with engine.connect() as connection:
            count = connection.execute(text("SELECT COUNT(*) FROM quota_request")).scalar_one()
        assert count == 1
    finally:
        engine.dispose()


def test_quota_request_pending_index_allows_terminal_then_pending(tmp_path: Path) -> None:
    engine = _upgraded_engine(tmp_path, "req_terminal.sqlite3")
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO business_calendar_version (id, version_id, timezone,"
                    " effective_from_utc, created_at_utc) VALUES"
                    " ('instance', 'cal_1', 'UTC', :now, :now)"
                ),
                {"now": "2026-08-01T00:00:00+00:00"},
            )
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO quota_request (quota_request_id, version, applicant_user_id,"
                    " applicant_role_snapshot, quota_period, business_calendar_version_id,"
                    " requested_pages, status, idempotency_fingerprint, created_at_utc,"
                    " updated_at_utc) VALUES"
                    " ('r1', 1, 'u1', 'user', '2026-08', 'cal_1', 100, 'rejected',"
                    " 'fp1', :now, :now)"
                ),
                {"now": "2026-08-01T00:00:00+00:00"},
            )
        # 终态 + pending 允许（同一申请人同月）
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO quota_request (quota_request_id, version, applicant_user_id,"
                    " applicant_role_snapshot, quota_period, business_calendar_version_id,"
                    " requested_pages, status, idempotency_fingerprint, created_at_utc,"
                    " updated_at_utc) VALUES"
                    " ('r2', 1, 'u1', 'user', '2026-08', 'cal_1', 200, 'pending',"
                    " 'fp2', :now, :now)"
                ),
                {"now": "2026-08-02T00:00:00+00:00"},
            )
        with engine.connect() as connection:
            count = connection.execute(text("SELECT COUNT(*) FROM quota_request")).scalar_one()
        assert count == 2
    finally:
        engine.dispose()
