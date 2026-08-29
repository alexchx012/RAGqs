"""Task 13: 并发/幂等/竞争路径的 SQLite **串行** 验收。

正式 spec §6 要求：业务月锁、月界、并发扣减、request/approval 的幂等与竞争、
credit 唯一性、账本投影可重建、事务原子性。SQLite 内存引擎（StaticPool 单连接）
**只能串行执行**：本文件只证明可串行验证的契约，明确以 ``serial`` 命名，绝不声称
证明并发（真实双连接 PG 并发由 ``tests/usage/test_concurrency_postgres.py`` 承担，
缺 ``RAGQS_TEST_POSTGRES_URL``/``RAGQS_ALLOW_DESTRUCTIVE_POSTGRES_TESTS=1`` 时整
组 skip 并记为 NOT RUN/BLOCKED）。

与 plan/brief 的差异（真实生产 API 钉住）：
- ``QuotaService._existing_entry`` 的幂等回读先于唯一约束：同指纹 credit 重放
  复用同一行（不抛 IntegrityError），异指纹同 source 409 ledger_invariant_conflict；
  数据库唯一约束 ``uq_quota_debit_adjustment`` 是并发双事务的最终防线，本文件
  以绕过服务层的原始 INSERT 证明该约束真实拒绝第二行。
- 终态 request 测试用**真实 reject transition**（``requests.reject``），不再用 raw
  SQL 改 status；同月 rejected 后重申 + 下一业务月重申各一个测试，名称与行为一致。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.engine import Connection
from sqlalchemy.exc import IntegrityError
from sqlalchemy.pool import StaticPool

from app.identity.schema import identity_metadata, identity_user_table
from app.identity.service import AuthPrincipal
from app.outbox.schema import outbox_metadata
from app.platform.database import SqlAlchemyDatabaseClock, core_metadata
from app.platform.errors import PlatformError
from app.usage.calendar import BusinessCalendarService
from app.usage.ledger import OwnershipSnapshot
from app.usage.ports import NoopOutboxEnqueuePort
from app.usage.quota import QuotaService
from app.usage.requests import QuotaRequestService
from app.usage.schema import quota_debit_table, quota_request_table, usage_metadata

NOW = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)
# Asia/Shanghai（UTC+8，无 DST）：2026-09-02 08:00 +08 = 2026-09-02 00:00 UTC。
SEPT = datetime(2026, 9, 2, 0, 0, tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class MutableClock:
    """H4：可推进时钟——共享同一个 times 列表，推进 ``times[0]`` 即可推进 now。"""

    times: list[datetime]

    def now_utc(self, connection: Connection | None = None) -> datetime:
        del connection
        return self.times[0]


@pytest.fixture()
def env():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    core_metadata.create_all(engine)
    identity_metadata.create_all(engine)
    outbox_metadata.create_all(engine)
    usage_metadata.create_all(engine)
    times = [NOW]
    clock = MutableClock(times)
    calendar = BusinessCalendarService(engine, SqlAlchemyDatabaseClock(engine), "Asia/Shanghai")
    quota = QuotaService(engine, clock, calendar)
    requests = QuotaRequestService(engine, clock, calendar, quota, NoopOutboxEnqueuePort())
    try:
        yield engine, quota, requests, times
    finally:
        engine.dispose()


def ownership() -> OwnershipSnapshot:
    return OwnershipSnapshot(
        actor_user_id="u1",
        actor_role_snapshot="user",
        actor_department_id_snapshot=None,
        quota_subject_user_id="u1",
        cost_center_key="user:u1",
    )


def applicant(uid: str = "u1") -> AuthPrincipal:
    return AuthPrincipal(
        user_id=uid, auth_session_id="s1", username="alice", role="user", department_id=None
    )


def approver() -> AuthPrincipal:
    return AuthPrincipal(
        user_id="ops1", auth_session_id="s2", username="op", role="ops", department_id=None
    )


def seed_identity(engine) -> None:
    with engine.begin() as connection:
        connection.execute(
            identity_user_table.insert().values(
                id="u1",
                username="alice",
                normalized_username="alice",
                password_hash="x",
                real_name="Alice",
                display_name="Alice",
                department_id=None,
                role="user",
                lifecycle_status="active",
                version=1,
                preferences_json={},
                transition_version=1,
                created_at_utc=NOW,
                updated_at_utc=NOW,
            )
        )


def _credit_rows(engine) -> list[dict]:
    with engine.connect() as connection:
        rows = (
            connection.execute(
                select(quota_debit_table).where(quota_debit_table.c.entry_kind == "credit")
            )
            .mappings()
            .all()
        )
    return [dict(row) for row in rows]


def test_serial_debits_allow_over_limit_after_direct_gate(env) -> None:
    """已通过受理门禁的 publication 完成时允许越限，后续门禁才拒绝。"""
    engine, quota, _requests, _times = env
    with engine.begin() as connection:
        lock = quota.calendar.lock_or_verify(connection)
        quota.append_debit(
            connection,
            quota_operation_id="job_1",
            publication_id="pub_1",
            quota_subject_user_id="u1",
            pages=300,
            ownership=ownership(),
            calendar_lock=lock,
            role="user",
            effective_at_utc=NOW,
        )
        quota.append_debit(
            connection,
            quota_operation_id="job_2",
            publication_id="pub_2",
            quota_subject_user_id="u1",
            pages=300,
            ownership=ownership(),
            calendar_lock=lock,
            role="user",
            effective_at_utc=NOW,
        )
        snapshot = quota.read_snapshot(connection, quota_subject_user_id="u1", role="user")
        assert snapshot.used == 600
        rows = connection.execute(select(quota_debit_table)).mappings().all()
        assert [row["quota_operation_id"] for row in rows] == ["job_1", "job_2"]
        with pytest.raises(PlatformError) as gate:
            quota.check_direct_ingest_balance(
                connection, quota_subject_user_id="u1", pages=1, role="user"
            )
        assert gate.value.code == "quota_exceeded"


def test_credit_uniqueness_enforced_by_constraint(env) -> None:
    """H9 串行路径：credit source 键唯一。

    1) 同指纹重放 → 复用同一 credit 行（投影只加一次，不抛错）；
    2) 异指纹同 source → 409 ledger_invariant_conflict（服务层幂等回读）；
    3) 绕过服务层原始 INSERT 同 (entry_kind, namespace, source_id) 第二行 →
       数据库唯一约束 uq_quota_debit_adjustment 拒绝（IntegrityError）。
    """
    engine, quota, _requests, _times = env
    with engine.begin() as connection:
        lock = quota.calendar.lock_or_verify(connection)
        first = quota.append_credit(
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
        # 同指纹重放：复用同一行，投影不重复累加
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
        assert replay == first
        snapshot = quota.read_snapshot(connection, quota_subject_user_id="u1", role="user")
        assert snapshot.extra_granted == 80  # 只加一次
        # 异指纹同 source：服务层幂等回读拒绝
        with pytest.raises(PlatformError) as conflict:
            quota.append_credit(
                connection,
                quota_subject_user_id="u1",
                quota_period="2026-08",
                pages=50,
                adjustment_source_namespace="quota_request",
                adjustment_source_id="req_1",
                ownership=ownership(),
                calendar_lock=lock,
                now=NOW,
            )
        assert conflict.value.code == "ledger_invariant_conflict"
    # 数据库唯一约束是并发双事务的最终防线：绕过服务层直接 INSERT 第二行
    with engine.begin() as connection:
        lock = quota.calendar.lock_or_verify(connection)
        with pytest.raises(IntegrityError):
            connection.execute(
                quota_debit_table.insert().values(
                    quota_debit_id="qd_raw_dup",
                    entry_kind="credit",
                    page_delta=-1,
                    entry_fingerprint="raw-dup-fingerprint",
                    quota_subject_user_id="u1",
                    quota_period="2026-08",
                    cost_center_key="user:u1",
                    ownership_json={},
                    effective_calendar_version_id=lock.version_id,
                    effective_at_utc=NOW,
                    effective_period="2026-08",
                    recorded_calendar_version_id=lock.version_id,
                    recorded_at_utc=NOW,
                    recorded_period="2026-08",
                    created_at_utc=NOW,
                    adjustment_source_namespace="quota_request",
                    adjustment_source_id="req_1",
                )
            )
    assert len(_credit_rows(engine)) == 1  # 约束拒绝后无残留


def test_rejected_terminal_request_same_month_allows_reapply(env) -> None:
    """H4：同月 pending 唯一；真实 reject transition 终态化后同月可重新申请。

    终态化走 ``requests.reject``（真实状态机 + audit + outbox），不再用 raw SQL 改
    status；partial unique index 只拦 pending，因此同月 rejected 后重申放行，且
    历史行保留（共 2 行）。断言按 request id 映射（不同请求的 created_at 相同，
    不做依赖插入顺序的排序断言）。
    """
    engine, _quota, requests, _times = env
    seed_identity(engine)
    created = requests.create(actor=applicant(), requested_pages=100, idempotency_key="k-1")
    assert created["quota_period"] == "2026-08"
    # 同月再申请 → pending_request_exists（partial index）
    with pytest.raises(PlatformError) as dup:
        requests.create(actor=applicant(), requested_pages=50, idempotency_key="k-2")
    assert dup.value.code == "pending_request_exists"
    # 真实 reject transition（非 raw SQL 改 status）
    rejected = requests.reject(
        actor=approver(),
        request_id=created["id"],
        expected_version=1,
        idempotency_key="reject-k-1",
    )
    assert rejected == {"id": created["id"], "version": 2, "status": "rejected"}
    # 同月终态化后重申：partial index 无 pending → 放行；历史行保留
    recreated = requests.create(actor=applicant(), requested_pages=60, idempotency_key="k-3")
    assert recreated["status"] == "pending"
    assert recreated["quota_period"] == "2026-08"
    with engine.connect() as connection:
        rows = (
            connection.execute(
                select(quota_request_table.c.quota_request_id, quota_request_table.c.status)
            )
            .mappings()
            .all()
        )
    by_id = {str(row["quota_request_id"]): str(row["status"]) for row in rows}
    assert len(by_id) == 2
    assert by_id[created["id"]] == "rejected"
    assert by_id[recreated["id"]] == "pending"


def test_next_period_reapply_allowed_while_previous_pending(env) -> None:
    """H4：下一业务月可重新申请（spec §5：同一申请人/当前业务月最多一个 pending）。

    partial unique index 以 (applicant_user_id, quota_period) 为键：2026-08 的 pending
    不阻止 2026-09 的新 pending（不同业务月独立）。断言按 request id 映射。
    """
    engine, _quota, requests, times = env
    seed_identity(engine)
    first = requests.create(actor=applicant(), requested_pages=100, idempotency_key="k-1")
    assert first["quota_period"] == "2026-08"
    times[0] = SEPT  # 推进业务月
    second = requests.create(actor=applicant(), requested_pages=80, idempotency_key="k-2")
    assert second["status"] == "pending"
    assert second["quota_period"] == "2026-09"
    with engine.connect() as connection:
        rows = (
            connection.execute(
                select(
                    quota_request_table.c.quota_request_id,
                    quota_request_table.c.quota_period,
                    quota_request_table.c.status,
                )
            )
            .mappings()
            .all()
        )
    by_id = {str(row["quota_request_id"]): dict(row) for row in rows}
    assert len(by_id) == 2
    assert by_id[first["id"]]["quota_period"] == "2026-08"
    assert by_id[first["id"]]["status"] == "pending"
    assert by_id[second["id"]]["quota_period"] == "2026-09"
    assert by_id[second["id"]]["status"] == "pending"
