"""QuotaRequestService create/read_pending/me 测试（Task 9 + review 修订）。

语义（正式 spec §5 + Task 9 约束 + review 修正）：
- create 只允许 user/minister（403 forbidden_target，ops/admin 均拒绝）；requested_pages
  严格非 bool int 1..500（422 validation_error，边界 1/500 成功）；Idempotency-Key
  非空且限长；真实 SqlAlchemyTransactionManager.reserve_idempotency/
  commit_idempotency（platform_idempotency 表，不复制第二套幂等表逻辑）。
- 同 key 同指纹（scope + canonical request hash）重放返回原 201 payload；同 key
  异指纹 / in_progress 稳定 409 idempotency_key_conflict（in_progress 用真实
  TxManager 提交一条 status=reserved 的平台幂等记录，非伪造不可观察的未提交竞争）；
  同申请人当前业务月最多一条 pending（partial unique index 兜底），冲突稳定 409
  pending_request_exists。
- create 在 calendar lock 后只读取一次 clock：同一 now 同时用于 quota_period /
  created_at_utc / updated_at_utc（review：旧实现 period 与 created_at 各读一次
  clock，跨月边界可能分属不同业务时点；SequenceClock 边界测试使旧实现确定失败）。
- 跨月：同 key 同 facts 重放原月完整 201 payload；同 key 异 pages 优先幂等冲突；
  new key 在新月可创建。
- 异常必须在事务边界正确处理：IntegrityError 捕获后立即转换为 PlatformError 并
  re-raise，由 SqlAlchemyTransaction.__exit__ 回滚，绝不继续使用 failed transaction。
- 创建不预留、不改 quota projection/ledger（DB 断言）；me 返回 Task7 QuotaSnapshot
  精确 10 字段形状并填 pending_request（unlimited 角色为 None）；read path 不创建
  projection（DB 断言）。
- 返回形状：{id, version, status, requested_pages, quota_period, created_at}，
  created_at 为 RFC3339（+00:00）。
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import Engine, create_engine, func, select
from sqlalchemy.engine import Connection
from sqlalchemy.pool import StaticPool

from app.identity.service import AuthPrincipal
from app.platform.database import (
    SqlAlchemyDatabaseClock,
    SqlAlchemyTransactionManager,
    core_metadata,
)
from app.platform.errors import PlatformError
from app.usage._fingerprint import canonical_json
from app.usage.calendar import BusinessCalendarService
from app.usage.ports import NoopOutboxEnqueuePort
from app.usage.quota import QuotaService
from app.usage.requests import QuotaRequestService
from app.usage.schema import quota_debit_table, quota_projection_table, usage_metadata

NOW = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)
# Asia/Shanghai 无 DST（UTC+8）：8 月末最后一刻 = 2026-08-31 15:59:59 UTC，
# 9 月 1 日零点 = 2026-08-31 16:00:00 UTC。
AUG_END = datetime(2026, 8, 31, 15, 59, 59, tzinfo=UTC)
SEP_START = datetime(2026, 8, 31, 16, 0, 0, tzinfo=UTC)
SEPT = datetime(2026, 9, 3, 8, 0, tzinfo=UTC)

_REQUEST_KEY = b"ragqs-quota-request-v1"
_SCOPE = "u1:POST:/quota-requests:"


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


@dataclass
class SequenceClock:
    """按调用次序返回时间（reserve→now→commit 与 TxManager 调用序列对应）。

    越界回退到最后一个值；若未来实现多读 clock，created_at 断言会显式失败，
    防调用次数漂移被静默掩盖。
    """

    values: list[datetime]

    def __post_init__(self) -> None:
        self._index = 0

    def now_utc(self, connection: Connection | None = None) -> datetime:
        del connection
        if self._index < len(self.values):
            value = self.values[self._index]
            self._index += 1
            return value
        return self.values[-1]


def make_service(
    clock: FixedClock | MutableClock | SequenceClock | None = None,
) -> tuple[Engine, QuotaRequestService, BusinessCalendarService]:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    core_metadata.create_all(engine)  # platform_idempotency 表
    usage_metadata.create_all(engine)
    if clock is None:
        clock = FixedClock(NOW)
    calendar = BusinessCalendarService(engine, SqlAlchemyDatabaseClock(engine), "Asia/Shanghai")
    quota = QuotaService(engine, clock, calendar)
    requests = QuotaRequestService(engine, clock, calendar, quota, NoopOutboxEnqueuePort())
    return engine, requests, calendar


def actor(role: str = "user", user_id: str = "u1") -> AuthPrincipal:
    return AuthPrincipal(
        user_id=user_id,
        auth_session_id="s1",
        username="alice",
        role=role,  # type: ignore[arg-type]
        department_id=None,
    )


def request_hash(user_id: str, requested_pages: int) -> str:
    """与 QuotaRequestService._request_hash 相同算法（独立复制，钉住契约）。"""
    canonical = canonical_json({"user_id": user_id, "requested_pages": requested_pages}).encode(
        "utf-8"
    )
    return hmac.new(_REQUEST_KEY, canonical, digestmod=hashlib.sha256).hexdigest()


def count_rows(engine, table) -> int:
    """聚合查询必须取标量（SELECT count(*) 恒返回一行，len() 恒为 1）。"""
    with engine.connect() as connection:
        return int(connection.execute(select(func.count()).select_from(table)).scalar_one())


def test_create_pending_and_replay_same_key() -> None:
    engine, requests, _ = make_service()
    first = requests.create(actor=actor(), requested_pages=100, idempotency_key="key-1")
    assert first["status"] == "pending"
    assert first["version"] == 1
    assert first["requested_pages"] == 100
    assert first["quota_period"] == "2026-08"
    assert first["created_at"].endswith("+00:00")  # RFC3339
    replay = requests.create(actor=actor(), requested_pages=100, idempotency_key="key-1")
    assert replay == first


def test_create_conflicting_key_and_duplicate_pending() -> None:
    engine, requests, _ = make_service()
    requests.create(actor=actor(), requested_pages=100, idempotency_key="key-1")
    with pytest.raises(PlatformError) as dup:
        requests.create(actor=actor(), requested_pages=200, idempotency_key="key-2")
    assert dup.value.code == "pending_request_exists"
    with pytest.raises(PlatformError) as conflict:
        requests.create(actor=actor(), requested_pages=300, idempotency_key="key-1")
    assert conflict.value.code == "idempotency_key_conflict"


@pytest.mark.parametrize("pages", [1, 500])
def test_create_accepts_valid_boundaries(pages: int) -> None:
    engine, requests, _ = make_service()
    created = requests.create(actor=actor(), requested_pages=pages, idempotency_key=f"k-{pages}")
    assert created["requested_pages"] == pages
    assert created["status"] == "pending"


def test_create_roles_user_and_minister_success() -> None:
    engine, requests, _ = make_service()
    user_created = requests.create(
        actor=actor(role="user", user_id="u-user"), requested_pages=10, idempotency_key="k-u"
    )
    assert user_created["status"] == "pending"
    minister_created = requests.create(
        actor=actor(role="minister", user_id="u-minister"),
        requested_pages=20,
        idempotency_key="k-m",
    )
    assert minister_created["status"] == "pending"


def test_create_ops_and_admin_forbidden() -> None:
    engine, requests, _ = make_service()
    for role in ("ops", "admin"):
        with pytest.raises(PlatformError) as forbidden:
            requests.create(
                actor=actor(role=role), requested_pages=100, idempotency_key=f"k-{role}"
            )
        assert forbidden.value.code == "forbidden_target"
        assert forbidden.value.status_code == 403


@pytest.mark.parametrize(
    "pages",
    [0, 501, -1, True, False, "100", 100.0, None],
)
def test_create_strict_pages_negatives(pages: object) -> None:
    engine, requests, _ = make_service()
    with pytest.raises(PlatformError) as invalid:
        requests.create(actor=actor(), requested_pages=pages, idempotency_key="k-neg")  # type: ignore[arg-type]
    assert invalid.value.code == "validation_error"
    assert invalid.value.status_code == 422


def test_create_role_and_key_validation() -> None:
    engine, requests, _ = make_service()
    with pytest.raises(PlatformError) as missing:
        requests.create(actor=actor(), requested_pages=100, idempotency_key="")
    assert missing.value.code == "validation_error"
    with pytest.raises(PlatformError) as blank:
        requests.create(actor=actor(), requested_pages=100, idempotency_key="   ")
    assert blank.value.code == "validation_error"
    # 256 接受：与 platform_idempotency.idempotency_key String(256) 对齐（spec 无
    # 255 产品上限）。
    long_ok = requests.create(
        actor=actor(user_id="u-key-256"), requested_pages=100, idempotency_key="x" * 256
    )
    assert long_ok["status"] == "pending"
    # 257 拒绝：键长度校验先于任何 DB 访问，同用户可复用，无 pending 冲突。
    with pytest.raises(PlatformError) as too_long:
        requests.create(
            actor=actor(user_id="u-key-256"), requested_pages=100, idempotency_key="x" * 257
        )
    assert too_long.value.code == "validation_error"


def test_create_single_clock_read_at_month_boundary() -> None:
    """旧双读取实现确定失败：period 读 AUG_END、created_at 读 SEP_START 分属两月。

    新实现 reserve→now(AUG_END)→commit 只读一次业务 now；created_at 必须等于
    AUG_END 且与 quota_period 同属 2026-08。
    """
    engine, requests, _ = make_service(SequenceClock([NOW, AUG_END, SEP_START, NOW]))
    created = requests.create(actor=actor(), requested_pages=100, idempotency_key="key-boundary")
    assert created["quota_period"] == "2026-08"
    assert created["created_at"] == AUG_END.isoformat()
    created_at = datetime.fromisoformat(created["created_at"])
    local = created_at.astimezone(ZoneInfo("Asia/Shanghai"))
    assert f"{local.year:04d}-{local.month:02d}" == created["quota_period"]


def test_create_in_progress_reserved_row_conflicts() -> None:
    """真实 TxManager 提交一条 status=reserved（hash/scope 匹配、未 commit result）
    的平台幂等记录——这是已提交的持久化状态（故障恢复路径），不是伪造不可观察的
    未提交并发事务；service 调用映射 409 idempotency_key_conflict。"""
    engine, requests, _ = make_service()
    tx_manager = SqlAlchemyTransactionManager(engine, SqlAlchemyDatabaseClock(engine))
    with tx_manager.begin() as tx:
        reservation = tx.reserve_idempotency(
            scope=_SCOPE,
            key="key-in-progress",
            request_hash=request_hash("u1", 100),
        )
        assert reservation.replayed is False
        # 不调用 commit_idempotency：事务提交后该行持久化为 status='reserved'
    with pytest.raises(PlatformError) as conflict:
        requests.create(actor=actor(), requested_pages=100, idempotency_key="key-in-progress")
    assert conflict.value.code == "idempotency_key_conflict"
    assert conflict.value.status_code == 409


def test_cross_month_replay_and_conflict() -> None:
    engine, requests, _ = make_service(MutableClock(NOW))
    first = requests.create(actor=actor(), requested_pages=100, idempotency_key="key-1")
    assert first["quota_period"] == "2026-08"
    # 移到新业务月
    requests._clock.now = SEPT
    # 同 key 同 facts：重放原月完整 201 payload（quota_period/created_at 均为 8 月）
    replay = requests.create(actor=actor(), requested_pages=100, idempotency_key="key-1")
    assert replay == first
    # 同 key 异 pages：幂等冲突优先（先于 pending 唯一性判定）
    with pytest.raises(PlatformError) as conflict:
        requests.create(actor=actor(), requested_pages=200, idempotency_key="key-1")
    assert conflict.value.code == "idempotency_key_conflict"
    # new key 在新月可创建
    new_month = requests.create(actor=actor(), requested_pages=50, idempotency_key="key-2")
    assert new_month["quota_period"] == "2026-09"
    assert new_month["status"] == "pending"


def test_me_exact_shape_and_pending() -> None:
    engine, requests, _ = make_service()
    created = requests.create(actor=actor(), requested_pages=80, idempotency_key="key-3")
    snapshot = requests.me(actor=actor())
    assert set(snapshot) == {
        "used",
        "base_limit",
        "extra_granted",
        "effective_limit",
        "unlimited",
        "reset_at",
        "business_timezone",
        "quota_period",
        "business_calendar_version_id",
        "pending_request",
    }
    assert snapshot["pending_request"] == {
        "id": created["id"],
        "version": 1,
        "requested_pages": 80,
        "quota_period": "2026-08",
        "created_at": created["created_at"],
    }
    assert snapshot["used"] == 0
    assert snapshot["base_limit"] == 500
    assert snapshot["extra_granted"] == 0
    assert snapshot["effective_limit"] == 500
    assert snapshot["unlimited"] is False
    assert snapshot["business_timezone"] == "Asia/Shanghai"
    assert snapshot["quota_period"] == "2026-08"
    assert snapshot["reset_at"].endswith("+00:00")
    assert snapshot["pending_request"]["created_at"].endswith("+00:00")
    # minister 与 user 同形状
    minister_snapshot = requests.me(actor=actor(role="minister"))
    assert set(minister_snapshot) == set(snapshot)
    assert minister_snapshot["unlimited"] is False


def test_me_unlimited_roles_shape() -> None:
    engine, requests, _ = make_service()
    for role in ("ops", "admin"):
        ops_snapshot = requests.me(actor=actor(role=role))
        assert set(ops_snapshot) == {
            "used",
            "base_limit",
            "extra_granted",
            "effective_limit",
            "unlimited",
            "reset_at",
            "business_timezone",
            "quota_period",
            "business_calendar_version_id",
            "pending_request",
        }
        assert ops_snapshot["unlimited"] is True
        assert ops_snapshot["pending_request"] is None
        assert ops_snapshot["effective_limit"] == 0


def test_create_does_not_write_ledger_or_projection() -> None:
    engine, requests, _ = make_service()
    requests.create(actor=actor(), requested_pages=100, idempotency_key="key-1")
    assert count_rows(engine, quota_debit_table) == 0
    assert count_rows(engine, quota_projection_table) == 0


def test_me_read_path_does_not_create_projection() -> None:
    engine, requests, _ = make_service()
    snapshot = requests.me(actor=actor())
    assert snapshot["used"] == 0
    assert snapshot["effective_limit"] == 500
    assert count_rows(engine, quota_projection_table) == 0
    assert count_rows(engine, quota_debit_table) == 0
