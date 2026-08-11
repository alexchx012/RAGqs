"""QuotaService 页额度账本（debit/reversal/supplement/credit）追加语义测试（Task 7）。

语义（正式 spec §2/§3 + Task 7 约束，旧 brief 示例的陷阱已审计修正）：
- quota_debit 是独立、追加式的产品页额度账本。原始 debit 对非空 quota_operation_id
  条件唯一：同指纹重放复用 persisted ID（幂等，绝不二次更新投影），异指纹
  409 ledger_invariant_conflict（稳定 invariant，不让 raw IntegrityError 泄漏）。
- reversal/supplement/credit 以 (entry_kind, adjustment_source_namespace,
  adjustment_source_id) 唯一；同指纹重放复用、异指纹 409；幂等优先级先于语义校验
  （同来源键满额 reversal 的重放不得被累计校验误拒，见 Task 6 review R2 同款顺序）。
- reversal 只引用原始 debit，page_delta=-pages；累计反转
  abs(sum(page_delta)) + pages 不得超过原 debit（50+80>120 → 409），恰等（50+70=120）
  允许；累计校验前先 SELECT ... FOR UPDATE 锁被引用 debit 行（PG 下按引用行串行化）。
- supplement 只引用原始 debit，page_delta=+pages；credit page_delta=-pages、
  adjustment_source_namespace 固定 'quota_request'，投影追加 credit_delta=+pages。
- 豁免（quota_exempt_reason / unlimited role / replay_generation>0）不产生 debit。
- 投影更新：insert-do-nothing 建行 → SELECT ... FOR UPDATE → 原子表达式 UPDATE；
  last_debit_id 记录最近一个 debit-kind（debit/supplement）分录，reversal/credit 不改。
- 输入边界：pages 必须为正整数（拒绝 0/负数/bool）；文本字段非空且限长；
  credit 的 quota_period 必须是当前业务月（YYYY-MM 且等于日历推导期）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.engine import Connection
from sqlalchemy.pool import StaticPool

from app.platform.database import SqlAlchemyDatabaseClock
from app.platform.errors import PlatformError
from app.usage.calendar import BusinessCalendarService, CalendarLock
from app.usage.ledger import OwnershipSnapshot
from app.usage.quota import QuotaService
from app.usage.schema import quota_debit_table, quota_projection_table, usage_metadata

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


def ownership_other() -> OwnershipSnapshot:
    """同 subject、不同 actor 的 ownership：fingerprint 必须区分（review #1）。"""
    return OwnershipSnapshot(
        actor_user_id="u2",
        actor_role_snapshot="user",
        actor_department_id_snapshot=None,
        quota_subject_user_id="u1",
        cost_center_key="user:u1",
    )


def ownership_for(subject: str | None) -> OwnershipSnapshot:
    """指定 quota_subject_user_id 的 ownership（subject 一致性校验用，review #4）。"""
    return OwnershipSnapshot(
        actor_user_id="u1",
        actor_role_snapshot="user",
        actor_department_id_snapshot=None,
        quota_subject_user_id=subject,
        cost_center_key="user:u1",
    )


def bad_ownership() -> OwnershipSnapshot:
    return OwnershipSnapshot(
        actor_user_id="u1",
        actor_role_snapshot="user",
        actor_department_id_snapshot=None,
        quota_subject_user_id="u1",
        cost_center_key="",
    )


def ledger_rows(connection) -> list[dict]:
    return [
        dict(row)
        for row in connection.execute(
            select(quota_debit_table).order_by(
                quota_debit_table.c.created_at_utc, quota_debit_table.c.quota_debit_id
            )
        ).mappings()
    ]


def projection_row(connection, subject: str = "u1") -> dict | None:
    row = (
        connection.execute(
            select(quota_projection_table).where(
                quota_projection_table.c.quota_subject_user_id == subject
            )
        )
        .mappings()
        .one_or_none()
    )
    return dict(row) if row is not None else None


