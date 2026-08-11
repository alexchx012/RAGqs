"""QuotaService 投影读取 / rebuild 测试（Task 7）。

语义（正式 spec §2/§3 + Task 7 约束）：
- read_snapshot 缺投影返回基线（used=0、extra=0）且不建行；unlimited 角色返回固定
  形状（effective_limit=0、extra_granted=0）；reset_at 为业务时区下月月初。
- 投影 (quota_subject_user_id, quota_period) 可重建：rebuild 使用调用方 connection，
  按 quota_debit 全量重放（debit/supplement 加 used、reversal 减 used 下限 0、
  credit 加 extra），缺失投影 upsert 创建，存在则整体覆盖 used/extra/last_debit_id。
- 增量投影与 rebuild 重放必须一致（含 last_debit_id）；rebuild 幂等（重复执行不漂移）。
- read_snapshot 的 period 由日历实时推导：旧月投影不影响新业务月基线。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, delete, event, select
from sqlalchemy.engine import Connection
from sqlalchemy.pool import NullPool, StaticPool

from app.platform.database import SqlAlchemyDatabaseClock
from app.platform.errors import PlatformError
from app.usage.calendar import BusinessCalendarService
from app.usage.ledger import OwnershipSnapshot
from app.usage.quota import QuotaService
from app.usage.schema import quota_projection_table, usage_metadata

NOW = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)
SEPT = datetime(2026, 9, 3, 8, 0, tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class FixedClock:
    now: datetime

    def now_utc(self, connection: Connection | None = None) -> datetime:
        del connection
        return self.now


@dataclass
class MutableClock:
    now: datetime

    def now_utc(self, connection: Connection | None = None) -> datetime:
        del connection
        return self.now


def make_quota(clock: FixedClock | MutableClock | None = None):
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    usage_metadata.create_all(engine)
    if clock is None:
        clock = FixedClock(NOW)
    calendar = BusinessCalendarService(engine, SqlAlchemyDatabaseClock(engine), "Asia/Shanghai")
    return engine, QuotaService(engine, clock, calendar)


def ownership() -> OwnershipSnapshot:
    return OwnershipSnapshot(
        actor_user_id="u1",
        actor_role_snapshot="user",
        actor_department_id_snapshot=None,
        quota_subject_user_id="u1",
        cost_center_key="user:u1",
    )


def projection_row(connection, subject: str = "u1", period: str = "2026-08") -> dict | None:
    row = (
        connection.execute(
            select(quota_projection_table).where(
                quota_projection_table.c.quota_subject_user_id == subject,
                quota_projection_table.c.quota_period == period,
            )
        )
        .mappings()
        .one_or_none()
    )
    return dict(row) if row is not None else None


def seed_ledger(engine, quota) -> str:
    """debit(120) + credit(80) + reversal(20)；返回 debit id。"""
    with engine.begin() as connection:
        lock = quota.calendar.lock_or_verify(connection)
        debit1 = quota.append_debit(
            connection,
            quota_operation_id="job_1",
            publication_id="pub_1",
            quota_subject_user_id="u1",
            pages=120,
            ownership=ownership(),
            calendar_lock=lock,
            role="user",
            effective_at_utc=NOW,
        )
        assert debit1 is not None
        quota.append_credit(
            connection,
            quota_subject_user_id="u1",
            quota_period="2026-08",
            pages=80,
            adjustment_source_namespace="quota_request",
            adjustment_source_id="req_1",
            ownership=ownership(),
            calendar_lock=lock,
            now=NOW,
        )
        quota.append_reversal(
            connection,
            referenced_debit_id=debit1,
            pages=20,
            adjustment_source_namespace="billing",
            adjustment_source_id="rev-1",
            ownership=ownership(),
            calendar_lock=lock,
            now=NOW,
        )
        return debit1


def test_snapshot_unlimited_role_shape() -> None:
    engine, quota = make_quota()
    with engine.begin() as connection:
        lock = quota.calendar.lock_or_verify(connection)
        snapshot = quota.read_snapshot(connection, quota_subject_user_id="ops_1", role="ops")
        assert snapshot.unlimited is True
        assert snapshot.base_limit == 500
        assert snapshot.extra_granted == 0
        assert snapshot.effective_limit == 0
        assert snapshot.reset_at == quota.calendar.next_month_start_utc(lock, NOW)
        assert snapshot.business_timezone == "Asia/Shanghai"
        assert snapshot.quota_period == "2026-08"
        assert snapshot.pending_request is None
        # unlimited 读取也不建投影行
        rows = connection.execute(select(quota_projection_table)).mappings().all()
        assert rows == []


def test_read_snapshot_without_projection_returns_baseline_without_creating_row() -> None:
    engine, quota = make_quota()
    with engine.begin() as connection:
        quota.calendar.lock_or_verify(connection)
        snapshot = quota.read_snapshot(connection, quota_subject_user_id="u1", role="user")
        rows = connection.execute(select(quota_projection_table)).mappings().all()
    assert snapshot.used == 0
    assert snapshot.extra_granted == 0
    assert snapshot.effective_limit == 500
    assert snapshot.unlimited is False
    assert rows == []  # 读不建行


def test_projection_rebuild_matches_ledger_with_upsert() -> None:
    engine, quota = make_quota()
    debit_id = seed_ledger(engine, quota)
    # 删除投影行（rebuild 目标：upsert 重建）
    with engine.begin() as connection:
        connection.execute(
            delete(quota_projection_table).where(
                quota_projection_table.c.quota_subject_user_id == "u1"
            )
        )
    with engine.begin() as connection:
        quota.rebuild_projection(connection, quota_subject_user_id="u1", quota_period="2026-08")
        snapshot = quota.read_snapshot(connection, quota_subject_user_id="u1", role="user")
        row = projection_row(connection)
        assert row is not None
        assert row["last_debit_id"] == debit_id
    assert snapshot.used == 100  # 120 - 20
    assert snapshot.extra_granted == 80
    assert snapshot.effective_limit == 580


def test_projection_rebuild_includes_supplement_and_is_idempotent() -> None:
    clock = MutableClock(NOW)
    engine, quota = make_quota(clock)
    with engine.begin() as connection:
        lock = quota.calendar.lock_or_verify(connection)
        debit_id = quota.append_debit(
            connection,
            quota_operation_id="job_1",
            publication_id="pub_1",
            quota_subject_user_id="u1",
            pages=120,
            ownership=ownership(),
            calendar_lock=lock,
            role="user",
            effective_at_utc=NOW,
        )
        assert debit_id is not None
        clock.now = datetime(2026, 8, 5, 12, 1, tzinfo=UTC)
        supplement_id = quota.append_supplement(
            connection,
            referenced_debit_id=debit_id,
            pages=30,
            adjustment_source_namespace="meter_recheck",
            adjustment_source_id="sup-1",
            ownership=ownership(),
            calendar_lock=lock,
            now=clock.now,
        )
        clock.now = datetime(2026, 8, 5, 12, 2, tzinfo=UTC)
        quota.append_reversal(
            connection,
            referenced_debit_id=debit_id,
            pages=20,
            adjustment_source_namespace="billing",
            adjustment_source_id="rev-1",
            ownership=ownership(),
            calendar_lock=lock,
            now=clock.now,
        )
        clock.now = datetime(2026, 8, 5, 12, 3, tzinfo=UTC)
        quota.append_credit(
            connection,
            quota_subject_user_id="u1",
            quota_period="2026-08",
            pages=80,
            adjustment_source_namespace="quota_request",
            adjustment_source_id="req_1",
            ownership=ownership(),
            calendar_lock=lock,
            now=clock.now,
        )
    with engine.begin() as connection:
        connection.execute(delete(quota_projection_table))
    with engine.begin() as connection:
        quota.rebuild_projection(connection, quota_subject_user_id="u1", quota_period="2026-08")
        row = projection_row(connection)
        assert row is not None
        assert row["used"] == 130  # 120 + 30 - 20
        assert row["extra_granted"] == 80
        assert row["last_debit_id"] == supplement_id  # 最近 debit-kind 分录
        # 幂等：重复 rebuild 不漂移
        quota.rebuild_projection(connection, quota_subject_user_id="u1", quota_period="2026-08")
        row = projection_row(connection)
        assert row["used"] == 130
        assert row["extra_granted"] == 80
        assert row["last_debit_id"] == supplement_id


def test_projection_rebuild_without_rows_creates_baseline() -> None:
    engine, quota = make_quota()
    with engine.begin() as connection:
        quota.rebuild_projection(connection, quota_subject_user_id="ghost", quota_period="2026-08")
        row = projection_row(connection, subject="ghost")
        assert row is not None
        assert row["used"] == 0
        assert row["extra_granted"] == 0
        assert row["last_debit_id"] is None
        snapshot = quota.read_snapshot(connection, quota_subject_user_id="ghost", role="user")
        assert snapshot.used == 0
        assert snapshot.effective_limit == 500


def test_rebuild_uses_caller_connection() -> None:
    """rebuild 的写入属于调用方事务：调用方回滚后投影行不存在。"""
    engine, quota = make_quota()
    connection = engine.connect()
    tx = connection.begin()
    try:
        quota.rebuild_projection(connection, quota_subject_user_id="u1", quota_period="2026-08")
        inside = connection.execute(select(quota_projection_table)).mappings().all()
        assert len(inside) == 1
        tx.rollback()
    finally:
        connection.close()
    with engine.connect() as connection:
        rows = connection.execute(select(quota_projection_table)).mappings().all()
    assert rows == []


def test_incremental_projection_matches_rebuild() -> None:
    clock = MutableClock(NOW)
    engine, quota = make_quota(clock)
    with engine.begin() as connection:
        lock = quota.calendar.lock_or_verify(connection)
        debit_id = quota.append_debit(
            connection,
            quota_operation_id="job_1",
            publication_id="pub_1",
            quota_subject_user_id="u1",
            pages=120,
            ownership=ownership(),
            calendar_lock=lock,
            role="user",
            effective_at_utc=NOW,
        )
        assert debit_id is not None
        clock.now = datetime(2026, 8, 5, 12, 1, tzinfo=UTC)
        supplement_id = quota.append_supplement(
            connection,
            referenced_debit_id=debit_id,
            pages=30,
            adjustment_source_namespace="meter_recheck",
            adjustment_source_id="sup-1",
            ownership=ownership(),
            calendar_lock=lock,
            now=clock.now,
        )
        clock.now = datetime(2026, 8, 5, 12, 2, tzinfo=UTC)
        quota.append_reversal(
            connection,
            referenced_debit_id=debit_id,
            pages=20,
            adjustment_source_namespace="billing",
            adjustment_source_id="rev-1",
            ownership=ownership(),
            calendar_lock=lock,
            now=clock.now,
        )
        clock.now = datetime(2026, 8, 5, 12, 3, tzinfo=UTC)
        quota.append_credit(
            connection,
            quota_subject_user_id="u1",
            quota_period="2026-08",
            pages=80,
            adjustment_source_namespace="quota_request",
            adjustment_source_id="req_1",
            ownership=ownership(),
            calendar_lock=lock,
            now=clock.now,
        )
        before = projection_row(connection)
        assert before is not None
        snapshot_before = quota.read_snapshot(connection, quota_subject_user_id="u1", role="user")
    with engine.begin() as connection:
        connection.execute(delete(quota_projection_table))
    with engine.begin() as connection:
        quota.rebuild_projection(connection, quota_subject_user_id="u1", quota_period="2026-08")
        after = projection_row(connection)
        assert after is not None
        snapshot_after = quota.read_snapshot(connection, quota_subject_user_id="u1", role="user")
    # 增量投影与 rebuild 完全一致（含 last_debit_id）
    assert before["used"] == after["used"] == 130
    assert before["extra_granted"] == after["extra_granted"] == 80
    assert before["last_debit_id"] == after["last_debit_id"] == supplement_id
    assert snapshot_before.used == snapshot_after.used == 130
    assert snapshot_before.extra_granted == snapshot_after.extra_granted == 80
    assert snapshot_before.effective_limit == snapshot_after.effective_limit == 580


def test_snapshot_new_period_baseline_with_old_projection() -> None:
    clock = MutableClock(NOW)
    engine, quota = make_quota(clock)
    with engine.begin() as connection:
        lock = quota.calendar.lock_or_verify(connection)
        quota.append_debit(
            connection,
            quota_operation_id="job_1",
            publication_id="pub_1",
            quota_subject_user_id="u1",
            pages=120,
            ownership=ownership(),
            calendar_lock=lock,
            role="user",
            effective_at_utc=NOW,
        )
        # 进入下一业务月：旧月投影存在，但新月读取返回基线且不建新月行
        clock.now = SEPT
        snapshot = quota.read_snapshot(connection, quota_subject_user_id="u1", role="user")
        assert snapshot.used == 0
        assert snapshot.extra_granted == 0
        assert snapshot.effective_limit == 500
        assert snapshot.quota_period == "2026-09"
        assert snapshot.reset_at == quota.calendar.next_month_start_utc(lock, SEPT)
        assert projection_row(connection, period="2026-09") is None
        assert projection_row(connection, period="2026-08")["used"] == 120  # 旧月投影保留


def test_rebuild_rejects_bad_period() -> None:
    """rebuild 的 quota_period 同样验证真实月份 01..12（review #7）。"""
    engine, quota = make_quota()
    with engine.begin() as connection:
        for bad_period in ("2026-00", "2026-13", "aug-2026"):
            with pytest.raises(PlatformError) as exc:
                quota.rebuild_projection(
                    connection, quota_subject_user_id="u1", quota_period=bad_period
                )
            assert exc.value.code == "validation_error"
            assert exc.value.status_code == 422
        assert connection.execute(select(quota_projection_table)).mappings().all() == []


