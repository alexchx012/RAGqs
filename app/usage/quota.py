"""QuotaService：产品页额度账本（debit/reversal/supplement/credit）与投影（Task 7）。

语义（正式 spec §2/§3 + Task 7 约束 + review 修订，旧 brief 示例的陷阱已审计修正）：
- quota_debit 是独立、追加式的产品页额度账本，不是成本或调用次数事实。entry_kind
  仅为 debit/reversal/supplement/credit；原始 debit 必为正，追加额度 credit 为负。
- 原始 debit 对非空 quota_operation_id 条件唯一；reversal/supplement/credit 以
  (entry_kind, adjustment_source_namespace, adjustment_source_id) 唯一。幂等优先级
  （与 UsageLedger._insert_usage_once / Task 6 review R2 一致）：先按唯一键回读既有
  行——同 fingerprint 复用 persisted ID（重放，绝不二次更新投影），异 fingerprint
  409 ledger_invariant_conflict（稳定 invariant，不让 raw IntegrityError 泄漏）。
- fingerprint 覆盖全部稳定事实（review #1）：debit 含 operation/publication/pages/
  ownership/subject/effective_at/effective_period/effective_calendar_version_id；
  reversal/supplement/credit 含 ownership（+referenced/pages/source 或
  subject/period/pages/source）。recorded_at/created_at 等重放会变化的记录时点不进入
  fingerprint。
- reversal 只引用原始 debit：累计反转 abs(sum(page_delta)) + pages 不得超过原 debit，
  超限 409 ledger_invariant_conflict；累计校验前 SELECT ... FOR UPDATE 锁被引用 debit
  行，把并发 reversal 按引用行串行化（PG 行锁 / SQLite 写锁）。reversal/supplement
  的 effective_calendar_version_id/effective_at/effective_period 直接继承被引用 debit
  （不按调用方 calendar_lock 重算），recorded_* 才用当前 calendar_lock + now（review #4）。
- 投影 (quota_subject_user_id, quota_period)：写路径建行 → 锁投影行 → 原子表达式
  UPDATE；幂等重放不得二次更新。read_snapshot 缺投影返回基线且不建行；rebuild 使用
  调用方 connection（不自行开事务），先确保投影行存在并 SELECT ... FOR UPDATE 锁定
  投影，再读取/汇总 ledger，最后覆盖投影（review #3：并发 append 在投影 UPDATE 上
  等待，其增量在 rebuild 提交后原子叠加，不丢失）。
- 任何 entry 的 ownership.quota_subject_user_id 必须与目标 subject 一致（debit/credit
  对显式 subject，reversal/supplement 对被引用 debit subject），冲突稳定 422
  （review #4）；credit 的 adjustment_source_namespace 服务层固定为 quota_request
  （review #6）；replay_generation 严格为非 bool 非负整数（review #5）；credit 的当前
  业务月校验只在首次插入路径执行（review #2）。
- 豁免（quota_exempt_reason / unlimited role / replay_generation>0）不产生 debit。
  quota_exempt_reason 仅可为 None 或精确 shared_library_submission（未知值/空串/
  非字符串在豁免早退前稳定 422，ops/admin/replay 不绕过；review Task8 #1）。
- 强入口校验（append_debit / record publication 路径）：全部纯输入校验——文本字段
  （quota_operation_id/publication_id/quota_subject_user_id 非空限长；role 用专用
  `_require_role`：必须 str、非空、<=32 且原值等于 value.strip()，任何前后空白稳定
  422，不做 strip 规范化）、pages 正整数、
  replay_generation 严格非 bool 非负整数、exempt reason 精确值、OwnershipSnapshot/
  CalendarLock/datetime 运行时 isinstance、ownership 必填字段、ownership subject 与
  显式 subject 一致——在豁免早退之前全部完成；豁免早退前不访问/写 DB、不取 clock，
  合法豁免参数零 SQL/零写入（review Task8 第一轮 #1/#2 + 第二轮 #1 + 第三轮 role）。
  只允许原始精确 'ops'/'admin' 被 unlimited；其他精确非空 role 保持 fail-safe 有限
  角色（后续权限层另管）。
- check_direct_ingest_balance：直接入库受理门禁，仅检查当前 used+pages 与
  effective_limit（超限 409 quota_exceeded），不预留、不创建账本/投影行、不改余额；
  ops/admin unlimited 放行。
- record_publication_debit：publication 业务终态事务内的 fail-closed debit 端口，显式
  校验 publication_status（与 provider-call 终态无关）；只有 succeeded 委托 append_debit，
  failed/cancelled/dead_letter 返回 None 且不写账。成功路径接收调用方 connection 与冻结的
  calendar_lock/published_at（effective_at_utc=published_at 归期，recorded 用 DB now）；
  同 quota_operation_id 幂等复用且不二次投影，异事实 409。
- check/record 是 DirectIngestGatePort/PublicationDebitPort 的结构化兼容别名
  （method 名与 ports 契约一致，公开的 check_direct_ingest_balance/
  record_publication_debit 方法名保留；record 与 ports 使用同一组精确领域类型）。
- 输入边界稳定 422 validation_error：pages 必须为正整数（拒绝 0/负数/bool）；
  文本字段非空且限长；quota_period 必须为 YYYY-MM 且月份 01..12。
"""

from __future__ import annotations

import logging
import re
import secrets
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol, cast

from sqlalchemy import Engine, and_, func, select, update
from sqlalchemy.engine import Connection, RowMapping

from app.platform.errors import PlatformError

from ._fingerprint import ledger_fingerprint
from ._sql import _insert_do_nothing
from .calendar import BusinessCalendarService, CalendarLock
from .ledger import OwnershipSnapshot, _ownership_json
from .ports import PublicationTerminalStatus
from .schema import quota_debit_table, quota_projection_table

_logger = logging.getLogger(__name__)

_UNLIMITED_ROLES = frozenset({"ops", "admin"})
_EXEMPT_REASON = "shared_library_submission"
_PUBLICATION_TERMINAL_STATUSES = frozenset({"succeeded", "failed", "cancelled", "dead_letter"})
_PERIOD_RE = re.compile(r"^(\d{4})-(\d{2})$")
_CREDIT_NAMESPACE = "quota_request"
_MAX_PAGE_DELTA = 2_147_483_647


