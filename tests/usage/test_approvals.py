"""QuotaRequestService 审批（summary/list/approve/reject）+ 审计 + outbox 原子性测试（Task 10 + review 两轮）。

语义（正式 spec §5 + Task 10 约束 + review 两轮修订；旧 brief 示例陷阱已审计修正）：
- summary 所有登录角色可读：仅 ops 看到当前业务月真实 pending 计数（2026-08 两条
  → 2）；user/minister/admin 一律 {"quota_pending": 0, "submission_pending": 0}
  （正式 spec L50：admin 的 quota_pending 为 0；旧 brief 示例把 admin 放行到真实
  计数，已按正式 spec 修正）。submission_pending 恒 0。
- list_quota_requests 仅精确 ops（403 forbidden_target，admin 同样 403）；status
  只允许 pending/approved/rejected/cancelled（其他 422 validation_error）；列表
  created_at 升序；item 精确形状（applicant/current_usage 嵌套）；display_name 读
  identity_user_table（缺失回落 applicant id）；current_usage 用
  QuotaService.read_snapshot 逐 applicant（读路径不创建投影；list 调用前后投影行数
  不变）；created_at/reviewed_at RFC3339（SQLite naive datetime → _utc 补 UTC）。
- approve/reject 均要求 Idempotency-Key（非空、≤256）与 expected_version 严格非
  bool **正整数**（reserve 前校验）；approve approved_pages 缺省 requested、非 None
  时严格非 bool int 且 >=1（0/-1 与 None 绝不 hash 碰撞、422），动态
  `<= requested_pages` 在锁后校验；reject 无自由文本 reason。
- 幂等：真实 TxManager reserve/commit；scope 是 `approval:` + 对 actor/action/target
  canonical fingerprint 的固定长度字符串，既保持隔离又不超过底层 String(128)；
  **审批专用 canonical hash `_approval_hash`**（HMAC-SHA256(b"ragqs-quota-approval-v1",
  canonical_json({user_id, action, target, expected_version, approved_pages}))）——
  **不复用 create 的 `_request_hash`**，**不用 `approved_pages or -1` truthiness**
  （None 与 0/-1 由 canonical_json typed-tag null/num 区分）；覆盖 user_id / action /
  target / expected_version / approved_pages 是否省略及原始值——same key 任一稳定
  请求事实变化均 409 idempotency_key_conflict；同 key 同事实重放原 200 payload；
  in_progress → 409。幂等 reserve 先于状态/version 校验（task brief）。跨
  action/target 的 same key 属独立幂等域（scope 指纹含 actor/action/target），非重放。
- approve 事务内固定顺序：reserve → 锁 quota_request 行（with_for_update）→ 校验
  pending（否则 409 already_processed）→ version==expected（否则 409
  version_conflict）→ 申请人 identity_user_table.lifecycle_status=='active'（否则
  409 quota_request_not_approvable）→ **单一 calendar lock + 单一 DB now**（
  `_require_approvable` 锁行后取一次 lock/now 并返回，复用于 period 校验 /
  append_credit 的 calendar_lock+now / reviewed_at / updated_at / audit
  occurred_at / 投影 updated_at——不再二次读取 clock）→ 目标期仍当前
  （period_for(now)==request.quota_period，否则同 409）→ append_credit
  （namespace=quota_request/source=request_id，同一 TxManager connection，投影
  updated_at 显式传同一 now——review 第二轮：`_update_projection_locked` 不再隐式
  读取 clock）→ UPDATE request（version+1/approved/approver 快照/approved_pages/
  credit_entry_id/reviewed_at/updated_at）→ platform_audit INSERT（details_json={}，
  request_id=current_context().request_id or "req_system"）→
  outbox.enqueue(connection=tx.connection, event_type="quota_approved",
  aggregate_type="quota_request", aggregate_id=request_id,
  transition_version=new_version, payload_fingerprint=ledger_fingerprint(...),
  payload={"request_id": request_id}) → commit_idempotency → 200。
- reject 同骨架：无 credit；result="quota_request_rejected"；
  event_type="quota_rejected"；返回 {id, version, status: "rejected"}。
- outbox.enqueue 抛错 → 整个事务回滚（request 仍 pending、零 credit/projection/
  outbox/audit/idempotency 残留，只剩 create 自己的幂等行）——RecordingOutboxPort
  是真正事务型外部表端口（同 connection 写入、持久化全部参数含 aggregate_type/
  payload_fingerprint，回滚后不残留），不是内存 Noop 假装原子（旧 brief 示例的
  SELECT rowcount==0 断言不可靠，已改为 count_rows）。
- 目标期关闭验证用可推进时钟（H4）：2026-08 创建申请后推进到 2026-09-02，
  approve/reject → 409 quota_request_not_approvable（旧 brief 示例时钟从未推进，
  关闭路径永不触发，已修正为申请后推进 now）。
- 月界单时钟证据（review 第二轮）：SequenceClock [NOW, AUG_END, SEP_START, NOW]
  中 TxManager 的 reserve/commit 为幂等元数据读取（合法、无业务语义）；domain now
  只在 `_require_approvable` 消费 1 次（AUG_END）；reviewed_at/updated_at/audit
  occurred_at/**projection.updated_at** 全 == AUG_END——旧多读实现（含投影隐式
  二次读取）确定失败。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

import pytest
from sqlalchemy import (
    JSON,
    Column,
    Integer,
    MetaData,
    String,
    Table,
    and_,
    create_engine,
    func,
    select,
)
from sqlalchemy.engine import Connection
from sqlalchemy.pool import StaticPool

from app.identity.schema import identity_metadata, identity_user_table
from app.identity.service import AuthPrincipal
from app.platform.database import (
    SqlAlchemyDatabaseClock,
    core_metadata,
    platform_audit_table,
    platform_idempotency_table,
)
from app.platform.errors import PlatformError
from app.usage._fingerprint import canonical_json, ledger_fingerprint
from app.usage.calendar import BusinessCalendarService
from app.usage.quota import QuotaService
from app.usage.requests import QuotaRequestService
from app.usage.schema import (
    quota_debit_table,
    quota_projection_table,
    quota_request_table,
    usage_metadata,
)

NOW = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)
# Asia/Shanghai（UTC+8，无 DST）：2026-08-31 15:59:59 UTC = 8 月末最后一刻；
# 2026-08-31 16:00:00 UTC = 2026-09-01 00:00 +08（业务月界）；2026-09-02 08:00 +08
# = 2026-09-02 00:00 UTC。
AUG_END = datetime(2026, 8, 31, 15, 59, 59, tzinfo=UTC)
SEP_START = datetime(2026, 8, 31, 16, 0, 0, tzinfo=UTC)
# Asia/Shanghai（UTC+8）：2026-09-02 08:00 +08 = 2026-09-02 00:00 UTC。
SEPT = datetime(2026, 9, 2, 0, 0, tzinfo=UTC)

_outbox_meta = MetaData()
tests_outbox_enqueued = Table(
    "tests_outbox_enqueued",
    _outbox_meta,
    Column("id", Integer, primary_key=True),
    Column("event_type", String(64), nullable=False),
    Column("aggregate_type", String(64), nullable=False),
    Column("aggregate_id", String(128), nullable=False),
    Column("transition_version", Integer, nullable=False),
    Column("payload_fingerprint", String(128), nullable=False),
    Column("payload_json", JSON, nullable=False),
)


@dataclass(frozen=True, slots=True)
class MutableClock:
    """H4：可推进时钟——测试共享同一个 times 列表，推进 `times[0]` 即可推进 now。"""

    times: list[datetime]

    def now_utc(self, connection: Connection | None = None) -> datetime:
        del connection
        return self.times[0]


@dataclass
class SequenceClock:
    """按调用次序返回时间（越界回退末值）。

    Task 10 review：审批事务的**业务 now** 必须只读取一次；同一注入 clock 也会被
    TxManager 的 reserve/commit_idempotency 用于幂等元数据时间，但这些读取不参与
    审批业务事实。业务 now 只在 `_require_approvable` 读取一次。旧多读实现会在序列
    [NOW, AUG_END, SEP_START, NOW] 下使 period 读 AUG_END 通过旧月校验、reviewed_at
    读 SEP_START 写入新月；当前实现只消费 AUG_END 作为业务 now。
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