def test_debit_single_per_operation_and_projection_update() -> None:
    engine, quota = make_quota()
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
        replay = quota.append_debit(
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
        assert replay == debit_id
        assert len(ledger_rows(connection)) == 1  # 幂等重放不新增行
        row = projection_row(connection)
        assert row is not None
        assert row["used"] == 120
        assert row["last_debit_id"] == debit_id
        snapshot = quota.read_snapshot(connection, quota_subject_user_id="u1", role="user")
    assert snapshot.used == 120
    assert snapshot.effective_limit == 500


def test_exempt_unlimited_replay_produce_no_debit() -> None:
    engine, quota = make_quota()
    with engine.begin() as connection:
        lock = quota.calendar.lock_or_verify(connection)
        cases = [
            {"quota_exempt_reason": "shared_library_submission", "role": "user"},
            {"role": "ops"},
            {"role": "admin"},
            {"replay_generation": 1, "role": "user"},
        ]
        for i, case in enumerate(cases):
            result = quota.append_debit(
                connection,
                quota_operation_id=f"job_x{i}",
                publication_id="pub_x",
                quota_subject_user_id="u1",
                pages=100,
                ownership=ownership(),
                calendar_lock=lock,
                role=case["role"],
                effective_at_utc=NOW,
                quota_exempt_reason=case.get("quota_exempt_reason"),
                replay_generation=case.get("replay_generation", 0),
            )
            assert result is None
        rows = connection.execute(select(quota_debit_table)).mappings().all()
        assert rows == []
        assert projection_row(connection) is None  # 豁免不建投影


def test_debit_same_operation_different_facts_is_conflict() -> None:
    engine, quota = make_quota()
    with engine.begin() as connection:
        lock = quota.calendar.lock_or_verify(connection)
        quota.append_debit(
            connection,
            quota_operation_id="job_7",
            publication_id="pub_7",
            quota_subject_user_id="u1",
            pages=100,
            ownership=ownership(),
            calendar_lock=lock,
            role="user",
            effective_at_utc=NOW,
        )
        with pytest.raises(PlatformError) as exc:
            quota.append_debit(
                connection,
                quota_operation_id="job_7",
                publication_id="pub_7",
                quota_subject_user_id="u1",
                pages=200,
                ownership=ownership(),
                calendar_lock=lock,
                role="user",
                effective_at_utc=NOW,
            )
        assert exc.value.code == "ledger_invariant_conflict"
        snapshot = quota.read_snapshot(connection, quota_subject_user_id="u1", role="user")
        assert snapshot.used == 100
        assert len(ledger_rows(connection)) == 1


def test_reversal_validates_cumulative_and_reduces_used() -> None:
    engine, quota = make_quota()
    with engine.begin() as connection:
        lock = quota.calendar.lock_or_verify(connection)
        debit_id = quota.append_debit(
            connection,
            quota_operation_id="job_2",
            publication_id="pub_2",
            quota_subject_user_id="u1",
            pages=120,
            ownership=ownership(),
            calendar_lock=lock,
            role="user",
            effective_at_utc=NOW,
        )
        quota.append_reversal(
            connection,
            referenced_debit_id=debit_id,
            pages=50,
            adjustment_source_namespace="billing",
            adjustment_source_id="rev-1",
            ownership=ownership(),
            calendar_lock=lock,
            now=NOW,
        )
        # 累计反转 50+80=130 > 120 → 拒绝
        with pytest.raises(PlatformError) as exc:
            quota.append_reversal(
                connection,
                referenced_debit_id=debit_id,
                pages=80,
                adjustment_source_namespace="billing",
                adjustment_source_id="rev-2",
                ownership=ownership(),
                calendar_lock=lock,
                now=NOW,
            )
        assert exc.value.code == "ledger_invariant_conflict"
        snapshot = quota.read_snapshot(connection, quota_subject_user_id="u1", role="user")
    assert snapshot.used == 70  # 120 - 50


def test_reversal_cumulative_boundary_allows_exact_total() -> None:
    engine, quota = make_quota()
    with engine.begin() as connection:
        lock = quota.calendar.lock_or_verify(connection)
        debit_id = quota.append_debit(
            connection,
            quota_operation_id="job_4",
            publication_id="pub_4",
            quota_subject_user_id="u1",
            pages=120,
            ownership=ownership(),
            calendar_lock=lock,
            role="user",
            effective_at_utc=NOW,
        )
        quota.append_reversal(
            connection,
            referenced_debit_id=debit_id,
            pages=50,
            adjustment_source_namespace="billing",
            adjustment_source_id="rev-1",
            ownership=ownership(),
            calendar_lock=lock,
            now=NOW,
        )
        # 恰好等于原 debit（50+70=120）→ 允许
        quota.append_reversal(
            connection,
            referenced_debit_id=debit_id,
            pages=70,
            adjustment_source_namespace="billing",
            adjustment_source_id="rev-2",
            ownership=ownership(),
            calendar_lock=lock,
            now=NOW,
        )
        snapshot = quota.read_snapshot(connection, quota_subject_user_id="u1", role="user")
        assert snapshot.used == 0
        assert len(ledger_rows(connection)) == 3


def test_reversal_replay_reuses_entry_without_double_apply() -> None:
    engine, quota = make_quota()
    with engine.begin() as connection:
        lock = quota.calendar.lock_or_verify(connection)
        debit_id = quota.append_debit(
            connection,
            quota_operation_id="job_5",
            publication_id="pub_5",
            quota_subject_user_id="u1",
            pages=120,
            ownership=ownership(),
            calendar_lock=lock,
            role="user",
            effective_at_utc=NOW,
        )
        reversal_id = quota.append_reversal(
            connection,
            referenced_debit_id=debit_id,
            pages=50,
            adjustment_source_namespace="billing",
            adjustment_source_id="rev-1",
            ownership=ownership(),
            calendar_lock=lock,
            now=NOW,
        )
        replay_id = quota.append_reversal(
            connection,
            referenced_debit_id=debit_id,
            pages=50,
            adjustment_source_namespace="billing",
            adjustment_source_id="rev-1",
            ownership=ownership(),
            calendar_lock=lock,
            now=NOW,
        )
        assert replay_id == reversal_id
        snapshot = quota.read_snapshot(connection, quota_subject_user_id="u1", role="user")
        assert snapshot.used == 70  # 幂等重放不二次扣减
        assert len(ledger_rows(connection)) == 2


def test_reversal_replay_at_cumulative_boundary_is_not_rejected() -> None:
    """幂等优先级：同来源键满额 reversal 的重放复用既有行，不被累计校验误拒。"""
    engine, quota = make_quota()
    with engine.begin() as connection:
        lock = quota.calendar.lock_or_verify(connection)
        debit_id = quota.append_debit(
            connection,
            quota_operation_id="job_6",
            publication_id="pub_6",
            quota_subject_user_id="u1",
            pages=120,
            ownership=ownership(),
            calendar_lock=lock,
            role="user",
            effective_at_utc=NOW,
        )
        quota.append_reversal(
            connection,
            referenced_debit_id=debit_id,
            pages=50,
            adjustment_source_namespace="billing",
            adjustment_source_id="rev-1",
            ownership=ownership(),
            calendar_lock=lock,
            now=NOW,
        )
        rev2 = quota.append_reversal(
            connection,
            referenced_debit_id=debit_id,
            pages=70,
            adjustment_source_namespace="billing",
            adjustment_source_id="rev-2",
            ownership=ownership(),
            calendar_lock=lock,
            now=NOW,
        )
        # 已满额（50+70=120）后重放 rev-2 → 复用而非 409
        replay = quota.append_reversal(
            connection,
            referenced_debit_id=debit_id,
            pages=70,
            adjustment_source_namespace="billing",
            adjustment_source_id="rev-2",
            ownership=ownership(),
            calendar_lock=lock,
            now=NOW,
        )
        assert replay == rev2
        snapshot = quota.read_snapshot(connection, quota_subject_user_id="u1", role="user")
        assert snapshot.used == 0
        assert len(ledger_rows(connection)) == 3


def test_reversal_rejects_missing_and_non_debit_reference() -> None:
    engine, quota = make_quota()
    with engine.begin() as connection:
        lock = quota.calendar.lock_or_verify(connection)
        credit_id = quota.append_credit(
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
        with pytest.raises(PlatformError) as exc:
            quota.append_reversal(
                connection,
                referenced_debit_id="qd_missing",
                pages=10,
                adjustment_source_namespace="billing",
                adjustment_source_id="r1",
                ownership=ownership(),
                calendar_lock=lock,
                now=NOW,
            )
        assert exc.value.code == "quota_debit_not_found"
        # 引用 credit（非 debit）→ 同样拒绝
        with pytest.raises(PlatformError) as exc:
            quota.append_reversal(
                connection,
                referenced_debit_id=credit_id,
                pages=10,
                adjustment_source_namespace="billing",
                adjustment_source_id="r1",
                ownership=ownership(),
                calendar_lock=lock,
                now=NOW,
            )
        assert exc.value.code == "quota_debit_not_found"
        # supplement 引用 credit → 拒绝
        with pytest.raises(PlatformError) as exc:
            quota.append_supplement(
                connection,
                referenced_debit_id=credit_id,
                pages=10,
                adjustment_source_namespace="meter_recheck",
                adjustment_source_id="s1",
                ownership=ownership(),
                calendar_lock=lock,
                now=NOW,
            )
        assert exc.value.code == "quota_debit_not_found"
        # supplement 引用 reversal 行（非 debit）→ 拒绝
        debit_id = quota.append_debit(
            connection,
            quota_operation_id="job_9",
            publication_id="pub_9",
            quota_subject_user_id="u1",
            pages=100,
            ownership=ownership(),
            calendar_lock=lock,
            role="user",
            effective_at_utc=NOW,
        )
        reversal_id = quota.append_reversal(
            connection,
            referenced_debit_id=debit_id,
            pages=10,
            adjustment_source_namespace="billing",
            adjustment_source_id="r2",
            ownership=ownership(),
            calendar_lock=lock,
            now=NOW,
        )
        with pytest.raises(PlatformError) as exc:
            quota.append_supplement(
                connection,
                referenced_debit_id=reversal_id,
                pages=10,
                adjustment_source_namespace="meter_recheck",
                adjustment_source_id="s2",
                ownership=ownership(),
                calendar_lock=lock,
                now=NOW,
            )
        assert exc.value.code == "quota_debit_not_found"


def test_supplement_increases_used() -> None:
    engine, quota = make_quota()
    with engine.begin() as connection:
        lock = quota.calendar.lock_or_verify(connection)
        debit_id = quota.append_debit(
            connection,
            quota_operation_id="job_3",
            publication_id="pub_3",
            quota_subject_user_id="u1",
            pages=100,
            ownership=ownership(),
            calendar_lock=lock,
            role="user",
            effective_at_utc=NOW,
        )
        quota.append_supplement(
            connection,
            referenced_debit_id=debit_id,
            pages=30,
            adjustment_source_namespace="meter_recheck",
            adjustment_source_id="sup-1",
            ownership=ownership(),
            calendar_lock=lock,
            now=NOW,
        )
        snapshot = quota.read_snapshot(connection, quota_subject_user_id="u1", role="user")
    assert snapshot.used == 130


def test_supplement_replay_reuses_without_double_apply() -> None:
    engine, quota = make_quota()
    with engine.begin() as connection:
        lock = quota.calendar.lock_or_verify(connection)
        debit_id = quota.append_debit(
            connection,
            quota_operation_id="job_10",
            publication_id="pub_10",
            quota_subject_user_id="u1",
            pages=100,
            ownership=ownership(),
            calendar_lock=lock,
            role="user",
            effective_at_utc=NOW,
        )
        supplement_id = quota.append_supplement(
            connection,
            referenced_debit_id=debit_id,
            pages=30,
            adjustment_source_namespace="meter_recheck",
            adjustment_source_id="sup-1",
            ownership=ownership(),
            calendar_lock=lock,
            now=NOW,
        )
        replay_id = quota.append_supplement(
            connection,
            referenced_debit_id=debit_id,
            pages=30,
            adjustment_source_namespace="meter_recheck",
            adjustment_source_id="sup-1",
            ownership=ownership(),
            calendar_lock=lock,
            now=NOW,
        )
        assert replay_id == supplement_id
        snapshot = quota.read_snapshot(connection, quota_subject_user_id="u1", role="user")
        assert snapshot.used == 130  # 幂等重放不二次累加
        assert len(ledger_rows(connection)) == 2


def test_credit_adds_extra_granted_once() -> None:
    engine, quota = make_quota()
    with engine.begin() as connection:
        lock = quota.calendar.lock_or_verify(connection)
        credit_id = quota.append_credit(
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
        snapshot = quota.read_snapshot(connection, quota_subject_user_id="u1", role="user")
        assert snapshot.extra_granted == 80
        assert snapshot.effective_limit == 580
        # 同来源重复 credit → 幂等复用既有行（不二次追加、不抛错）
        replay_id = quota.append_credit(
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
        assert replay_id == credit_id
        snapshot = quota.read_snapshot(connection, quota_subject_user_id="u1", role="user")
        assert snapshot.extra_granted == 80
        assert snapshot.effective_limit == 580
        rows = ledger_rows(connection)
        assert len(rows) == 1
        assert rows[0]["entry_kind"] == "credit"
        assert rows[0]["page_delta"] == -80
        assert rows[0]["quota_period"] == "2026-08"
        assert rows[0]["effective_period"] == "2026-08"
        assert rows[0]["adjustment_source_namespace"] == "quota_request"


def test_credit_same_source_different_facts_is_conflict() -> None:
    engine, quota = make_quota()
    with engine.begin() as connection:
        lock = quota.calendar.lock_or_verify(connection)
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
        with pytest.raises(PlatformError) as exc:
            quota.append_credit(
                connection,
                quota_subject_user_id="u1",
                quota_period="2026-08",
                pages=100,
                adjustment_source_namespace="quota_request",
                adjustment_source_id="req_1",
                ownership=ownership(),
                calendar_lock=lock,
                now=NOW,
            )
        assert exc.value.code == "ledger_invariant_conflict"
        snapshot = quota.read_snapshot(connection, quota_subject_user_id="u1", role="user")
        assert snapshot.extra_granted == 80
        assert len(ledger_rows(connection)) == 1


def test_credit_rejects_wrong_period() -> None:
    engine, quota = make_quota()
    with engine.begin() as connection:
        lock = quota.calendar.lock_or_verify(connection)
        with pytest.raises(PlatformError) as exc:
            quota.append_credit(
                connection,
                quota_subject_user_id="u1",
                quota_period="2026-07",
                pages=10,
                adjustment_source_namespace="quota_request",
                adjustment_source_id="req_1",
                ownership=ownership(),
                calendar_lock=lock,
                now=NOW,
            )
        assert exc.value.code == "validation_error"
        with pytest.raises(PlatformError) as exc:
            quota.append_credit(
                connection,
                quota_subject_user_id="u1",
                quota_period="aug-2026",
                pages=10,
                adjustment_source_namespace="quota_request",
                adjustment_source_id="req_1",
                ownership=ownership(),
                calendar_lock=lock,
                now=NOW,
            )
        assert exc.value.code == "validation_error"
        assert connection.execute(select(quota_debit_table)).mappings().all() == []


def test_input_boundaries_validation() -> None:
    engine, quota = make_quota()
    with engine.begin() as connection:
        lock = quota.calendar.lock_or_verify(connection)

        def expect_422(call) -> None:
            with pytest.raises(PlatformError) as exc:
                call()
            assert exc.value.code == "validation_error"
            assert exc.value.status_code == 422

        # debit：pages 边界（0 / 负数 / bool）
        expect_422(
            lambda: quota.append_debit(
                connection,
                quota_operation_id="job_a",
                publication_id="pub",
                pages=0,
                quota_subject_user_id="u1",
                ownership=ownership(),
                calendar_lock=lock,
                role="user",
                effective_at_utc=NOW,
            )
        )
        expect_422(
            lambda: quota.append_debit(
                connection,
                quota_operation_id="job_a",
                publication_id="pub",
                pages=-5,
                quota_subject_user_id="u1",
                ownership=ownership(),
                calendar_lock=lock,
                role="user",
                effective_at_utc=NOW,
            )
        )
        expect_422(
            lambda: quota.append_debit(
                connection,
                quota_operation_id="job_a",
                publication_id="pub",
                pages=True,
                quota_subject_user_id="u1",
                ownership=ownership(),
                calendar_lock=lock,
                role="user",
                effective_at_utc=NOW,
            )
        )
        # debit：文本非空
        expect_422(
            lambda: quota.append_debit(
                connection,
                quota_operation_id="",
                publication_id="pub",
                pages=10,
                quota_subject_user_id="u1",
                ownership=ownership(),
                calendar_lock=lock,
                role="user",
                effective_at_utc=NOW,
            )
        )
        expect_422(
            lambda: quota.append_debit(
                connection,
                quota_operation_id="job_a",
                publication_id="",
                pages=10,
                quota_subject_user_id="u1",
                ownership=ownership(),
                calendar_lock=lock,
                role="user",
                effective_at_utc=NOW,
            )
        )
        expect_422(
            lambda: quota.append_debit(
                connection,
                quota_operation_id="job_a",
                publication_id="pub",
                pages=10,
                quota_subject_user_id="",
                ownership=ownership(),
                calendar_lock=lock,
                role="user",
                effective_at_utc=NOW,
            )
        )
        # ownership：cost_center_key 非空
        expect_422(
            lambda: quota.append_debit(
                connection,
                quota_operation_id="job_a",
                publication_id="pub",
                pages=10,
                quota_subject_user_id="u1",
                ownership=bad_ownership(),
                calendar_lock=lock,
                role="user",
                effective_at_utc=NOW,
            )
        )
        # reversal：pages / source 边界
        expect_422(
            lambda: quota.append_reversal(
                connection,
                referenced_debit_id="qd_x",
                pages=0,
                adjustment_source_namespace="billing",
                adjustment_source_id="r1",
                ownership=ownership(),
                calendar_lock=lock,
                now=NOW,
            )
        )
        expect_422(
            lambda: quota.append_reversal(
                connection,
                referenced_debit_id="qd_x",
                pages=10,
                adjustment_source_namespace="",
                adjustment_source_id="r1",
                ownership=ownership(),
                calendar_lock=lock,
                now=NOW,
            )
        )
        expect_422(
            lambda: quota.append_reversal(
                connection,
                referenced_debit_id="qd_x",
                pages=10,
                adjustment_source_namespace="billing",
                adjustment_source_id="",
                ownership=ownership(),
                calendar_lock=lock,
                now=NOW,
            )
        )
        expect_422(
            lambda: quota.append_reversal(
                connection,
                referenced_debit_id="",
                pages=10,
                adjustment_source_namespace="billing",
                adjustment_source_id="r1",
                ownership=ownership(),
                calendar_lock=lock,
                now=NOW,
            )
        )
        # supplement：pages 边界
        expect_422(
            lambda: quota.append_supplement(
                connection,
                referenced_debit_id="qd_x",
                pages=-1,
                adjustment_source_namespace="meter_recheck",
                adjustment_source_id="s1",
                ownership=ownership(),
                calendar_lock=lock,
                now=NOW,
            )
        )
        # credit：pages / period / source 边界
        expect_422(
            lambda: quota.append_credit(
                connection,
                quota_subject_user_id="u1",
                quota_period="2026-08",
                pages=True,
                adjustment_source_namespace="quota_request",
                adjustment_source_id="c1",
                ownership=ownership(),
                calendar_lock=lock,
                now=NOW,
            )
        )
        expect_422(
            lambda: quota.append_credit(
                connection,
                quota_subject_user_id="u1",
                quota_period="2026-08",
                pages=10,
                adjustment_source_namespace="",
                adjustment_source_id="c1",
                ownership=ownership(),
                calendar_lock=lock,
                now=NOW,
            )
        )
        expect_422(
            lambda: quota.append_credit(
                connection,
                quota_subject_user_id="u1",
                quota_period="2026-08",
                pages=10,
                adjustment_source_namespace="quota_request",
                adjustment_source_id="",
                ownership=ownership(),
                calendar_lock=lock,
                now=NOW,
            )
        )
        # 门禁：pages 边界
        expect_422(
            lambda: quota.check_direct_ingest_balance(
                connection, quota_subject_user_id="u1", pages=0, role="user"
            )
        )
        # 全部校验失败路径不产生任何账本/投影行
        assert connection.execute(select(quota_debit_table)).mappings().all() == []
        assert connection.execute(select(quota_projection_table)).mappings().all() == []


def test_last_debit_id_tracks_debit_kind_entries() -> None:
    engine, quota = make_quota()
    with engine.begin() as connection:
        lock = quota.calendar.lock_or_verify(connection)
        debit_id = quota.append_debit(
            connection,
            quota_operation_id="job_11",
            publication_id="pub_11",
            quota_subject_user_id="u1",
            pages=120,
            ownership=ownership(),
            calendar_lock=lock,
            role="user",
            effective_at_utc=NOW,
        )
        assert projection_row(connection)["last_debit_id"] == debit_id
        supplement_id = quota.append_supplement(
            connection,
            referenced_debit_id=debit_id,
            pages=30,
            adjustment_source_namespace="meter_recheck",
            adjustment_source_id="sup-1",
            ownership=ownership(),
            calendar_lock=lock,
            now=NOW,
        )
        assert projection_row(connection)["last_debit_id"] == supplement_id
        quota.append_reversal(
            connection,
            referenced_debit_id=debit_id,
            pages=20,
            adjustment_source_namespace="billing",
            adjustment_source_id="rev-1",
            ownership=ownership(),
            calendar_lock=lock,
            now=NOW,
        )
        assert projection_row(connection)["last_debit_id"] == supplement_id  # reversal 不改
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
        assert projection_row(connection)["last_debit_id"] == supplement_id  # credit 不改


def test_reversal_supplement_inherit_referenced_facts() -> None:
    engine, quota = make_quota()
    with engine.begin() as connection:
        lock = quota.calendar.lock_or_verify(connection)
        debit_id = quota.append_debit(
            connection,
            quota_operation_id="job_8",
            publication_id="pub_8",
            quota_subject_user_id="u1",
            pages=120,
            ownership=ownership(),
            calendar_lock=lock,
            role="user",
            effective_at_utc=NOW,
        )
        quota.append_reversal(
            connection,
            referenced_debit_id=debit_id,
            pages=20,
            adjustment_source_namespace="billing",
            adjustment_source_id="rev-1",
            ownership=ownership(),
            calendar_lock=lock,
            now=NOW,
        )
        quota.append_supplement(
            connection,
            referenced_debit_id=debit_id,
            pages=30,
            adjustment_source_namespace="meter_recheck",
            adjustment_source_id="sup-1",
            ownership=ownership(),
            calendar_lock=lock,
            now=NOW,
        )
        by_kind = {row["entry_kind"]: row for row in ledger_rows(connection)}
        for kind in ("debit", "reversal", "supplement"):
            row = by_kind[kind]
            assert row["quota_subject_user_id"] == "u1"
            assert row["quota_period"] == "2026-08"
            assert row["effective_period"] == "2026-08"
            assert row["recorded_period"] == "2026-08"
            assert row["cost_center_key"] == "user:u1"
            assert row["ownership_json"] == {
                "actor_user_id": "u1",
                "actor_role_snapshot": "user",
                "actor_department_id_snapshot": None,
                "quota_subject_user_id": "u1",
                "cost_center_key": "user:u1",
                "space_id": None,
                "space_kind": None,
                "space_owner_user_id": None,
                "authorization_version": None,
                "fence_token": None,
            }
        # reversal/supplement 继承原 debit 的 effective 事实（日历版本与 effective_at）
        assert by_kind["reversal"]["effective_at_utc"] == by_kind["debit"]["effective_at_utc"]
        assert by_kind["supplement"]["effective_at_utc"] == by_kind["debit"]["effective_at_utc"]
        assert (
            by_kind["reversal"]["effective_calendar_version_id"]
            == by_kind["debit"]["effective_calendar_version_id"]
        )
        assert (
            by_kind["supplement"]["effective_calendar_version_id"]
            == by_kind["debit"]["effective_calendar_version_id"]
        )
        # 符号与来源
        assert by_kind["debit"]["page_delta"] == 120
        assert by_kind["reversal"]["page_delta"] == -20
        assert by_kind["supplement"]["page_delta"] == 30
        assert by_kind["reversal"]["referenced_debit_id"] == debit_id
        assert by_kind["supplement"]["referenced_debit_id"] == debit_id
        assert by_kind["reversal"]["adjustment_source_namespace"] == "billing"
        assert by_kind["supplement"]["adjustment_source_namespace"] == "meter_recheck"


def test_check_direct_ingest_balance_gate() -> None:
    engine, quota = make_quota()
    with engine.begin() as connection:
        lock = quota.calendar.lock_or_verify(connection)
        # unlimited 角色放行，即使页数远超限额
        quota.check_direct_ingest_balance(
            connection, quota_subject_user_id="u1", pages=10**6, role="ops"
        )
        quota.check_direct_ingest_balance(
            connection, quota_subject_user_id="u1", pages=10**6, role="admin"
        )
        # 有限角色：未超限通过；超限 409 quota_exceeded
        quota.check_direct_ingest_balance(
            connection, quota_subject_user_id="u1", pages=200, role="user"
        )
        with pytest.raises(PlatformError) as exc:
            quota.check_direct_ingest_balance(
                connection, quota_subject_user_id="u1", pages=600, role="user"
            )
        assert exc.value.code == "quota_exceeded"
        # 门禁不建行
        assert connection.execute(select(quota_projection_table)).mappings().all() == []
        # 已有 debit 后按新投影判定（300+200==500 通过；300+201>500 拒绝）
        quota.append_debit(
            connection,
            quota_operation_id="job_12",
            publication_id="pub_12",
            quota_subject_user_id="u1",
            pages=300,
            ownership=ownership(),
            calendar_lock=lock,
            role="user",
            effective_at_utc=NOW,
        )
        quota.check_direct_ingest_balance(
            connection, quota_subject_user_id="u1", pages=200, role="user"
        )
        with pytest.raises(PlatformError) as exc:
            quota.check_direct_ingest_balance(
                connection, quota_subject_user_id="u1", pages=201, role="user"
            )
        assert exc.value.code == "quota_exceeded"


def test_debit_fingerprint_covers_subject_and_effective_facts() -> None:
    """debit fingerprint 含 subject/effective_at/effective_period/calendar（review #1）。"""
    engine, quota = make_quota()
    with engine.begin() as connection:
        lock = quota.calendar.lock_or_verify(connection)
        synthetic = CalendarLock(
            version_id="cal_synthetic", timezone="Asia/Shanghai", effective_from_utc=NOW
        )
        debit_id = quota.append_debit(
            connection,
            quota_operation_id="job_fp",
            publication_id="pub_fp",
            quota_subject_user_id="u1",
            pages=100,
            ownership=ownership(),
            calendar_lock=lock,
            role="user",
            effective_at_utc=NOW,
        )
        assert debit_id is not None
        # 同事实重放 → 复用
        replay = quota.append_debit(
            connection,
            quota_operation_id="job_fp",
            publication_id="pub_fp",
            quota_subject_user_id="u1",
            pages=100,
            ownership=ownership(),
            calendar_lock=lock,
            role="user",
            effective_at_utc=NOW,
        )
        assert replay == debit_id
        # subject 变化 → 409（subject 进 fingerprint）
        with pytest.raises(PlatformError) as exc:
            quota.append_debit(
                connection,
                quota_operation_id="job_fp",
                publication_id="pub_fp",
                quota_subject_user_id="u2",
                pages=100,
                ownership=ownership_for("u2"),
                calendar_lock=lock,
                role="user",
                effective_at_utc=NOW,
            )
        assert exc.value.code == "ledger_invariant_conflict"
        # effective_at 变化 → 409（effective_at 进 fingerprint）
        with pytest.raises(PlatformError) as exc:
            quota.append_debit(
                connection,
                quota_operation_id="job_fp",
                publication_id="pub_fp",
                quota_subject_user_id="u1",
                pages=100,
                ownership=ownership(),
                calendar_lock=lock,
                role="user",
                effective_at_utc=NOW + timedelta(days=1),
            )
        assert exc.value.code == "ledger_invariant_conflict"
        # effective_calendar_version_id 变化（合成 lock）→ 409
        with pytest.raises(PlatformError) as exc:
            quota.append_debit(
                connection,
                quota_operation_id="job_fp",
                publication_id="pub_fp",
                quota_subject_user_id="u1",
                pages=100,
                ownership=ownership(),
                calendar_lock=synthetic,
                role="user",
                effective_at_utc=NOW,
            )
        assert exc.value.code == "ledger_invariant_conflict"
        # 全部 409 路径不新增行
        assert len(ledger_rows(connection)) == 1
        snapshot = quota.read_snapshot(connection, quota_subject_user_id="u1", role="user")
        assert snapshot.used == 100


def test_reversal_fingerprint_includes_ownership() -> None:
    engine, quota = make_quota()
    with engine.begin() as connection:
        lock = quota.calendar.lock_or_verify(connection)
        debit_id = quota.append_debit(
            connection,
            quota_operation_id="job_fpr",
            publication_id="pub_fpr",
            quota_subject_user_id="u1",
            pages=120,
            ownership=ownership(),
            calendar_lock=lock,
            role="user",
            effective_at_utc=NOW,
        )
        assert debit_id is not None
        reversal_id = quota.append_reversal(
            connection,
            referenced_debit_id=debit_id,
            pages=50,
            adjustment_source_namespace="billing",
            adjustment_source_id="rev-1",
            ownership=ownership(),
            calendar_lock=lock,
            now=NOW,
        )
        # 同事实重放 → 复用
        replay = quota.append_reversal(
            connection,
            referenced_debit_id=debit_id,
            pages=50,
            adjustment_source_namespace="billing",
            adjustment_source_id="rev-1",
            ownership=ownership(),
            calendar_lock=lock,
            now=NOW,
        )
        assert replay == reversal_id
        # 同来源键、ownership 变化（同 subject 不同 actor）→ 409
        with pytest.raises(PlatformError) as exc:
            quota.append_reversal(
                connection,
                referenced_debit_id=debit_id,
                pages=50,
                adjustment_source_namespace="billing",
                adjustment_source_id="rev-1",
                ownership=ownership_other(),
                calendar_lock=lock,
                now=NOW,
            )
        assert exc.value.code == "ledger_invariant_conflict"
        snapshot = quota.read_snapshot(connection, quota_subject_user_id="u1", role="user")
        assert snapshot.used == 70  # 幂等，不二次扣减
        assert len(ledger_rows(connection)) == 2


def test_supplement_fingerprint_includes_ownership() -> None:
    engine, quota = make_quota()
    with engine.begin() as connection:
        lock = quota.calendar.lock_or_verify(connection)
        debit_id = quota.append_debit(
            connection,
            quota_operation_id="job_fps",
            publication_id="pub_fps",
            quota_subject_user_id="u1",
            pages=100,
            ownership=ownership(),
            calendar_lock=lock,
            role="user",
            effective_at_utc=NOW,
        )
        assert debit_id is not None
        supplement_id = quota.append_supplement(
            connection,
            referenced_debit_id=debit_id,
            pages=30,
            adjustment_source_namespace="meter_recheck",
            adjustment_source_id="sup-1",
            ownership=ownership(),
            calendar_lock=lock,
            now=NOW,
        )
        replay = quota.append_supplement(
            connection,
            referenced_debit_id=debit_id,
            pages=30,
            adjustment_source_namespace="meter_recheck",
            adjustment_source_id="sup-1",
            ownership=ownership(),
            calendar_lock=lock,
            now=NOW,
        )
        assert replay == supplement_id
        # 同来源键、ownership 变化 → 409
        with pytest.raises(PlatformError) as exc:
            quota.append_supplement(
                connection,
                referenced_debit_id=debit_id,
                pages=30,
                adjustment_source_namespace="meter_recheck",
                adjustment_source_id="sup-1",
                ownership=ownership_other(),
                calendar_lock=lock,
                now=NOW,
            )
        assert exc.value.code == "ledger_invariant_conflict"
        snapshot = quota.read_snapshot(connection, quota_subject_user_id="u1", role="user")
        assert snapshot.used == 130  # 幂等，不二次累加
        assert len(ledger_rows(connection)) == 2


def test_credit_fingerprint_includes_ownership() -> None:
    engine, quota = make_quota()
    with engine.begin() as connection:
        lock = quota.calendar.lock_or_verify(connection)
        credit_id = quota.append_credit(
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
        replay = quota.append_credit(
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
        assert replay == credit_id
        # 同来源键、ownership 变化 → 409
        with pytest.raises(PlatformError) as exc:
            quota.append_credit(
                connection,
                quota_subject_user_id="u1",
                quota_period="2026-08",
                pages=80,
                adjustment_source_namespace="quota_request",
                adjustment_source_id="req_1",
                ownership=ownership_other(),
                calendar_lock=lock,
                now=NOW,
            )
        assert exc.value.code == "ledger_invariant_conflict"
        snapshot = quota.read_snapshot(connection, quota_subject_user_id="u1", role="user")
        assert snapshot.extra_granted == 80
        assert len(ledger_rows(connection)) == 1


def test_ownership_subject_must_match_entry_subject() -> None:
    """任何 entry 的 ownership.quota_subject_user_id 必须与目标 subject 一致（review #4）。"""
    engine, quota = make_quota()
    with engine.begin() as connection:
        lock = quota.calendar.lock_or_verify(connection)

        def expect_422(call) -> None:
            with pytest.raises(PlatformError) as exc:
                call()
            assert exc.value.code == "validation_error"
            assert exc.value.status_code == 422

        # debit：显式 subject 与 ownership.quota_subject_user_id 不一致 → 422
        expect_422(
            lambda: quota.append_debit(
                connection,
                quota_operation_id="job_s1",
                publication_id="pub",
                quota_subject_user_id="u1",
                pages=10,
                ownership=ownership_for("other"),
                calendar_lock=lock,
                role="user",
                effective_at_utc=NOW,
            )
        )
        expect_422(
            lambda: quota.append_debit(
                connection,
                quota_operation_id="job_s2",
                publication_id="pub",
                quota_subject_user_id="u1",
                pages=10,
                ownership=ownership_for(None),
                calendar_lock=lock,
                role="user",
                effective_at_utc=NOW,
            )
        )
        # credit：显式 subject 不一致 → 422
        expect_422(
            lambda: quota.append_credit(
                connection,
                quota_subject_user_id="u1",
                quota_period="2026-08",
                pages=10,
                adjustment_source_namespace="quota_request",
                adjustment_source_id="req_s",
                ownership=ownership_for("other"),
                calendar_lock=lock,
                now=NOW,
            )
        )
        # 合法 debit 建立引用基础
        debit_id = quota.append_debit(
            connection,
            quota_operation_id="job_s3",
            publication_id="pub",
            quota_subject_user_id="u1",
            pages=100,
            ownership=ownership(),
            calendar_lock=lock,
            role="user",
            effective_at_utc=NOW,
        )
        assert debit_id is not None
        # reversal/supplement：与被引用 debit subject 不一致 → 422
        expect_422(
            lambda: quota.append_reversal(
                connection,
                referenced_debit_id=debit_id,
                pages=10,
                adjustment_source_namespace="billing",
                adjustment_source_id="rev_s",
                ownership=ownership_for("other"),
                calendar_lock=lock,
                now=NOW,
            )
        )
        expect_422(
            lambda: quota.append_supplement(
                connection,
                referenced_debit_id=debit_id,
                pages=10,
                adjustment_source_namespace="meter_recheck",
                adjustment_source_id="sup_s",
                ownership=ownership_for("other"),
                calendar_lock=lock,
                now=NOW,
            )
        )
        # 全部 422 不产生额外 entry；投影只含合法 debit
        assert len(ledger_rows(connection)) == 1
        row = projection_row(connection)
        assert row is not None
        assert row["used"] == 100


def test_credit_cross_month_idempotency_priority() -> None:
    """credit 跨月：同指纹复用、异指纹 409、首次错误月份 422（review #2）。"""
    clock = MutableClock(NOW)
    engine, quota = make_quota(clock)
    with engine.begin() as connection:
        lock = quota.calendar.lock_or_verify(connection)
        credit_id = quota.append_credit(
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
        assert credit_id is not None
        # 进入下一业务月：同事实重放 → 复用（当前月校验只在首次插入路径）
        clock.now = SEPT
        replay = quota.append_credit(
            connection,
            quota_subject_user_id="u1",
            quota_period="2026-08",
            pages=80,
            adjustment_source_namespace="quota_request",
            adjustment_source_id="req_1",
            ownership=ownership(),
            calendar_lock=lock,
            now=SEPT,
        )
        assert replay == credit_id
        assert len(ledger_rows(connection)) == 1
        # 同来源键、异事实（pages 不同）→ 409（而非 422 当前月校验）
        with pytest.raises(PlatformError) as exc:
            quota.append_credit(
                connection,
                quota_subject_user_id="u1",
                quota_period="2026-08",
                pages=100,
                adjustment_source_namespace="quota_request",
                adjustment_source_id="req_1",
                ownership=ownership(),
                calendar_lock=lock,
                now=SEPT,
            )
        assert exc.value.code == "ledger_invariant_conflict"
        # 首次插入、错误月份 → 422
        with pytest.raises(PlatformError) as exc:
            quota.append_credit(
                connection,
                quota_subject_user_id="u1",
                quota_period="2026-08",
                pages=40,
                adjustment_source_namespace="quota_request",
                adjustment_source_id="req_2",
                ownership=ownership(),
                calendar_lock=lock,
                now=SEPT,
            )
        assert exc.value.code == "validation_error"
        assert len(ledger_rows(connection)) == 1


def test_credit_namespace_must_be_quota_request() -> None:
    """credit 的 adjustment_source_namespace 服务层固定为 quota_request（review #6）。"""
    engine, quota = make_quota()
    with engine.begin() as connection:
        lock = quota.calendar.lock_or_verify(connection)
        with pytest.raises(PlatformError) as exc:
            quota.append_credit(
                connection,
                quota_subject_user_id="u1",
                quota_period="2026-08",
                pages=10,
                adjustment_source_namespace="billing",
                adjustment_source_id="req_1",
                ownership=ownership(),
                calendar_lock=lock,
                now=NOW,
            )
        assert exc.value.code == "validation_error"
        assert exc.value.status_code == 422
        assert connection.execute(select(quota_debit_table)).mappings().all() == []


def test_replay_generation_strict_validation() -> None:
    """replay_generation 严格非 bool 非负整数（review #5）。"""
    engine, quota = make_quota()
    with engine.begin() as connection:
        lock = quota.calendar.lock_or_verify(connection)

        def expect_422(replay_generation) -> None:
            with pytest.raises(PlatformError) as exc:
                quota.append_debit(
                    connection,
                    quota_operation_id=f"job_rg{replay_generation!r}",
                    publication_id="pub",
                    quota_subject_user_id="u1",
                    pages=10,
                    ownership=ownership(),
                    calendar_lock=lock,
                    role="user",
                    effective_at_utc=NOW,
                    replay_generation=replay_generation,
                )
            assert exc.value.code == "validation_error"

        expect_422(True)
        expect_422(False)
        expect_422(-1)
        # 合法：1 → 豁免（None，不建行）；0 → 正常 debit
        assert (
            quota.append_debit(
                connection,
                quota_operation_id="job_rg1",
                publication_id="pub",
                quota_subject_user_id="u1",
                pages=10,
                ownership=ownership(),
                calendar_lock=lock,
                role="user",
                effective_at_utc=NOW,
                replay_generation=1,
            )
            is None
        )
        debit_id = quota.append_debit(
            connection,
            quota_operation_id="job_rg0",
            publication_id="pub",
            quota_subject_user_id="u1",
            pages=10,
            ownership=ownership(),
            calendar_lock=lock,
            role="user",
            effective_at_utc=NOW,
            replay_generation=0,
        )
        assert debit_id is not None
        assert len(ledger_rows(connection)) == 1


def test_period_validation_rejects_bad_months() -> None:
    """_require_period 验证真实月份 01..12，不放行 00/13（review #7）。"""
    engine, quota = make_quota()
    with engine.begin() as connection:
        lock = quota.calendar.lock_or_verify(connection)
        for bad_period in ("2026-00", "2026-13"):
            with pytest.raises(PlatformError) as exc:
                quota.append_credit(
                    connection,
                    quota_subject_user_id="u1",
                    quota_period=bad_period,
                    pages=10,
                    adjustment_source_namespace="quota_request",
                    adjustment_source_id=f"req_{bad_period}",
                    ownership=ownership(),
                    calendar_lock=lock,
                    now=NOW,
                )
            assert exc.value.code == "validation_error"
            assert exc.value.status_code == 422
        assert connection.execute(select(quota_debit_table)).mappings().all() == []


def test_reversal_supplement_inherit_effective_facts_with_synthetic_lock() -> None:
    """reversal/supplement 直接继承被引用 debit 的 effective facts（review #4）。

    用合成 CalendarLock（不同 version_id + 不同时区）验证：effective 不按调用方
    lock 重算（否则 8/31 15:30 UTC 在 UTC+14 会得到 2026-09）；recorded_* 才用
    当前 lock + now。
    """
    engine, quota = make_quota()
    synthetic = CalendarLock(
        version_id="cal_synthetic", timezone="Pacific/Kiritimati", effective_from_utc=NOW
    )
    with engine.begin() as connection:
        real_lock = quota.calendar.lock_or_verify(connection)
        edge = datetime(2026, 8, 31, 15, 30, tzinfo=UTC)
        debit_id = quota.append_debit(
            connection,
            quota_operation_id="job_edge",
            publication_id="pub_edge",
            quota_subject_user_id="u1",
            pages=100,
            ownership=ownership(),
            calendar_lock=real_lock,
            role="user",
            effective_at_utc=edge,
        )
        assert debit_id is not None
        debit_row = dict(
            connection.execute(
                select(quota_debit_table).where(quota_debit_table.c.quota_debit_id == debit_id)
            )
            .mappings()
            .one()
        )
        assert debit_row["effective_period"] == "2026-08"  # Asia/Shanghai 8/31 23:30
        now = datetime(2026, 8, 31, 16, 0, tzinfo=UTC)
        rev_id = quota.append_reversal(
            connection,
            referenced_debit_id=debit_id,
            pages=10,
            adjustment_source_namespace="billing",
            adjustment_source_id="rev-edge",
            ownership=ownership(),
            calendar_lock=synthetic,
            now=now,
        )
        sup_id = quota.append_supplement(
            connection,
            referenced_debit_id=debit_id,
            pages=20,
            adjustment_source_namespace="meter_recheck",
            adjustment_source_id="sup-edge",
            ownership=ownership(),
            calendar_lock=synthetic,
            now=now,
        )
        for entry_id in (rev_id, sup_id):
            row = dict(
                connection.execute(
                    select(quota_debit_table).where(quota_debit_table.c.quota_debit_id == entry_id)
                )
                .mappings()
                .one()
            )
            # effective 直接继承被引用 debit（合成 lock 重算会得到 2026-09 → 冲突）
            assert (
                row["effective_calendar_version_id"] == debit_row["effective_calendar_version_id"]
            )
            assert row["effective_at_utc"] == debit_row["effective_at_utc"]
            assert row["effective_period"] == "2026-08"
            # recorded_* 用当前 calendar_lock + now（UTC+14：8/31 16:00 UTC = 9/1）
            assert row["recorded_calendar_version_id"] == "cal_synthetic"
            assert row["recorded_period"] == "2026-09"