class Clock(Protocol):
    def now_utc(self, connection: Connection | None = None) -> datetime: ...


@dataclass(frozen=True, slots=True)
class QuotaSnapshot:
    used: int
    base_limit: int
    extra_granted: int
    effective_limit: int
    unlimited: bool
    reset_at: datetime
    business_timezone: str
    quota_period: str
    business_calendar_version_id: str
    pending_request: dict | None


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _require_text(value: Any, name: str, max_len: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PlatformError("validation_error", f"{name} must be a non-empty string", {}, 422)
    text_value = value.strip()
    if len(text_value) > max_len:
        raise PlatformError(
            "validation_error", f"{name} must be at most {max_len} characters", {}, 422
        )
    return text_value


def _require_role(value: Any) -> str:
    """role 专用校验（review Task8 第三轮）：必须 str、非空、<=32 字符，且原值必须
    等于 value.strip()——任何前后空白（空格/tab/newline/Unicode 常见 whitespace）稳定
    422，不做 strip 规范化。

    与通用 _require_text 不同（后者 strip 后返回规范化值）：role 是语义标识符，前后
    空白会使 ' ops '/'ops ' 被规范化后命中 unlimited 判定而绕过豁免门禁；因此原值含
    前后空白一律 422。只允许原始精确 'ops'/'admin' 被 unlimited；其他精确非空 role
    保持 fail-safe 有限角色（后续权限层另管）。仍位于豁免早退之前。
    """
    if not isinstance(value, str):
        raise PlatformError("validation_error", "role must be a string", {}, 422)
    if not value:
        raise PlatformError("validation_error", "role must be a non-empty string", {}, 422)
    if len(value) > 32:
        raise PlatformError("validation_error", "role must be at most 32 characters", {}, 422)
    if value != value.strip():
        raise PlatformError(
            "validation_error",
            "role must not have leading or trailing whitespace",
            {"role": value},
            422,
        )
    return value


def _require_positive_pages(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= _MAX_PAGE_DELTA:
        raise PlatformError(
            "validation_error",
            "pages must be a positive integer within the quota ledger range",
            {},
            422,
        )
    return value


def _require_publication_status(value: Any) -> PublicationTerminalStatus:
    if not isinstance(value, str) or value not in _PUBLICATION_TERMINAL_STATUSES:
        raise PlatformError(
            "validation_error",
            "publication_status must be succeeded, failed, cancelled, or dead_letter",
            {},
            422,
        )
    return cast(PublicationTerminalStatus, value)


def _require_replay_generation(value: Any) -> int:
    """replay_generation 严格为非 bool 非负整数（review #5）：True/False 一律 422。"""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise PlatformError(
            "validation_error",
            "replay_generation must be a non-negative integer",
            {},
            422,
        )
    return value


def _require_period(value: Any) -> str:
    text_value = _require_text(value, "quota_period", 7)
    match = _PERIOD_RE.fullmatch(text_value)
    if match is None or not 1 <= int(match.group(2)) <= 12:
        raise PlatformError(
            "validation_error",
            "quota_period must be a YYYY-MM business month with month 01..12",
            {},
            422,
        )
    return text_value


def _require_exempt_reason(value: Any) -> str | None:
    """quota_exempt_reason 仅可为 None 或精确 shared_library_submission（review Task8 #1）。

    未知值/空串/非字符串在豁免早退前稳定 422——ops/admin/replay_generation 不得绕过
    输入错误（spec §3：共享库投稿豁免是唯一豁免理由）。
    """
    if value is None:
        return None
    if not isinstance(value, str) or value != _EXEMPT_REASON:
        raise PlatformError(
            "validation_error",
            f"quota_exempt_reason must be None or '{_EXEMPT_REASON}'",
            {"quota_exempt_reason": value},
            422,
        )
    return value


def _require_ownership(value: Any) -> OwnershipSnapshot:
    """运行时 isinstance 校验强入口类型（review Task8 #2）：非法类型稳定 422。"""
    if not isinstance(value, OwnershipSnapshot):
        raise PlatformError(
            "validation_error",
            "ownership must be an OwnershipSnapshot",
            {},
            422,
        )
    return value


def _require_calendar_lock(value: Any) -> CalendarLock:
    """运行时 isinstance 校验强入口类型（review Task8 #2）：非法类型稳定 422。"""
    if not isinstance(value, CalendarLock):
        raise PlatformError(
            "validation_error",
            "calendar_lock must be a CalendarLock",
            {},
            422,
        )
    return value


def _require_effective_at(value: Any) -> datetime:
    """运行时 isinstance 校验强入口类型（review Task8 #2）：非法类型稳定 422。"""
    if not isinstance(value, datetime):
        raise PlatformError(
            "validation_error",
            "effective_at_utc must be a datetime",
            {},
            422,
        )
    return value


class QuotaService:
    def __init__(
        self,
        engine: Engine,
        clock: Clock,
        calendar: BusinessCalendarService,
        base_limit: int = 500,
        invariant_alert_port: Any | None = None,
    ) -> None:
        self._engine = engine
        self._clock = clock
        self.calendar = calendar
        self._base_limit = base_limit
        self._invariant_alert_port = invariant_alert_port

    @staticmethod
    def unlimited_role(role: str) -> bool:
        return role in _UNLIMITED_ROLES

    @staticmethod
    def _entry_fingerprint(kind: str, payload: dict) -> str:
        return ledger_fingerprint(f"quota_{kind}", payload)

    @staticmethod
    def _validate_ownership(ownership: OwnershipSnapshot) -> None:
        """ownership 必填字段校验（review Task8 第二轮 #3：恢复精确 OwnershipSnapshot 类型）。

        append_debit 中由 `_require_ownership` 的 isinstance 校验先行保证类型；reversal/
        supplement/credit 入口自身签名即精确类型。不使用 Any 掩盖。
        """
        _require_text(ownership.actor_user_id, "actor_user_id", 64)
        _require_text(ownership.cost_center_key, "cost_center_key", 128)
        if ownership.actor_role_snapshot is not None:
            _require_text(ownership.actor_role_snapshot, "actor_role_snapshot", 32)
        if ownership.actor_department_id_snapshot is not None:
            _require_text(
                ownership.actor_department_id_snapshot, "actor_department_id_snapshot", 64
            )
        if ownership.quota_subject_user_id is not None:
            _require_text(ownership.quota_subject_user_id, "quota_subject_user_id", 64)

    def _alert_usage_invariant_conflict(self, exc: PlatformError) -> None:
        """分录唯一键异指纹冲突的回滚后 best-effort ops 告警（usage ledger A45 同型）。

        只能在调用方事务边界（事务已回滚、连接已归还）之后调用：adapter 用独立
        短事务发布，失败仅记日志——绝不掩盖原 409。只对 {"index": [...]} 形状的
        指纹冲突告警；累计超量反转等其余 409 无 index，不发。
        """
        if self._invariant_alert_port is None or exc.code != "ledger_invariant_conflict":
            return
        index = exc.details.get("index")
        if not isinstance(index, list) or not index:
            return
        try:
            self._invariant_alert_port.publish_usage_ledger_invariant_conflict(
                unique_key_fields=[str(name) for name in index]
            )
        except Exception:
            _logger.warning("quota invariant alert could not be published", exc_info=True)

    def publish_invariant_alert(self, exc: PlatformError) -> None:
        """caller-owned 事务路径回滚后的公开告警入口（documents 发布 debit、审批 credit）。"""
        self._alert_usage_invariant_conflict(exc)

    def _existing_entry(
        self,
        connection: Connection,
        *,
        unique_where: Any,
        entry_fingerprint: str,
        conflict_index: list[str],
    ) -> str | None:
        """按唯一键回读既有行：同 fingerprint 复用 persisted ID；异 fingerprint 409。"""
        existing = (
            connection.execute(select(quota_debit_table).where(unique_where))
            .mappings()
            .one_or_none()
        )
        if existing is None:
            return None
        if existing["entry_fingerprint"] != entry_fingerprint:
            raise PlatformError(
                "ledger_invariant_conflict",
                "Quota entry fingerprint does not match the existing ledger row",
                {"index": list(conflict_index)},
                409,
            )
        return str(existing["quota_debit_id"])

    def _insert_entry(
        self,
        connection: Connection,
        *,
        values: dict[str, Any],
        conflict_index: list[str],
        unique_where: Any,
    ) -> tuple[str, bool]:
        """条件唯一幂等插入；返回 (persisted_id, inserted_new)。

        幂等优先级（与 UsageLedger._insert_usage_once 一致）：先按唯一键回读既有行，
        同 fingerprint 复用（inserted=False，调用方不得再更新投影），异 fingerprint
        409；无既有行才 insert-do-nothing，并发竞态（insert 0 行）回读比对，同指纹
        复用、异指纹 409。
        """
        entry_fingerprint = values["entry_fingerprint"]
        existing_id = self._existing_entry(
            connection,
            unique_where=unique_where,
            entry_fingerprint=entry_fingerprint,
            conflict_index=conflict_index,
        )
        if existing_id is not None:
            return existing_id, False
        entry_id = str(values["quota_debit_id"])
        inserted = _insert_do_nothing(connection, quota_debit_table, values, conflict_index)
        if inserted:
            return entry_id, True
        existing_id = self._existing_entry(
            connection,
            unique_where=unique_where,
            entry_fingerprint=entry_fingerprint,
            conflict_index=conflict_index,
        )
        if existing_id is None:
            raise PlatformError(
                "ledger_invariant_conflict",
                "Quota entry insert race lost; no persisted row to reuse",
                {"index": list(conflict_index)},
                409,
            )
        return existing_id, False

    def _base_entry_values(
        self,
        *,
        entry_kind: str,
        page_delta: int,
        ownership: OwnershipSnapshot,
        calendar_lock: CalendarLock,
        effective_at_utc: datetime,
        recorded_at_utc: datetime,
        fingerprint_payload: dict,
        effective_calendar_version_id: str | None = None,
        effective_period: str | None = None,
    ) -> dict[str, Any]:
        """分录基础值；effective 事实可选直接继承（reversal/supplement 用被引用 debit
        的 facts，不按调用方 calendar_lock 重算，review #4）；recorded_* 始终用当前
        calendar_lock + recorded_at_utc。"""
        effective = _utc(effective_at_utc)
        recorded = _utc(recorded_at_utc)
        return {
            "entry_kind": entry_kind,
            "page_delta": page_delta,
            "entry_fingerprint": self._entry_fingerprint(entry_kind, fingerprint_payload),
            "cost_center_key": ownership.cost_center_key,
            "ownership_json": _ownership_json(ownership),
            "effective_calendar_version_id": (
                effective_calendar_version_id or calendar_lock.version_id
            ),
            "effective_at_utc": effective,
            "effective_period": (
                effective_period or self.calendar.period_for(calendar_lock, effective)
            ),
            "recorded_calendar_version_id": calendar_lock.version_id,
            "recorded_at_utc": recorded,
            "recorded_period": self.calendar.period_for(calendar_lock, recorded),
            "created_at_utc": recorded,
        }

    def _lock_projection(
        self,
        connection: Connection,
        *,
        quota_subject_user_id: str,
        quota_period: str,
        now: datetime,
    ) -> dict[str, Any]:
        """确保投影行存在并持有写路径的行锁。"""
        _insert_do_nothing(
            connection,
            quota_projection_table,
            {
                "quota_subject_user_id": quota_subject_user_id,
                "quota_period": quota_period,
                "base_limit": self._base_limit,
                "extra_granted": 0,
                "used": 0,
                "last_debit_id": None,
                "updated_at_utc": now,
            },
            ["quota_subject_user_id", "quota_period"],
        )
        return dict(
            connection.execute(
                select(quota_projection_table)
                .where(
                    and_(
                        quota_projection_table.c.quota_subject_user_id == quota_subject_user_id,
                        quota_projection_table.c.quota_period == quota_period,
                    )
                )
                .with_for_update()
            )
            .mappings()
            .one()
        )

    def _update_projection_locked(
        self,
        connection: Connection,
        *,
        quota_subject_user_id: str,
        quota_period: str,
        debit_delta: int = 0,
        credit_delta: int = 0,
        last_debit_id: str | None = None,
        updated_at_utc: datetime | None = None,
    ) -> None:
        """写路径投影更新：① 建行 → ② 锁投影行 → ③ 原子表达式 UPDATE。

        调用方持有调用方 connection 上的事务；本方法不自行开事务。幂等重放
        （inserted=False）不调用本方法，因此同一事实绝不二次累加。
        `updated_at_utc`（review 第二轮）：调用方已读取的 recorded 业务 now——
        append_debit/reversal/supplement/credit 均已持有该 now，显式传入后本方法
        **不再自行读取 clock**（消除写路径的隐式二次 clock read，审批月界一致性：
        projection.updated_at 与 entry recorded/request reviewed_at/audit 同一
        业务时点）；None 时回退 `self._clock.now_utc(connection)`（兼容 Task 7 外部
        调用方缺省路径）。数值/锁序/调用方事务语义不变。
        """
        now = updated_at_utc if updated_at_utc is not None else self._clock.now_utc(connection)
        self._lock_projection(
            connection,
            quota_subject_user_id=quota_subject_user_id,
            quota_period=quota_period,
            now=now,
        )
        connection.execute(
            update(quota_projection_table)
            .where(
                and_(
                    quota_projection_table.c.quota_subject_user_id == quota_subject_user_id,
                    quota_projection_table.c.quota_period == quota_period,
                )
            )
            .values(
                base_limit=self._base_limit,
                extra_granted=quota_projection_table.c.extra_granted + credit_delta,
                used=quota_projection_table.c.used + debit_delta,
                last_debit_id=(
                    last_debit_id
                    if last_debit_id is not None
                    else quota_projection_table.c.last_debit_id
                ),
                updated_at_utc=now,
            )
        )

    def append_debit(
        self,
        connection: Connection,
        *,
        quota_operation_id: str,
        publication_id: str,
        quota_subject_user_id: str,
        pages: int,
        ownership: OwnershipSnapshot,
        calendar_lock: CalendarLock,
        role: str,
        effective_at_utc: datetime,
        quota_exempt_reason: str | None = None,
        replay_generation: int = 0,
    ) -> str | None:
        """原始 debit：豁免 → None；quota_operation_id 条件唯一 + fingerprint 幂等。

        全部纯输入校验在豁免早退之前完成（review Task8 第二轮 #1 + 第三轮 role）：
        replay_generation 严格非 bool 非负整数、exempt reason 仅 None/精确
        shared_library_submission、ownership/calendar_lock/effective_at_utc 运行时
        isinstance、文本字段（quota_operation_id/publication_id/quota_subject_user_id
        非空限长；role 用专用 `_require_role`——str/非空/<=32/原值无前后空白，不做
        strip 规范化）、pages 正整数、ownership 必填字段、ownership.quota_subject_user_id
        与显式 subject 一致——非法输入稳定 422，不泄漏 AttributeError/TypeError，且
        ops/admin/replay 豁免不绕过输入错误（含 ' ops '/'ops ' 等带空白 role 不因
        unlimited 判定豁免）。豁免（合法理由 / unlimited role / replay_generation>0）
        → None：早退前不访问/写 DB、不取 clock，零 SQL/零写入。
        fingerprint 含全部稳定事实（subject/effective facts，review #1）；
        新增（inserted=True）才更新投影。
        """
        replay_generation = _require_replay_generation(replay_generation)
        quota_exempt_reason = _require_exempt_reason(quota_exempt_reason)
        ownership = _require_ownership(ownership)
        calendar_lock = _require_calendar_lock(calendar_lock)
        effective_at_utc = _require_effective_at(effective_at_utc)
        quota_operation_id = _require_text(quota_operation_id, "quota_operation_id", 128)
        publication_id = _require_text(publication_id, "publication_id", 128)
        quota_subject_user_id = _require_text(quota_subject_user_id, "quota_subject_user_id", 64)
        role = _require_role(role)
        pages = _require_positive_pages(pages)
        self._validate_ownership(ownership)
        if ownership.quota_subject_user_id != quota_subject_user_id:
            raise PlatformError(
                "validation_error",
                "ownership.quota_subject_user_id must match the debit subject",
                {"quota_subject_user_id": quota_subject_user_id},
                422,
            )
        if quota_exempt_reason is not None or self.unlimited_role(role) or replay_generation > 0:
            return None
        now = self._clock.now_utc(connection)
        effective = _utc(effective_at_utc)
        period = self.calendar.period_for(calendar_lock, effective)
        base = self._base_entry_values(
            entry_kind="debit",
            page_delta=pages,
            ownership=ownership,
            calendar_lock=calendar_lock,
            effective_at_utc=effective_at_utc,
            recorded_at_utc=now,
            fingerprint_payload={
                "operation": quota_operation_id,
                "publication_id": publication_id,
                "pages": pages,
                "ownership": _ownership_json(ownership),
                "subject": quota_subject_user_id,
                "effective_at_utc": effective,
                "effective_period": period,
                "effective_calendar_version_id": calendar_lock.version_id,
            },
        )
        values = {
            "quota_debit_id": f"qd_{secrets.token_urlsafe(9)}",
            **base,
            "quota_operation_id": quota_operation_id,
            "publication_id": publication_id,
            "quota_subject_user_id": quota_subject_user_id,
            "quota_period": period,
            "quota_exempt_reason": quota_exempt_reason,
        }
        unique_where = quota_debit_table.c.quota_operation_id == quota_operation_id
        existing_id = self._existing_entry(
            connection,
            unique_where=unique_where,
            entry_fingerprint=str(values["entry_fingerprint"]),
            conflict_index=["quota_operation_id"],
        )
        if existing_id is not None:
            return existing_id
        self._lock_projection(
            connection,
            quota_subject_user_id=quota_subject_user_id,
            quota_period=period,
            now=now,
        )
        # A same-operation concurrent writer may have committed while this call waited for the
        # projection lock. Replays must return the persisted debit instead of failing quota check.
        existing_id = self._existing_entry(
            connection,
            unique_where=unique_where,
            entry_fingerprint=str(values["entry_fingerprint"]),
            conflict_index=["quota_operation_id"],
        )
        if existing_id is not None:
            return existing_id
        debit_id, inserted = self._insert_entry(
            connection,
            values=values,
            conflict_index=["quota_operation_id"],
            unique_where=unique_where,
        )
        if inserted:
            self._update_projection_locked(
                connection,
                quota_subject_user_id=quota_subject_user_id,
                quota_period=period,
                debit_delta=pages,
                last_debit_id=debit_id,
                updated_at_utc=now,  # 复用本方法已读取的 recorded now（review 第二轮）
            )
        return debit_id

    def record_publication_debit(
        self,
        connection: Connection,
        *,
        publication_status: PublicationTerminalStatus,
        quota_operation_id: str,
        publication_id: str,
        quota_subject_user_id: str,
        pages: int,
        ownership: OwnershipSnapshot,
        calendar_lock: CalendarLock,
        role: str,
        quota_exempt_reason: str | None = None,
        replay_generation: int = 0,
        published_at: datetime,
    ) -> str | None:
        """Fail-closed publication debit boundary; ``published_at`` decides the period.

        `publication_status` is the business publication terminal state, not a provider
        call status. Only `succeeded` reaches append_debit; `failed`, `cancelled`, and
        `dead_letter` return None without touching quota storage. Unknown status values
        are rejected before terminal-state or quota-exemption early returns.

        spec §2/§3：publication 页额度按同一发布事务冻结的 published_at 归期
        （effective_at_utc=published_at，跨业务月整体归期不拆分），recorded 用 DB now
        （append_debit 内部取 clock.now_utc）。同 quota_operation_id 幂等复用且不二次
        投影，异事实 409 ledger_invariant_conflict；shared_library_submission、
        ops/admin、replay_generation>0 豁免返回 None 且不写账本。
        """
        publication_status = _require_publication_status(publication_status)
        if publication_status != "succeeded":
            return None
        return self.append_debit(
            connection,
            quota_operation_id=quota_operation_id,
            publication_id=publication_id,
            quota_subject_user_id=quota_subject_user_id,
            pages=pages,
            ownership=ownership,
            calendar_lock=calendar_lock,
            role=role,
            effective_at_utc=published_at,
            quota_exempt_reason=quota_exempt_reason,
            replay_generation=replay_generation,
        )

    def record(
        self,
        connection: Connection,
        *,
        publication_status: PublicationTerminalStatus,
        quota_operation_id: str,
        publication_id: str,
        quota_subject_user_id: str,
        pages: int,
        ownership: OwnershipSnapshot,
        calendar_lock: CalendarLock,
        role: str,
        quota_exempt_reason: str | None = None,
        replay_generation: int = 0,
        published_at: datetime,
    ) -> str | None:
        """PublicationDebitPort 结构化兼容别名：委托 record_publication_debit。

        与 ports.PublicationDebitPort 使用同一组精确领域类型（publication status /
        OwnershipSnapshot / CalendarLock / datetime）。若运行时收到动态错误值（类型层面
        被绕过），record_publication_debit 的 status 校验或 append_debit 的强入口校验
        给出稳定 PlatformError（422 validation_error），不泄漏 AttributeError/TypeError。
        """
        return self.record_publication_debit(
            connection,
            publication_status=publication_status,
            quota_operation_id=quota_operation_id,
            publication_id=publication_id,
            quota_subject_user_id=quota_subject_user_id,
            pages=pages,
            ownership=ownership,
            calendar_lock=calendar_lock,
            role=role,
            quota_exempt_reason=quota_exempt_reason,
            replay_generation=replay_generation,
            published_at=published_at,
        )

    def _require_referenced_debit(
        self, connection: Connection, referenced_debit_id: str, *, lock: bool
    ) -> dict[str, Any]:
        """回读被引用 debit 行（reversal 用 lock=True 持有行锁串行化累计校验）。"""
        statement = select(quota_debit_table).where(
            quota_debit_table.c.quota_debit_id == referenced_debit_id
        )
        if lock:
            statement = statement.with_for_update()
        ref = connection.execute(statement).mappings().one_or_none()
        if ref is None or ref["entry_kind"] != "debit":
            raise PlatformError("quota_debit_not_found", "Referenced debit was not found", {}, 404)
        return dict(ref)

    def append_reversal(
        self,
        connection: Connection,
        *,
        referenced_debit_id: str,
        pages: int,
        adjustment_source_namespace: str,
        adjustment_source_id: str,
        ownership: OwnershipSnapshot,
        calendar_lock: CalendarLock,
        now: datetime,
    ) -> str:
        """reversal：只引用原始 debit；累计反转不超原 debit；来源键唯一 + 幂等。

        幂等优先级：同来源键既有行先按 fingerprint 判定（重放复用 / 异指纹 409，
        fingerprint 含 ownership，review #1），累计反转校验只影响首次插入路径。
        首次插入路径：SELECT ... FOR UPDATE 锁被引用 debit 行（PG 行锁串行化并发
        reversal），校验 ownership.quota_subject_user_id 与被引用 debit subject 一致
        （review #4），再执行累计校验与插入；effective facts 直接继承被引用 debit
        （review #4），recorded_* 用当前 calendar_lock + now。
        """
        referenced_debit_id = _require_text(referenced_debit_id, "referenced_debit_id", 64)
        pages = _require_positive_pages(pages)
        namespace = _require_text(adjustment_source_namespace, "adjustment_source_namespace", 64)
        source_id = _require_text(adjustment_source_id, "adjustment_source_id", 128)
        self._validate_ownership(ownership)
        fingerprint_payload = {
            "referenced": referenced_debit_id,
            "pages": pages,
            "source": (namespace, source_id),
            "ownership": _ownership_json(ownership),
        }
        unique_where = and_(
            quota_debit_table.c.entry_kind == "reversal",
            quota_debit_table.c.adjustment_source_namespace == namespace,
            quota_debit_table.c.adjustment_source_id == source_id,
        )
        fingerprint = self._entry_fingerprint("reversal", fingerprint_payload)
        existing_id = self._existing_entry(
            connection,
            unique_where=unique_where,
            entry_fingerprint=fingerprint,
            conflict_index=["entry_kind", "adjustment_source_namespace", "adjustment_source_id"],
        )
        if existing_id is not None:
            return existing_id  # 幂等重放：不更新投影
        # 首次插入路径：锁引用行 → 二次查重 → subject 一致 → 累计校验 → 插入。
        ref = self._require_referenced_debit(connection, referenced_debit_id, lock=True)
        existing_id = self._existing_entry(
            connection,
            unique_where=unique_where,
            entry_fingerprint=fingerprint,
            conflict_index=["entry_kind", "adjustment_source_namespace", "adjustment_source_id"],
        )
        if existing_id is not None:
            return existing_id
        if ownership.quota_subject_user_id != ref["quota_subject_user_id"]:
            raise PlatformError(
                "validation_error",
                "ownership.quota_subject_user_id must match the referenced debit subject",
                {"referenced_debit_id": referenced_debit_id},
                422,
            )
        reversed_total = connection.execute(
            select(func.coalesce(func.sum(quota_debit_table.c.page_delta), 0)).where(
                and_(
                    quota_debit_table.c.entry_kind == "reversal",
                    quota_debit_table.c.referenced_debit_id == referenced_debit_id,
                )
            )
        ).scalar_one()
        # sum(page_delta) 为负（reversal 行 page_delta=-pages）；已反转绝对值 + 本次
        # pages 不得超过原 debit。
        if -int(reversed_total) + pages > int(ref["page_delta"]):
            raise PlatformError(
                "ledger_invariant_conflict",
                "Cumulative reversal exceeds the original debit",
                {"referenced_debit_id": referenced_debit_id},
                409,
            )
        base = self._base_entry_values(
            entry_kind="reversal",
            page_delta=-pages,
            ownership=ownership,
            calendar_lock=calendar_lock,
            effective_at_utc=ref["effective_at_utc"],
            recorded_at_utc=now,
            fingerprint_payload=fingerprint_payload,
            effective_calendar_version_id=str(ref["effective_calendar_version_id"]),
            effective_period=str(ref["effective_period"]),
        )
        reversal_id, inserted = self._insert_entry(
            connection,
            values={
                "quota_debit_id": f"qd_{secrets.token_urlsafe(9)}",
                **base,
                "quota_subject_user_id": ref["quota_subject_user_id"],
                "quota_period": ref["quota_period"],
                "referenced_debit_id": referenced_debit_id,
                "adjustment_source_namespace": namespace,
                "adjustment_source_id": source_id,
            },
            conflict_index=["entry_kind", "adjustment_source_namespace", "adjustment_source_id"],
            unique_where=unique_where,
        )
        if inserted:
            self._update_projection_locked(
                connection,
                quota_subject_user_id=str(ref["quota_subject_user_id"]),
                quota_period=str(ref["quota_period"]),
                debit_delta=-pages,
                updated_at_utc=now,  # 复用调用方传入的 recorded now（review 第二轮）
            )
        return reversal_id

    def append_supplement(
        self,
        connection: Connection,
        *,
        referenced_debit_id: str,
        pages: int,
        adjustment_source_namespace: str,
        adjustment_source_id: str,
        ownership: OwnershipSnapshot,
        calendar_lock: CalendarLock,
        now: datetime,
    ) -> str:
        """supplement：只引用原始 debit；page_delta=+pages；来源键唯一 + 幂等。

        fingerprint 含 ownership（review #1）；首次插入路径校验
        ownership.quota_subject_user_id 与被引用 debit subject 一致（review #4）；
        effective facts 直接继承被引用 debit（review #4）。新增才更新投影
        （debit_delta=+pages, last_debit_id=supplement_id）。
        """
        referenced_debit_id = _require_text(referenced_debit_id, "referenced_debit_id", 64)
        pages = _require_positive_pages(pages)
        namespace = _require_text(adjustment_source_namespace, "adjustment_source_namespace", 64)
        source_id = _require_text(adjustment_source_id, "adjustment_source_id", 128)
        self._validate_ownership(ownership)
        fingerprint_payload = {
            "referenced": referenced_debit_id,
            "pages": pages,
            "source": (namespace, source_id),
            "ownership": _ownership_json(ownership),
        }
        unique_where = and_(
            quota_debit_table.c.entry_kind == "supplement",
            quota_debit_table.c.adjustment_source_namespace == namespace,
            quota_debit_table.c.adjustment_source_id == source_id,
        )
        existing_id = self._existing_entry(
            connection,
            unique_where=unique_where,
            entry_fingerprint=self._entry_fingerprint("supplement", fingerprint_payload),
            conflict_index=["entry_kind", "adjustment_source_namespace", "adjustment_source_id"],
        )
        if existing_id is not None:
            return existing_id  # 幂等重放：不更新投影
        ref = self._require_referenced_debit(connection, referenced_debit_id, lock=False)
        if ownership.quota_subject_user_id != ref["quota_subject_user_id"]:
            raise PlatformError(
                "validation_error",
                "ownership.quota_subject_user_id must match the referenced debit subject",
                {"referenced_debit_id": referenced_debit_id},
                422,
            )
        base = self._base_entry_values(
            entry_kind="supplement",
            page_delta=pages,
            ownership=ownership,
            calendar_lock=calendar_lock,
            effective_at_utc=ref["effective_at_utc"],
            recorded_at_utc=now,
            fingerprint_payload=fingerprint_payload,
            effective_calendar_version_id=str(ref["effective_calendar_version_id"]),
            effective_period=str(ref["effective_period"]),
        )
        supplement_id, inserted = self._insert_entry(
            connection,
            values={
                "quota_debit_id": f"qd_{secrets.token_urlsafe(9)}",
                **base,
                "quota_subject_user_id": ref["quota_subject_user_id"],
                "quota_period": ref["quota_period"],
                "referenced_debit_id": referenced_debit_id,
                "adjustment_source_namespace": namespace,
                "adjustment_source_id": source_id,
            },
            conflict_index=["entry_kind", "adjustment_source_namespace", "adjustment_source_id"],
            unique_where=unique_where,
        )
        if inserted:
            self._update_projection_locked(
                connection,
                quota_subject_user_id=str(ref["quota_subject_user_id"]),
                quota_period=str(ref["quota_period"]),
                debit_delta=pages,
                last_debit_id=supplement_id,
                updated_at_utc=now,  # 复用调用方传入的 recorded now（review 第二轮）
            )
        return supplement_id

    def append_credit(
        self,
        connection: Connection,
        *,
        quota_subject_user_id: str,
        quota_period: str,
        pages: int,
        adjustment_source_namespace: str,
        adjustment_source_id: str,
        ownership: OwnershipSnapshot,
        calendar_lock: CalendarLock,
        now: datetime,
    ) -> str:
        """credit：page_delta=-pages（追加额度）；来源键唯一 + 幂等。

        幂等优先级（review #2）：先按 source key + 完整 fingerprint（含 ownership）
        回读既有行——同指纹始终复用 persisted ID，异指纹 409；当前业务月校验
        （spec §5：审批只面向申请人当前业务月）只在首次插入路径执行，跨月同事实
        重放不被误拒。adjustment_source_namespace 服务层固定为 quota_request
        （review #6）；ownership.quota_subject_user_id 必须与显式 subject 一致
        （review #4）。新增才更新投影（credit_delta=+pages）。
        """
        quota_subject_user_id = _require_text(quota_subject_user_id, "quota_subject_user_id", 64)
        quota_period = _require_period(quota_period)
        pages = _require_positive_pages(pages)
        namespace = _require_text(adjustment_source_namespace, "adjustment_source_namespace", 64)
        if namespace != _CREDIT_NAMESPACE:
            raise PlatformError(
                "validation_error",
                "credit adjustment_source_namespace must be 'quota_request'",
                {"adjustment_source_namespace": namespace},
                422,
            )
        source_id = _require_text(adjustment_source_id, "adjustment_source_id", 128)
        self._validate_ownership(ownership)
        if ownership.quota_subject_user_id != quota_subject_user_id:
            raise PlatformError(
                "validation_error",
                "ownership.quota_subject_user_id must match the credit subject",
                {"quota_subject_user_id": quota_subject_user_id},
                422,
            )
        fingerprint_payload = {
            "subject": quota_subject_user_id,
            "period": quota_period,
            "pages": pages,
            "source": (namespace, source_id),
            "ownership": _ownership_json(ownership),
        }
        unique_where = and_(
            quota_debit_table.c.entry_kind == "credit",
            quota_debit_table.c.adjustment_source_namespace == namespace,
            quota_debit_table.c.adjustment_source_id == source_id,
        )
        existing_id = self._existing_entry(
            connection,
            unique_where=unique_where,
            entry_fingerprint=self._entry_fingerprint("credit", fingerprint_payload),
            conflict_index=["entry_kind", "adjustment_source_namespace", "adjustment_source_id"],
        )
        if existing_id is not None:
            return existing_id  # 幂等重放：不更新投影
        # 首次插入路径：校验 quota_period 为当前业务月。
        current_period = self.calendar.period_for(calendar_lock, _utc(now))
        if quota_period != current_period:
            raise PlatformError(
                "validation_error",
                "quota_period must be the current business month",
                {"quota_period": quota_period, "current_period": current_period},
                422,
            )
        base = self._base_entry_values(
            entry_kind="credit",
            page_delta=-pages,
            ownership=ownership,
            calendar_lock=calendar_lock,
            effective_at_utc=now,
            recorded_at_utc=now,
            fingerprint_payload=fingerprint_payload,
        )
        credit_id, inserted = self._insert_entry(
            connection,
            values={
                "quota_debit_id": f"qd_{secrets.token_urlsafe(9)}",
                **base,
                "quota_subject_user_id": quota_subject_user_id,
                "quota_period": quota_period,
                "adjustment_source_namespace": namespace,
                "adjustment_source_id": source_id,
            },
            conflict_index=["entry_kind", "adjustment_source_namespace", "adjustment_source_id"],
            unique_where=unique_where,
        )
        if inserted:
            self._update_projection_locked(
                connection,
                quota_subject_user_id=quota_subject_user_id,
                quota_period=quota_period,
                credit_delta=pages,
                updated_at_utc=now,  # 复用调用方传入的审批业务 now（review 第二轮）
            )
        return credit_id

    def check_direct_ingest_balance(
        self, connection: Connection, *, quota_subject_user_id: str, pages: int, role: str
    ) -> None:
        """直接入库受理门禁：余额不足 409 quota_exceeded；unlimited 放行；不写行。"""
        quota_subject_user_id = _require_text(quota_subject_user_id, "quota_subject_user_id", 64)
        pages = _require_positive_pages(pages)
        if self.unlimited_role(role):
            return
        snapshot = self.read_snapshot(
            connection, quota_subject_user_id=quota_subject_user_id, role=role
        )
        if snapshot.used + pages > snapshot.effective_limit:
            raise PlatformError("quota_exceeded", "Quota limit exceeded", {}, 409)

    def check(
        self, connection: Connection, *, quota_subject_user_id: str, pages: int, role: str
    ) -> None:
        """DirectIngestGatePort 结构化兼容别名：委托 check_direct_ingest_balance。"""
        self.check_direct_ingest_balance(
            connection,
            quota_subject_user_id=quota_subject_user_id,
            pages=pages,
            role=role,
        )

    def read_snapshot(
        self, connection: Connection, *, quota_subject_user_id: str, role: str
    ) -> QuotaSnapshot:
        """读取当前业务月额度快照；缺投影返回基线且不建行；unlimited 固定形状。

        reset_at 由业务时区下月月初产生（前端不得依浏览器时区计算）。
        """
        quota_subject_user_id = _require_text(quota_subject_user_id, "quota_subject_user_id", 64)
        lock = self.calendar.lock_or_verify(connection)
        now = self._clock.now_utc(connection)
        period = self.calendar.period_for(lock, now)
        reset_at = self.calendar.next_month_start_utc(lock, now)
        row = None
        if not self.unlimited_role(role):
            row = (
                connection.execute(
                    select(quota_projection_table).where(
                        and_(
                            quota_projection_table.c.quota_subject_user_id == quota_subject_user_id,
                            quota_projection_table.c.quota_period == period,
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
        return self._snapshot(lock=lock, period=period, reset_at=reset_at, role=role, row=row)

    def read_snapshots(
        self,
        connection: Connection,
        *,
        entries: Sequence[tuple[str, str]],
    ) -> dict[tuple[str, str], QuotaSnapshot]:
        """批量读取多个 (subject, role) 的当前业务月快照。

        与逐个 read_snapshot 结果一致：共享一次 calendar 校验/时钟/期间，
        投影一次 in_ 预取，供 ops 列表页避免逐行 snapshot 查询。
        """
        normalized = [
            (_require_text(quota_subject_user_id, "quota_subject_user_id", 64), role)
            for quota_subject_user_id, role in entries
        ]
        lock = self.calendar.lock_or_verify(connection)
        now = self._clock.now_utc(connection)
        period = self.calendar.period_for(lock, now)
        reset_at = self.calendar.next_month_start_utc(lock, now)
        subjects = {
            quota_subject_user_id
            for quota_subject_user_id, role in normalized
            if not self.unlimited_role(role)
        }
        projections: dict[str, RowMapping] = {}
        if subjects:
            rows = (
                connection.execute(
                    select(quota_projection_table).where(
                        and_(
                            quota_projection_table.c.quota_subject_user_id.in_(subjects),
                            quota_projection_table.c.quota_period == period,
                        )
                    )
                )
                .mappings()
                .all()
            )
            projections = {str(row["quota_subject_user_id"]): row for row in rows}
        return {
            (quota_subject_user_id, role): self._snapshot(
                lock=lock,
                period=period,
                reset_at=reset_at,
                role=role,
                row=projections.get(quota_subject_user_id),
            )
            for quota_subject_user_id, role in normalized
        }

    def _snapshot(
        self,
        *,
        lock: CalendarLock,
        period: str,
        reset_at: datetime,
        role: str,
        row: RowMapping | None,
    ) -> QuotaSnapshot:
        if self.unlimited_role(role):
            return QuotaSnapshot(
                used=0,
                base_limit=self._base_limit,
                extra_granted=0,
                effective_limit=0,
                unlimited=True,
                reset_at=reset_at,
                business_timezone=lock.timezone,
                quota_period=period,
                business_calendar_version_id=lock.version_id,
                pending_request=None,
            )
        used = int(cast(Any, row["used"])) if row is not None else 0
        extra = int(cast(Any, row["extra_granted"])) if row is not None else 0
        return QuotaSnapshot(
            used=used,
            base_limit=self._base_limit,
            extra_granted=extra,
            effective_limit=self._base_limit + extra,
            unlimited=False,
            reset_at=reset_at,
            business_timezone=lock.timezone,
            quota_period=period,
            business_calendar_version_id=lock.version_id,
            pending_request=None,
        )

    def rebuild_projection(
        self, connection: Connection, *, quota_subject_user_id: str, quota_period: str
    ) -> None:
        """按 quota_debit 全量重放重建投影；使用调用方 connection（不自行开事务）。

        锁序（review #3）：先 `_insert_do_nothing` 确保投影行存在并 `SELECT ... FOR
        UPDATE` 锁定投影，再读取/汇总 quota_debit ledger，最后覆盖投影。并发 append
        在投影 UPDATE 上等待（PG 行锁），其 ledger 行对 rebuild 的读取不可见（未提交）
        ——rebuild 写入旧总量并提交后，append 的原子增量在其上叠加，最终一致，不会
        lost update。缺失投影 upsert；存在则整体覆盖 used/extra/last_debit_id。
        debit/supplement 加 used（最近一个为 last_debit_id），reversal 减 used，credit
        加 extra；对总量取 max(0, used) 使重放与排序无关（累计反转不变量保证合法账本
        任意顺序下总量非负）。
        """
        quota_subject_user_id = _require_text(quota_subject_user_id, "quota_subject_user_id", 64)
        quota_period = _require_period(quota_period)
        now = self._clock.now_utc(connection)
        # 1) 确保投影行存在并持有投影行锁（append 的投影 UPDATE 会在此等待）。
        _insert_do_nothing(
            connection,
            quota_projection_table,
            {
                "quota_subject_user_id": quota_subject_user_id,
                "quota_period": quota_period,
                "base_limit": self._base_limit,
                "extra_granted": 0,
                "used": 0,
                "last_debit_id": None,
                "updated_at_utc": now,
            },
            ["quota_subject_user_id", "quota_period"],
        )
        connection.execute(
            select(quota_projection_table)
            .where(
                and_(
                    quota_projection_table.c.quota_subject_user_id == quota_subject_user_id,
                    quota_projection_table.c.quota_period == quota_period,
                )
            )
            .with_for_update()
        )
        # 2) 持有锁后读取 ledger（看不到未提交 append → 先写旧总量并提交，append
        #    随后原子增量，最终一致）。
        rows = (
            connection.execute(
                select(quota_debit_table)
                .where(
                    and_(
                        quota_debit_table.c.quota_subject_user_id == quota_subject_user_id,
                        quota_debit_table.c.quota_period == quota_period,
                    )
                )
                .order_by(quota_debit_table.c.created_at_utc, quota_debit_table.c.quota_debit_id)
            )
            .mappings()
            .all()
        )
        used = 0
        extra = 0
        last_debit_id = None
        for row in rows:
            kind = row["entry_kind"]
            delta = int(row["page_delta"])
            if kind in ("debit", "supplement"):
                used += delta
                last_debit_id = str(row["quota_debit_id"])
            elif kind == "reversal":
                used += delta
            elif kind == "credit":
                extra += abs(delta)
        # 总下限 0：累计反转不变量（每个 debit 的反转 ≤ 其 page_delta）保证任意合法
        # 账本在任何重放顺序下 used 非负；对总量取 max(0, used) 使重放与排序无关
        # （created_at 并列时不会因中间态被错误截断到 0）。
        used = max(0, used)
        connection.execute(
            update(quota_projection_table)
            .where(
                and_(
                    quota_projection_table.c.quota_subject_user_id == quota_subject_user_id,
                    quota_projection_table.c.quota_period == quota_period,
                )
            )
            .values(
                base_limit=self._base_limit,
                used=used,
                extra_granted=extra,
                last_debit_id=last_debit_id,
                updated_at_utc=now,
            )
        )