class RecordingOutboxPort:
    """真正事务型 outbox 端口：同 connection 写入外部表（回滚即消失），非内存 Noop。

    Task 10 review：保留全部入参（含 aggregate_type / payload_fingerprint）持久化，
    测试对 outbox 行做完整字段断言，不丢弃任何契约参数。
    """

    def __init__(self, fail: bool = False) -> None:
        self.fail = fail

    def enqueue(
        self,
        *,
        connection,
        event_type: Literal["quota_approved", "quota_rejected"],
        aggregate_type: Literal["quota_request"],
        aggregate_id: str,
        transition_version: int,
        recipient_user_id: str,
        occurred_at: datetime,
        payload_fingerprint: str,
        payload: dict,
    ) -> None:
        del recipient_user_id, occurred_at
        if self.fail:
            raise PlatformError(
                "quota_event_outbox_unavailable", "Outbox enqueue failed", {}, 503, True
            )
        connection.execute(
            tests_outbox_enqueued.insert().values(
                event_type=event_type,
                aggregate_type=aggregate_type,
                aggregate_id=aggregate_id,
                transition_version=transition_version,
                payload_fingerprint=payload_fingerprint,
                payload_json=payload,
            )
        )


def make_service(now: datetime = NOW, clock: MutableClock | SequenceClock | None = None):
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    core_metadata.create_all(engine)
    identity_metadata.create_all(engine)
    usage_metadata.create_all(engine)
    _outbox_meta.create_all(engine)
    if clock is None:
        times = [now]
        clock = MutableClock(times)
    elif isinstance(clock, MutableClock):
        times = clock.times
    else:
        times = [now]
    calendar = BusinessCalendarService(engine, SqlAlchemyDatabaseClock(engine), "Asia/Shanghai")
    quota = QuotaService(engine, clock, calendar)
    return engine, quota, calendar, times


def seed_identity(engine) -> None:
    with engine.begin() as connection:
        for user in [
            ("u1", "alice", "user", "active"),
            ("u2", "bob", "user", "active"),
            ("ops1", "op", "ops", "active"),
            ("ops2", "op2", "ops", "active"),
            ("admin1", "root", "admin", "active"),
        ]:
            uid, username, role, lifecycle = user
            connection.execute(
                identity_user_table.insert().values(
                    id=uid,
                    username=username,
                    normalized_username=username,
                    password_hash="x",
                    real_name=username,
                    display_name=username,
                    department_id=None,
                    role=role,
                    lifecycle_status=lifecycle,
                    version=1,
                    preferences_json={},
                    transition_version=1,
                    created_at_utc=NOW,
                    updated_at_utc=NOW,
                )
            )


def applicant(uid: str = "u1") -> AuthPrincipal:
    return AuthPrincipal(
        user_id=uid,
        auth_session_id="s1",
        username="alice",
        role="user",  # type: ignore[arg-type]
        department_id=None,
    )


def approver() -> AuthPrincipal:
    return AuthPrincipal(
        user_id="ops1",
        auth_session_id="s2",
        username="op",
        role="ops",
        department_id=None,
    )


def admin() -> AuthPrincipal:
    return AuthPrincipal(
        user_id="root1",
        auth_session_id="s3",
        username="root",
        role="admin",
        department_id=None,
    )


def ministrator() -> AuthPrincipal:
    return AuthPrincipal(
        user_id="m1",
        auth_session_id="s4",
        username="minister",
        role="minister",  # type: ignore[arg-type]
        department_id=None,
    )


def second_approver() -> AuthPrincipal:
    return AuthPrincipal(
        user_id="ops2",
        auth_session_id="s5",
        username="op2",
        role="ops",
        department_id=None,
    )


def create_pending(engine, requests, pages: int = 100, uid: str = "u1") -> dict:
    return requests.create(
        actor=applicant(uid), requested_pages=pages, idempotency_key=f"create-{uid}-{pages}"
    )


def count_rows(engine, table) -> int:
    """聚合查询必须取标量；SELECT 的 Result.rowcount 不可靠（旧 brief 示例陷阱）。"""
    with engine.connect() as connection:
        return int(connection.execute(select(func.count()).select_from(table)).scalar_one())