def test_rebuild_locks_projection_before_reading_ledger(tmp_path) -> None:
    """rebuild 先锁投影再读 ledger：竞争 append 最终不丢增量（review #3，确定性编排）。

    SQLite 为单写者模型：一旦 append 事务执行过任何写语句（即使 calendar 的
    conflict no-op INSERT）即持有 RESERVED 锁，重建方会直接得到 "database is locked"，
    无法模拟 PG 的"append 已写 ledger 后在投影 FOR UPDATE 上等待"行锁序。本测试采用
    SQLite 下最接近的确定性调度：append 事务在**其首个写（INSERT INTO quota_debit）**
    执行前被 before_cursor_execute 阻塞且不持任何锁（calendar_lock 由主线程预先取得，
    append_debit 对其只做纯函数计算，不写 calendar 表）→ 重建方完成"锁投影 → 读
    ledger（未见 append 行）→ 覆盖旧总量并提交" → 释放 append → append 插入 ledger
    并原子增量叠加。最终投影 = 重建旧总量 + append 增量，无 lost update；事件顺序
    显式断言：重建投影写入发生在 append 账本插入之前（append 的 ledger 行对 rebuild
    的读取不可见，其增量仍在重建结果上叠加）。PG 行锁等待语义（append 在投影
    FOR UPDATE 上等待、rebuild 读不到未提交 append）留 Task 13 真实 PG 验证。
    """
    engine = create_engine(
        f"sqlite+pysqlite:///{tmp_path / 'rebuild_race.sqlite3'}",
        connect_args={"check_same_thread": False},
        poolclass=NullPool,
    )
    usage_metadata.create_all(engine)
    clock = MutableClock(NOW)
    calendar = BusinessCalendarService(engine, SqlAlchemyDatabaseClock(engine), "Asia/Shanghai")
    quota = QuotaService(engine, clock, calendar)
    # 先提交 120 页 debit（重建会看到的唯一账本行），并预先取得 calendar_lock
    with engine.begin() as connection:
        lock = quota.calendar.lock_or_verify(connection)
        quota.append_debit(
            connection,
            quota_operation_id="job_1",
            publication_id="pub_1",
            quota_subject_user_id="u1",
            pages=120,
            ownership=ownership(),
            calendar_lock=lock,
            role="user",
            effective_at_utc=NOW,
        )
    append_ready = threading.Event()
    append_released = threading.Event()
    append_errors: list[BaseException] = []
    append_results: list[bool] = []
    order: list[str] = []
    order_lock = threading.Lock()

    def record(marker: str) -> None:
        with order_lock:
            order.append(marker)

    def on_before_cursor_execute(conn, cursor, statement, parameters, context, executemany):
        del cursor, parameters, context, executemany
        if conn is not append_conn:
            return
        if "INSERT INTO quota_debit" in statement:
            record("append_ledger_insert_blocked")
            append_ready.set()
            append_released.wait(timeout=30)
            record("append_ledger_insert_released")

    event.listen(engine, "before_cursor_execute", on_before_cursor_execute)

    append_conn = engine.connect()
    append_tx = append_conn.begin()

    def append_worker():
        try:
            # 复用主线程取得的 calendar_lock（纯 dataclass），首个写即 ledger INSERT
            quota.append_debit(
                append_conn,
                quota_operation_id="job_2",
                publication_id="pub_2",
                quota_subject_user_id="u1",
                pages=30,
                ownership=ownership(),
                calendar_lock=lock,
                role="user",
                effective_at_utc=NOW,
            )
            append_tx.commit()
            record("append_commit")
            append_results.append(True)
        except BaseException as exc:  # pragma: no cover - failure path
            append_errors.append(exc)
            try:
                append_tx.rollback()
            except Exception:
                pass

    worker = threading.Thread(target=append_worker)
    worker.start()
    assert append_ready.wait(timeout=30), "append 未到达账本插入"
    # append 阻塞在首写前且不持锁；此时执行 rebuild（锁投影 → 读 ledger → 覆盖）
    with engine.begin() as rebuild_conn:
        quota.rebuild_projection(rebuild_conn, quota_subject_user_id="u1", quota_period="2026-08")
    record("rebuild_projection_update_done")
    append_released.set()
    worker.join(timeout=30)
    assert not worker.is_alive()
    append_conn.close()
    assert not append_errors, append_errors
    assert append_results == [True]
    # 事件顺序：重建投影写入先于 append 账本插入（append 增量随后叠加）
    assert order.index("rebuild_projection_update_done") < order.index(
        "append_ledger_insert_released"
    )
    # 最终投影 = 重建读到的旧总量(120) + append 增量(30)，无 lost update
    with engine.connect() as connection:
        row = projection_row(connection)
        assert row is not None
        assert row["used"] == 150
        assert row["last_debit_id"].startswith("qd_")