def test_approve_adds_unique_credit_audit_event_and_updates_projection() -> None:
    engine, quota, calendar, _times = make_service()
    seed_identity(engine)
    requests = QuotaRequestService(
        engine, MutableClock(_times), calendar, quota, RecordingOutboxPort()
    )
    created = create_pending(engine, requests, 100)
    result = requests.approve(
        actor=approver(),
        request_id=created["id"],
        expected_version=1,
        approved_pages=80,
        idempotency_key="approve-1",
    )
    assert result["status"] == "approved"
    assert result["approved_pages"] == 80
    assert result["version"] == 2
    assert result["credit_entry_id"] is not None
    assert result["quota_period"] == "2026-08"
    with engine.connect() as connection:
        credits = (
            connection.execute(
                select(quota_debit_table).where(quota_debit_table.c.entry_kind == "credit")
            )
            .mappings()
            .all()
        )
        assert len(credits) == 1
        assert credits[0]["page_delta"] == -80
        assert credits[0]["adjustment_source_namespace"] == "quota_request"
        assert credits[0]["adjustment_source_id"] == created["id"]
        events = connection.execute(select(tests_outbox_enqueued)).mappings().all()
        assert len(events) == 1
        assert events[0]["event_type"] == "quota_approved"
        assert events[0]["aggregate_type"] == "quota_request"
        assert events[0]["aggregate_id"] == created["id"]
        assert events[0]["transition_version"] == 2
        assert events[0]["payload_json"] == {"request_id": created["id"]}
        # payload_fingerprint 与同一 canonical 算法独立计算一致（review 4：不丢弃参数）
        expected_fp = ledger_fingerprint("quota_approved", {"request_id": created["id"]})
        assert events[0]["payload_fingerprint"] == expected_fp
        audits = connection.execute(select(platform_audit_table)).mappings().all()
        assert len(audits) == 1
        assert audits[0]["actor_id"] == "ops1"
        assert audits[0]["resource_type"] == "quota_request"
        assert audits[0]["resource_id"] == created["id"]
        assert audits[0]["result"] == "quota_request_approved"
        assert audits[0]["details_json"] == {}
        # 服务层测试无 RequestContext：audit request_id 回落 "req_system"
        assert audits[0]["request_id"] == "req_system"
        # occurred_at 与 reviewed_at 同一 now（单一 clock，review 2）
        assert audits[0]["occurred_at_utc"].replace(tzinfo=UTC) == NOW
        snapshot = quota.read_snapshot(connection, quota_subject_user_id="u1", role="user")
        assert snapshot.extra_granted == 80
    with pytest.raises(PlatformError) as twice:
        requests.approve(
            actor=approver(),
            request_id=created["id"],
            expected_version=2,
            approved_pages=80,
            idempotency_key="approve-2",
        )
    assert twice.value.code == "already_processed"


def test_approve_defaults_to_requested_and_validates_version_range() -> None:
    engine, quota, calendar, _times = make_service()
    seed_identity(engine)
    requests = QuotaRequestService(
        engine, MutableClock(_times), calendar, quota, RecordingOutboxPort()
    )
    created = create_pending(engine, requests, 100)
    result = requests.approve(
        actor=approver(),
        request_id=created["id"],
        expected_version=1,
        approved_pages=None,
        idempotency_key="approve-default",
    )
    assert result["approved_pages"] == 100
    created2 = create_pending(engine, requests, 50, uid="u2")
    with pytest.raises(PlatformError) as ver:
        requests.approve(
            actor=approver(),
            request_id=created2["id"],
            expected_version=99,
            approved_pages=50,
            idempotency_key="approve-ver",
        )
    assert ver.value.code == "version_conflict"


def test_approve_rejects_when_applicant_inactive() -> None:
    engine, quota, calendar, _times = make_service()
    seed_identity(engine)
    requests = QuotaRequestService(
        engine, MutableClock(_times), calendar, quota, RecordingOutboxPort()
    )
    created = create_pending(engine, requests, 100)
    with engine.begin() as connection:
        connection.execute(
            identity_user_table.update()
            .where(identity_user_table.c.id == "u1")
            .values(lifecycle_status="pending_delete")
        )
    with pytest.raises(PlatformError) as frozen:
        requests.approve(
            actor=approver(),
            request_id=created["id"],
            expected_version=1,
            approved_pages=50,
            idempotency_key="approve-frozen",
        )
    assert frozen.value.code == "quota_request_not_approvable"


def test_approve_rejects_when_target_period_closed() -> None:
    """H4：可推进时钟——申请在 2026-08 创建后推进到 2026-09-02，目标月 2026-08 已关闭。

    旧 brief 示例创建与审批共用同一固定 now（2026-09-02），申请月即当前月，关闭
    路径永不触发；这里在 create 之后推进 `times[0]` 使关闭路径确定可达。
    """
    engine, quota, calendar, times = make_service(now=NOW)
    seed_identity(engine)
    requests = QuotaRequestService(
        engine, MutableClock(times), calendar, quota, RecordingOutboxPort()
    )
    created = create_pending(engine, requests, 100)
    assert created["quota_period"] == "2026-08"
    times[0] = SEPT  # 推进业务月
    with pytest.raises(PlatformError) as closed:
        requests.approve(
            actor=approver(),
            request_id=created["id"],
            expected_version=1,
            approved_pages=50,
            idempotency_key="approve-closed",
        )
    assert closed.value.code == "quota_request_not_approvable"


def test_reject_writes_no_credit_and_publishes_quota_rejected() -> None:
    engine, quota, calendar, _times = make_service()
    seed_identity(engine)
    requests = QuotaRequestService(
        engine, MutableClock(_times), calendar, quota, RecordingOutboxPort()
    )
    created = create_pending(engine, requests, 100)
    result = requests.reject(
        actor=approver(),
        request_id=created["id"],
        expected_version=1,
        idempotency_key="reject-1",
    )
    assert result == {"id": created["id"], "version": 2, "status": "rejected"}
    assert count_rows(engine, quota_debit_table) == 0
    with engine.connect() as connection:
        events = connection.execute(select(tests_outbox_enqueued)).mappings().all()
        assert len(events) == 1
        assert events[0]["event_type"] == "quota_rejected"
        assert events[0]["aggregate_type"] == "quota_request"
        assert events[0]["aggregate_id"] == created["id"]
        assert events[0]["transition_version"] == 2
        assert events[0]["payload_json"] == {"request_id": created["id"]}
        expected_fp = ledger_fingerprint("quota_rejected", {"request_id": created["id"]})
        assert events[0]["payload_fingerprint"] == expected_fp
        audits = connection.execute(select(platform_audit_table)).mappings().all()
        assert len(audits) == 1
        assert audits[0]["actor_id"] == "ops1"
        assert audits[0]["resource_type"] == "quota_request"
        assert audits[0]["resource_id"] == created["id"]
        assert audits[0]["result"] == "quota_request_rejected"
        assert audits[0]["details_json"] == {}
        assert audits[0]["request_id"] == "req_system"
        assert audits[0]["occurred_at_utc"].replace(tzinfo=UTC) == NOW


def test_approval_atomicity_when_outbox_fails() -> None:
    engine, quota, calendar, _times = make_service()
    seed_identity(engine)
    requests = QuotaRequestService(
        engine, MutableClock(_times), calendar, quota, RecordingOutboxPort(fail=True)
    )
    created = create_pending(engine, requests, 100)
    with pytest.raises(PlatformError) as outbox_err:
        requests.approve(
            actor=approver(),
            request_id=created["id"],
            expected_version=1,
            approved_pages=50,
            idempotency_key="approve-fail",
        )
    assert outbox_err.value.code == "quota_event_outbox_unavailable"
    assert outbox_err.value.status_code == 503
    with engine.connect() as connection:
        row = (
            connection.execute(
                select(quota_request_table).where(
                    quota_request_table.c.quota_request_id == created["id"]
                )
            )
            .mappings()
            .one()
        )
        assert row["status"] == "pending"  # 全回滚：不残留 approved 状态
        assert row["version"] == 1
    assert count_rows(engine, quota_debit_table) == 0
    assert count_rows(engine, quota_projection_table) == 0
    assert count_rows(engine, tests_outbox_enqueued) == 0
    assert count_rows(engine, platform_audit_table) == 0
    # approve 的幂等 reserve 行随事务回滚：只剩 create 自己的已完成幂等行。
    assert count_rows(engine, platform_idempotency_table) == 1


def test_summary_and_list_scoping() -> None:
    engine, quota, calendar, times = make_service()
    seed_identity(engine)
    requests = QuotaRequestService(
        engine, MutableClock(times), calendar, quota, RecordingOutboxPort()
    )
    first = create_pending(engine, requests, 100)
    # 推进 1 分钟再创建第二条：created_at 严格升序，列表顺序确定（同秒并列无定义序）。
    times[0] = NOW + timedelta(minutes=1)
    create_pending(engine, requests, 50, uid="u2")
    assert requests.summary(actor=approver()) == {"quota_pending": 2, "submission_pending": 0}
    assert requests.summary(actor=applicant()) == {"quota_pending": 0, "submission_pending": 0}
    assert requests.summary(actor=admin()) == {"quota_pending": 0, "submission_pending": 0}
    items = requests.list_quota_requests(actor=approver(), status="pending")
    assert len(items) == 2
    assert set(items[0]) == {
        "id",
        "version",
        "status",
        "applicant",
        "current_usage",
        "requested_pages",
        "approved_pages",
        "quota_period",
        "created_at",
        "reviewed_at",
    }
    assert items[0]["applicant"] == {"id": "u1", "display_name": "alice"}
    assert items[0]["current_usage"] == {"used": 0, "effective_limit": 500}
    assert items[0]["approved_pages"] is None
    assert items[0]["reviewed_at"] is None
    assert items[0]["created_at"] == first["created_at"]
    assert items[0]["created_at"].endswith("+00:00")
    assert items[0]["created_at"] < items[1]["created_at"]
    assert items[1]["applicant"]["display_name"] == "bob"
    # 无 identity 行的申请人（display_name 缺失）→ 回落 applicant id
    times[0] = NOW + timedelta(minutes=2)
    create_pending(engine, requests, 30, uid="ghost")
    fallback = requests.list_quota_requests(actor=approver(), status="pending")
    assert len(fallback) == 3
    assert fallback[2]["applicant"] == {"id": "ghost", "display_name": "ghost"}
    with pytest.raises(PlatformError) as forbidden:
        requests.list_quota_requests(actor=applicant(), status="pending")
    assert forbidden.value.status_code == 403
    with pytest.raises(PlatformError) as forbidden_admin:
        requests.list_quota_requests(actor=admin(), status="pending")
    assert forbidden_admin.value.status_code == 403
    with pytest.raises(PlatformError) as bad_status:
        requests.list_quota_requests(actor=approver(), status="bogus")
    assert bad_status.value.code == "validation_error"


_APPROVAL_KEY = b"ragqs-quota-approval-v1"


def expected_approval_hash(
    *,
    action: str,
    user_id: str,
    target: str,
    expected_version: int,
    approved_pages: int | None,
) -> str:
    """独立 canonical 计算，作为 **expected 基准**（非被测对象）。

    review 第二轮：hash 契约测试不再只复制本地 helper 自比——改为断言**生产
    `QuotaRequestService._approval_hash`** 的结果 == 本 expected，并端到端读取
    `platform_idempotency.request_hash` 与 expected 比较。覆盖 user_id / action /
    target / expected_version / approved_pages 原始值（None 与 0/-1 由 canonical_json
    typed-tag 区分：null vs num，不用 truthiness）。
    """
    import hashlib
    import hmac

    canonical = canonical_json(
        {
            "user_id": user_id,
            "action": action,
            "target": target,
            "expected_version": expected_version,
            "approved_pages": approved_pages,
        }
    ).encode("utf-8")
    return hmac.new(_APPROVAL_KEY, canonical, digestmod=hashlib.sha256).hexdigest()


def test_approval_hash_production_contract_and_persisted_request_hash() -> None:
    """review 第二轮：生产 `_approval_hash` 固定结果 == 独立 canonical expected；审批后
    `platform_idempotency.request_hash` 与 expected 逐字节一致（端到端钉住持久化
    幂等行的真实 hash）。覆盖 expected_version/action/target/user/pages presence/value。"""
    engine, quota, calendar, _times = make_service()
    seed_identity(engine)
    requests = QuotaRequestService(
        engine, MutableClock(_times), calendar, quota, RecordingOutboxPort()
    )
    created = create_pending(engine, requests, 100)
    # 生产方法契约：8 个稳定事实变体全部 == 独立 canonical expected
    cases = [
        ("approve", "ops1", created["id"], 1, 80),
        ("reject", "ops1", created["id"], 1, None),
        ("approve", "ops2", created["id"], 1, None),
        ("approve", "ops1", created["id"], 2, 80),
        ("approve", "ops1", created["id"], 1, None),  # pages 省略
        ("approve", "ops1", created["id"], 1, 90),  # pages 值变化
    ]
    for action, user_id, target, ev, pages in cases:
        assert requests._approval_hash(
            action=action,
            actor_user_id=user_id,
            request_id=target,
            expected_version=ev,
            approved_pages=pages,
        ) == expected_approval_hash(
            action=action,
            user_id=user_id,
            target=target,
            expected_version=ev,
            approved_pages=pages,
        )
    # 端到端：approve（pages=80）+ 第二个请求 approve（pages 省略，expected_version 2）
    requests.approve(
        actor=approver(),
        request_id=created["id"],
        expected_version=1,
        approved_pages=80,
        idempotency_key="key-persist-1",
    )
    pending2 = create_pending(engine, requests, 50, uid="u2")
    requests.approve(
        actor=approver(),
        request_id=pending2["id"],
        expected_version=1,
        approved_pages=None,
        idempotency_key="key-persist-2",
    )
    with engine.connect() as connection:
        rows = (
            connection.execute(
                select(
                    platform_idempotency_table.c.idempotency_key,
                    platform_idempotency_table.c.request_hash,
                ).where(
                    platform_idempotency_table.c.idempotency_key.in_(
                        ["key-persist-1", "key-persist-2"]
                    )
                )
            )
            .mappings()
            .all()
        )
        persisted = {row["idempotency_key"]: row["request_hash"] for row in rows}
    assert persisted["key-persist-1"] == expected_approval_hash(
        action="approve",
        user_id="ops1",
        target=created["id"],
        expected_version=1,
        approved_pages=80,
    )
    assert persisted["key-persist-2"] == expected_approval_hash(
        action="approve",
        user_id="ops1",
        target=pending2["id"],
        expected_version=1,
        approved_pages=None,  # pages 省略 → null
    )
    # 稳定事实变化 → 不同 hash（生产方法层面，非仅本地 helper）
    assert requests._approval_hash(
        action="approve",
        actor_user_id="ops1",
        request_id=created["id"],
        expected_version=1,
        approved_pages=80,
    ) != requests._approval_hash(
        action="approve",
        actor_user_id="ops1",
        request_id=created["id"],
        expected_version=1,
        approved_pages=None,
    )


def test_approval_hash_distinguishes_all_stable_facts() -> None:
    """Task 10 review：生产 hash 必须区分全部稳定请求事实——same key 任一变化均 409
    （行为层面已由 conflict 测试实证；本测试钉住生产方法指纹区分）。"""
    engine, quota, calendar, _times = make_service()
    requests = QuotaRequestService(
        engine, MutableClock(_times), calendar, quota, RecordingOutboxPort()
    )
    base = dict(
        action="approve",
        user_id="ops1",
        target="qr_x",
        expected_version=1,
        approved_pages=80,
    )
    variants = {
        "action": dict(action="reject"),
        "user_id": dict(user_id="ops2"),
        "target": dict(target="qr_y"),
        "expected_version": dict(expected_version=2),
        "pages_omitted": dict(approved_pages=None),
        "pages_value": dict(approved_pages=90),
        "pages_zero": dict(approved_pages=0),
        "pages_minus_one": dict(approved_pages=-1),
    }

    def _prod(**kw: object) -> str:
        return requests._approval_hash(
            action=kw["action"],  # type: ignore[arg-type]
            actor_user_id=kw["user_id"],  # type: ignore[arg-type]
            request_id=kw["target"],  # type: ignore[arg-type]
            expected_version=kw["expected_version"],  # type: ignore[arg-type]
            approved_pages=kw["approved_pages"],  # type: ignore[arg-type]
        )

    base_fp = _prod(**base)
    assert _prod(**base) == base_fp  # 同事实同指纹
    for name, delta in variants.items():
        assert _prod(**{**base, **delta}) != base_fp, name
    # 0/-1 与 None 绝不 hash 碰撞（typed-tag null vs num）
    assert _prod(**base) != _prod(
        action="approve",
        user_id="ops1",
        target="qr_x",
        expected_version=1,
        approved_pages=0,
    )
    assert _prod(**base) != _prod(
        action="approve",
        user_id="ops1",
        target="qr_x",
        expected_version=1,
        approved_pages=-1,
    )


def test_approve_same_key_different_expected_version_conflicts() -> None:
    """review：same key 不同 expected_version → 409 idempotency_key_conflict（重放尝试
    无副作用残留，同 key 同事实重放仍可用）。"""
    engine, quota, calendar, _times = make_service()
    seed_identity(engine)
    requests = QuotaRequestService(
        engine, MutableClock(_times), calendar, quota, RecordingOutboxPort()
    )
    created = create_pending(engine, requests, 100)
    first = requests.approve(
        actor=approver(),
        request_id=created["id"],
        expected_version=1,
        approved_pages=80,
        idempotency_key="key-ver",
    )
    with pytest.raises(PlatformError) as conflict:
        requests.approve(
            actor=approver(),
            request_id=created["id"],
            expected_version=2,
            approved_pages=80,
            idempotency_key="key-ver",
        )
    assert conflict.value.code == "idempotency_key_conflict"
    # 冲突重放尝试零副作用：credit/outbox/audit 仍各 1 行，request 版本 2。
    assert count_rows(engine, quota_debit_table) == 1
    assert count_rows(engine, tests_outbox_enqueued) == 1
    assert count_rows(engine, platform_audit_table) == 1
    with engine.connect() as connection:
        row = (
            connection.execute(
                select(quota_request_table).where(
                    quota_request_table.c.quota_request_id == created["id"]
                )
            )
            .mappings()
            .one()
        )
        assert row["version"] == 2
    # 同 key 同事实重放仍可用（完整 payload）
    replay = requests.approve(
        actor=approver(),
        request_id=created["id"],
        expected_version=1,
        approved_pages=80,
        idempotency_key="key-ver",
    )
    assert replay == first
    assert count_rows(engine, quota_debit_table) == 1  # 不重复 credit


def test_reject_same_key_different_expected_version_conflicts() -> None:
    """review：reject 的 expected_version 变化同样 409；replay 后无重复副作用。"""
    engine, quota, calendar, _times = make_service()
    seed_identity(engine)
    requests = QuotaRequestService(
        engine, MutableClock(_times), calendar, quota, RecordingOutboxPort()
    )
    created = create_pending(engine, requests, 100)
    first = requests.reject(
        actor=approver(),
        request_id=created["id"],
        expected_version=1,
        idempotency_key="key-ver-reject",
    )
    with pytest.raises(PlatformError) as conflict:
        requests.reject(
            actor=approver(),
            request_id=created["id"],
            expected_version=2,
            idempotency_key="key-ver-reject",
        )
    assert conflict.value.code == "idempotency_key_conflict"
    assert count_rows(engine, tests_outbox_enqueued) == 1
    assert count_rows(engine, platform_audit_table) == 1
    with engine.connect() as connection:
        row = (
            connection.execute(
                select(quota_request_table).where(
                    quota_request_table.c.quota_request_id == created["id"]
                )
            )
            .mappings()
            .one()
        )
        assert row["status"] == "rejected"
        assert row["version"] == 2
    replay = requests.reject(
        actor=approver(),
        request_id=created["id"],
        expected_version=1,
        idempotency_key="key-ver-reject",
    )
    assert replay == first
    assert count_rows(engine, tests_outbox_enqueued) == 1


def test_approval_same_key_different_action_and_target_conflict() -> None:
    """review：hash 覆盖 action/target（契约层面由 `test_approval_hash_covers_
    all_stable_facts` 钉住）；行为层面——scope 指纹含 actor/action/target，跨
    action/target 的 same key 属于**独立幂等域**（新 scope 新记录），不产生幂等
    重放：reject 尝试对已 approved 的 target → 业务 409 already_processed（绝不
    重放 approve payload）；不同 target 的 same key → 独立新请求正常处理。"""
    engine, quota, calendar, _times = make_service()
    seed_identity(engine)
    requests = QuotaRequestService(
        engine, MutableClock(_times), calendar, quota, RecordingOutboxPort()
    )
    created = create_pending(engine, requests, 100)
    approved = requests.approve(
        actor=approver(),
        request_id=created["id"],
        expected_version=1,
        approved_pages=None,
        idempotency_key="shared-key",
    )
    # same key + reject action（同 target）：hash 不同（action 覆盖）→ 非重放 →
    # target 已 approved → 业务 409 already_processed，绝不返回 approve payload
    with pytest.raises(PlatformError) as action_conflict:
        requests.reject(
            actor=approver(),
            request_id=created["id"],
            expected_version=2,
            idempotency_key="shared-key",
        )
    assert action_conflict.value.code == "already_processed"
    # same key + 不同 target（另一 pending）：scope 含 target → 独立幂等域 → 正常审批
    created2 = create_pending(engine, requests, 50, uid="u2")
    other = requests.approve(
        actor=approver(),
        request_id=created2["id"],
        expected_version=1,
        approved_pages=None,
        idempotency_key="shared-key",
    )
    assert other["status"] == "approved"
    assert other["id"] == created2["id"]
    # 同 key 同 scope 同事实重放仍可用（created 的 approve）
    replay = requests.approve(
        actor=approver(),
        request_id=created["id"],
        expected_version=1,
        approved_pages=None,
        idempotency_key="shared-key",
    )
    assert replay == approved
    # 两个 approve 各产生唯一 credit/outbox（独立幂等域）
    assert count_rows(engine, quota_debit_table) == 2
    assert count_rows(engine, tests_outbox_enqueued) == 2


def test_approve_same_key_pages_presence_value_and_invalid_zero() -> None:
    """review：same key approved_pages 是否省略/值变化均 409；非法 0/-1 与 None 不
    hash 碰撞且稳定 422（reserve 前）。"""
    engine, quota, calendar, _times = make_service()
    seed_identity(engine)
    requests = QuotaRequestService(
        engine, MutableClock(_times), calendar, quota, RecordingOutboxPort()
    )
    created = create_pending(engine, requests, 100)
    first = requests.approve(
        actor=approver(),
        request_id=created["id"],
        expected_version=1,
        approved_pages=None,
        idempotency_key="key-pages",
    )
    assert first["approved_pages"] == 100
    # same key + 显式值 → 409（presence 变化）
    with pytest.raises(PlatformError) as presence:
        requests.approve(
            actor=approver(),
            request_id=created["id"],
            expected_version=1,
            approved_pages=100,
            idempotency_key="key-pages",
        )
    assert presence.value.code == "idempotency_key_conflict"
    # same key + 不同值 → 409（value 变化）
    with pytest.raises(PlatformError) as value:
        requests.approve(
            actor=approver(),
            request_id=created["id"],
            expected_version=1,
            approved_pages=90,
            idempotency_key="key-pages",
        )
    assert value.value.code == "idempotency_key_conflict"
    # 非法 0/-1：reserve 前 422 validation_error（typed-tag 保证与 None 不碰撞）
    for bad in (0, -1):
        with pytest.raises(PlatformError) as invalid:
            requests.approve(
                actor=approver(),
                request_id=created["id"],
                expected_version=1,
                approved_pages=bad,
                idempotency_key="key-pages",
            )
        assert invalid.value.code == "validation_error"
        assert invalid.value.status_code == 422
    # 0/-1 尝试零残留：credit/outbox 仍各 1 行，无幂等行新增（reserve 前 422）
    assert count_rows(engine, quota_debit_table) == 1
    assert count_rows(engine, tests_outbox_enqueued) == 1
    assert count_rows(engine, platform_idempotency_table) == 2  # create + approve
    # 同 key 同事实（None）重放仍可用
    replay = requests.approve(
        actor=approver(),
        request_id=created["id"],
        expected_version=1,
        approved_pages=None,
        idempotency_key="key-pages",
    )
    assert replay == first


def test_approve_replay_has_no_duplicate_side_effects() -> None:
    """review：同 key 同事实 replay 后 credit/projection/audit/outbox/request 版本
    均无重复变化。"""
    engine, quota, calendar, _times = make_service()
    seed_identity(engine)
    requests = QuotaRequestService(
        engine, MutableClock(_times), calendar, quota, RecordingOutboxPort()
    )
    created = create_pending(engine, requests, 100)
    requests.approve(
        actor=approver(),
        request_id=created["id"],
        expected_version=1,
        approved_pages=80,
        idempotency_key="key-replay",
    )
    replay = requests.approve(
        actor=approver(),
        request_id=created["id"],
        expected_version=1,
        approved_pages=80,
        idempotency_key="key-replay",
    )
    assert replay["version"] == 2
    assert count_rows(engine, quota_debit_table) == 1  # 唯一 credit
    assert count_rows(engine, tests_outbox_enqueued) == 1  # 唯一 outbox
    assert count_rows(engine, platform_audit_table) == 1  # 唯一 audit
    with engine.connect() as connection:
        row = (
            connection.execute(
                select(quota_request_table).where(
                    quota_request_table.c.quota_request_id == created["id"]
                )
            )
            .mappings()
            .one()
        )
        assert row["version"] == 2  # 不二次 +1
        assert row["status"] == "approved"
        proj = (
            connection.execute(
                select(quota_projection_table).where(
                    and_(
                        quota_projection_table.c.quota_subject_user_id == "u1",
                        quota_projection_table.c.quota_period == "2026-08",
                    )
                )
            )
            .mappings()
            .one()
        )
        assert proj["extra_granted"] == 80  # 不二次累加


def test_approve_single_clock_read_at_month_boundary() -> None:
    """review：审批事务只读取一次 clock——period 校验与 reviewed_at/audit/credit/
    projection.updated_at 同一 now。旧多读实现确定失败：period 校验读 AUG_END 通过
    旧月校验、reviewed_at 读 SEP_START 写入新月、投影隐式读取写入 SEP_START。

    SequenceClock 消费区分（review 第二轮）：TxManager 的 reserve/commit_idempotency
    为幂等元数据（created_at/completed_at）读取 clock，属**合法元数据读取**（无业务
    语义）；domain now 只在 `_require_approvable` 消费 1 次（AUG_END）。序列
    [NOW, AUG_END, SEP_START, NOW]：reserve=NOW、业务 now=AUG_END、commit=SEP_START
    （第 4 值越界回退不再消费）。断言：reviewed_at/updated_at/audit occurred_at/
    projection.updated_at 全 == AUG_END——旧实现（reviewed_at/投影各隐式二次读取
    SEP_START）确定失败；若未来 QuotaService 投影路径隐式读取 clock，投影 updated_at
    断言同样捕获（写 SEP_START/NOW 而非 AUG_END）。
    """
    engine, quota, calendar, _times = make_service(now=NOW)
    seed_identity(engine)
    create_requests = QuotaRequestService(
        engine, MutableClock([NOW]), calendar, quota, RecordingOutboxPort()
    )
    created = create_pending(engine, create_requests, 100)
    assert created["quota_period"] == "2026-08"
    requests = QuotaRequestService(
        engine,
        SequenceClock([NOW, AUG_END, SEP_START, NOW]),
        calendar,
        quota,
        RecordingOutboxPort(),
    )
    result = requests.approve(
        actor=approver(),
        request_id=created["id"],
        expected_version=1,
        approved_pages=80,
        idempotency_key="approve-boundary",
    )
    assert result["quota_period"] == "2026-08"
    with engine.connect() as connection:
        row = (
            connection.execute(
                select(quota_request_table).where(
                    quota_request_table.c.quota_request_id == created["id"]
                )
            )
            .mappings()
            .one()
        )
        # 单一 now：reviewed_at 与 updated_at 都是 AUG_END（旧多读实现会写入
        # SEP_START 而失败）
        assert row["reviewed_at_utc"].replace(tzinfo=UTC) == AUG_END
        assert row["updated_at_utc"].replace(tzinfo=UTC) == AUG_END
        credit = (
            connection.execute(
                select(quota_debit_table).where(quota_debit_table.c.entry_kind == "credit")
            )
            .mappings()
            .one()
        )
        assert credit["effective_period"] == "2026-08"
        assert credit["recorded_period"] == "2026-08"
        # 显式 projection timestamp（review 第二轮）：append_credit 传入同一审批业务
        # now，投影 updated_at == AUG_END——旧实现 `_update_projection_locked` 隐式
        # 二次读取 clock（SEP_START）而失败。
        proj = (
            connection.execute(
                select(quota_projection_table).where(
                    and_(
                        quota_projection_table.c.quota_subject_user_id == "u1",
                        quota_projection_table.c.quota_period == "2026-08",
                    )
                )
            )
            .mappings()
            .one()
        )
        assert proj["updated_at_utc"].replace(tzinfo=UTC) == AUG_END
        audit = connection.execute(select(platform_audit_table)).mappings().one()
        assert audit["occurred_at_utc"].replace(tzinfo=UTC) == AUG_END


def test_reject_single_clock_read_at_month_boundary() -> None:
    """review：reject 同样只读一次 clock——关闭月边界内 reject 成功且 reviewed_at 与
    period 校验同一 now（旧多读实现把 reviewed_at 写入 SEP_START 而失败）。"""
    engine, quota, calendar, _times = make_service(now=NOW)
    seed_identity(engine)
    create_requests = QuotaRequestService(
        engine, MutableClock([NOW]), calendar, quota, RecordingOutboxPort()
    )
    created = create_pending(engine, create_requests, 100)
    requests = QuotaRequestService(
        engine,
        SequenceClock([NOW, AUG_END, SEP_START, NOW]),
        calendar,
        quota,
        RecordingOutboxPort(),
    )
    result = requests.reject(
        actor=approver(),
        request_id=created["id"],
        expected_version=1,
        idempotency_key="reject-boundary",
    )
    assert result["status"] == "rejected"
    with engine.connect() as connection:
        row = (
            connection.execute(
                select(quota_request_table).where(
                    quota_request_table.c.quota_request_id == created["id"]
                )
            )
            .mappings()
            .one()
        )
        assert row["reviewed_at_utc"].replace(tzinfo=UTC) == AUG_END
        assert row["updated_at_utc"].replace(tzinfo=UTC) == AUG_END


def test_reject_cannot_cross_closed_month() -> None:
    """review：reject 越过关闭月（业务 now 在 2026-09）→ 统一 409
    quota_request_not_approvable，零副作用。"""
    engine, quota, calendar, _times = make_service(now=NOW)
    seed_identity(engine)
    create_requests = QuotaRequestService(
        engine, MutableClock([NOW]), calendar, quota, RecordingOutboxPort()
    )
    created = create_pending(engine, create_requests, 100)
    requests = QuotaRequestService(
        engine, SequenceClock([NOW, SEPT, NOW]), calendar, quota, RecordingOutboxPort()
    )
    with pytest.raises(PlatformError) as closed:
        requests.reject(
            actor=approver(),
            request_id=created["id"],
            expected_version=1,
            idempotency_key="reject-closed",
        )
    assert closed.value.code == "quota_request_not_approvable"
    assert count_rows(engine, tests_outbox_enqueued) == 0
    assert count_rows(engine, platform_audit_table) == 0
    with engine.connect() as connection:
        row = (
            connection.execute(
                select(quota_request_table).where(
                    quota_request_table.c.quota_request_id == created["id"]
                )
            )
            .mappings()
            .one()
        )
        assert row["status"] == "pending"
        assert row["version"] == 1


def test_approve_approved_pages_over_requested_rejected() -> None:
    """review：approved_pages 动态上限（requested=100、approved=101）→ 422，事务/
    幂等 reserve 无残留副作用。"""
    engine, quota, calendar, _times = make_service()
    seed_identity(engine)
    requests = QuotaRequestService(
        engine, MutableClock(_times), calendar, quota, RecordingOutboxPort()
    )
    created = create_pending(engine, requests, 100)
    with pytest.raises(PlatformError) as over:
        requests.approve(
            actor=approver(),
            request_id=created["id"],
            expected_version=1,
            approved_pages=101,
            idempotency_key="approve-over",
        )
    assert over.value.code == "validation_error"
    assert over.value.status_code == 422
    # 零残留：request 仍 pending/version 1、无 credit/outbox/audit、无幂等行
    with engine.connect() as connection:
        row = (
            connection.execute(
                select(quota_request_table).where(
                    quota_request_table.c.quota_request_id == created["id"]
                )
            )
            .mappings()
            .one()
        )
        assert row["status"] == "pending"
        assert row["version"] == 1
    assert count_rows(engine, quota_debit_table) == 0
    assert count_rows(engine, tests_outbox_enqueued) == 0
    assert count_rows(engine, platform_audit_table) == 0
    assert count_rows(engine, platform_idempotency_table) == 1  # 只有 create 行
    # 合法值仍可审批（无 failed transaction 残留）
    ok = requests.approve(
        actor=approver(),
        request_id=created["id"],
        expected_version=1,
        approved_pages=100,
        idempotency_key="approve-ok-after-over",
    )
    assert ok["status"] == "approved"
    assert ok["approved_pages"] == 100


def test_summary_minister_zero_and_status_listing() -> None:
    """review：minister summary 0/0（spec L50）；approved/rejected/cancelled 列表
    与 reviewed_at RFC3339；list 前后 projection 行数不变（读路径无副作用）。"""
    engine, quota, calendar, times = make_service()
    seed_identity(engine)
    requests = QuotaRequestService(
        engine, MutableClock(times), calendar, quota, RecordingOutboxPort()
    )
    assert requests.summary(actor=ministrator()) == {"quota_pending": 0, "submission_pending": 0}
    # approved / rejected：真实审批产生 reviewed_at
    p1 = create_pending(engine, requests, 100, uid="u1")
    requests.approve(
        actor=approver(),
        request_id=p1["id"],
        expected_version=1,
        approved_pages=80,
        idempotency_key="list-approve",
    )
    times[0] = NOW + timedelta(minutes=1)
    p2 = create_pending(engine, requests, 50, uid="u2")
    requests.reject(
        actor=approver(),
        request_id=p2["id"],
        expected_version=1,
        idempotency_key="list-reject",
    )
    # cancelled：受控 seed 走内部 _cancel_transition（不开放申请人取消 API）
    with engine.begin() as connection:
        p3 = (
            connection.execute(
                select(quota_request_table.c.quota_request_id).where(
                    and_(
                        quota_request_table.c.applicant_user_id == "u1",
                        quota_request_table.c.status == "pending",
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
    assert p3 is None  # u1 的 pending 已被 approve
    times[0] = NOW + timedelta(minutes=2)
    p4 = create_pending(engine, requests, 30, uid="ghost")
    with engine.begin() as connection:
        affected = requests._cancel_transition(
            connection, request_id=p4["id"], reason="review_cancel", now=NOW
        )
    assert affected == 1
    # list 调用前后 projection 行数不变（read_snapshot 缺投影不建行——审批写路径
    # 已建 u1 的投影行，list 只读不得新增/修改）
    before = count_rows(engine, quota_projection_table)
    _ = requests.list_quota_requests(actor=approver(), status="pending")
    after = count_rows(engine, quota_projection_table)
    assert before == after
    approved_items = requests.list_quota_requests(actor=approver(), status="approved")
    assert len(approved_items) == 1
    assert approved_items[0]["id"] == p1["id"]
    assert approved_items[0]["status"] == "approved"
    assert approved_items[0]["approved_pages"] == 80
    assert approved_items[0]["reviewed_at"].endswith("+00:00")
    rejected_items = requests.list_quota_requests(actor=approver(), status="rejected")
    assert len(rejected_items) == 1
    assert rejected_items[0]["id"] == p2["id"]
    assert rejected_items[0]["status"] == "rejected"
    assert rejected_items[0]["reviewed_at"].endswith("+00:00")
    cancelled_items = requests.list_quota_requests(actor=approver(), status="cancelled")
    assert len(cancelled_items) == 1
    assert cancelled_items[0]["id"] == p4["id"]
    assert cancelled_items[0]["status"] == "cancelled"
    assert cancelled_items[0]["reviewed_at"].endswith("+00:00")
    # cancelled 行 approved_pages 为 None；保持 created_at 升序/ghost 回落
    assert cancelled_items[0]["approved_pages"] is None
    assert cancelled_items[0]["applicant"]["display_name"] == "ghost"
    assert approved_items[0]["created_at"] < rejected_items[0]["created_at"]
