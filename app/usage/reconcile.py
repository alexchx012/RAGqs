"""Unknown provider call 结构化对账（Task 5，H3 + review agent-11）。

语义（正式 spec 优先于旧 task brief/plan）：
- 对账结果结构化：ConfirmedUsage（provider 确认已发送且有完整计量 → 补记 usage）、
  ConfirmedNotSent（确认未发送 → not_sent，无 usage）、StillUnknown（保持 unknown，
  下次再试）、ReconciliationOnlyAmount（仅金额可确认 → 写入 usage_reconciliation
  对账分组，不伪造 usage，保持 unknown 供后续全量对账）。
- provider 查询（port.confirm）严格在事务外（connection=None，网络 I/O 绝不持 DB
  事务）：候选扫描（短连接）→ 每条候选短事务 1（stale dispatching 幂等转 unknown）
  → 事务外 confirm → 单个短事务 2 应用决策。应用使用 connection-aware ledger 方法
  （complete_provider_call_in_transaction / mark_not_sent_in_transaction /
  record_reconciliation_amount_only），不嵌套自开事务 wrapper。
- TOCTOU：应用事务内重新读取状态，非 unknown（已 completed/not_sent）→ 跳过，
  避免终态 usage 与 amount-only 并存。
- starvation 轮转：候选按 last_reconcile_attempt_at_utc NULL-first 升序排序；
  StillUnknown / ReconciliationOnlyAmount 更新尝试时间，防止 limit 前的永久 unknown
  饿死后续候选。
- ProviderReconciliationPort 只在 app/usage/ports.py 定义（单一来源），本模块导入。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import Engine, and_, or_, select, update
from sqlalchemy.engine import Connection

from app.platform.errors import PlatformError

from .ledger import OwnershipSnapshot, ProviderMeasurement, UsageLedger  # noqa: F401
from .ports import ProviderReconciliationPort
from .schema import provider_call_table, usage_event_table


@dataclass(frozen=True, slots=True)
class ConfirmedUsage:
    """对账确认：provider 确认已发送且有完整计量 → 补记 usage（含实际 started）。"""

    measurement: ProviderMeasurement
    ownership: OwnershipSnapshot
    result: str
    started_at_utc: datetime
    provider_request_id: str | None = None


@dataclass(frozen=True, slots=True)
class ConfirmedNotSent:
    """对账确认：provider 确认未发送 → not_sent，无 usage。"""


@dataclass(frozen=True, slots=True)
class StillUnknown:
    """对账仍无法确认 → 保持 unknown，下次再试。"""


@dataclass(frozen=True, slots=True)
class ReconciliationOnlyAmount:
    """仅金额可确认（无请求级计量）→ 写入 usage_reconciliation 对账分组，不伪造 usage。"""

    amount: Decimal
    currency_code: str
    ownership: OwnershipSnapshot


class NoopProviderReconciliationPort:
    """测试/演示适配器：真实 no-op（一律 StillUnknown），不返回无效 ConfirmedUsage。"""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def confirm(self, *, provider_call_id, fingerprint, connection) -> StillUnknown:
        del fingerprint, connection
        self.calls.append(provider_call_id)
        return StillUnknown()


class UnavailableProviderReconciliationPort:
    """生产缺省：fail-closed。"""

    def confirm(self, *, provider_call_id, fingerprint, connection):
        del provider_call_id, fingerprint, connection
        raise PlatformError(
            "provider_reconciliation_unavailable",
            "Provider reconciliation is not configured",
            {},
            503,
            True,
        )


class LedgerBackedProviderReconciliationPort:
    """生产对账端口：以 usage ledger 的持久化状态为 provider 侧确认事实。

    DashScope compatible-mode 未提供按 provider_call_id 的结果查询 API；
    生产实现基于账本不变量做保守判定（§2.9 deadline 契约保证 provider 调用
    不会越过 deadline 继续处理）：

    - 行不存在 → ``StillUnknown``（无法确认，保持 unknown）；
    - ``prepared``（从未 dispatch）→ ``ConfirmedNotSent``（确定未发送——
      worker 在 prepare 与 dispatch 之间崩溃的恢复场景）；
    - ``not_sent`` → ``ConfirmedNotSent``；
    - ``completed`` → ``ConfirmedUsage``（从 usage_event 恢复计量与归属）；
    - ``dispatching``/``unknown`` → ``StillUnknown``（已发送但结果未知，
      保守不虚构用量；provider 侧查询 API 就绪后在此接入）。

    只读查询：port 契约要求 provider 查询在事务外执行（connection=None，
    不得回写账本）。
    """

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def confirm(
        self,
        *,
        provider_call_id: str,
        fingerprint: str,
        connection: Connection | None,
    ) -> ConfirmedUsage | ConfirmedNotSent | StillUnknown:
        del fingerprint, connection
        with self._engine.connect() as reader:
            call = (
                reader.execute(
                    select(provider_call_table).where(
                        provider_call_table.c.provider_call_id == provider_call_id
                    )
                )
                .mappings()
                .first()
            )
            if call is None:
                return StillUnknown()
            status = str(call["status"])
            if status == "prepared":
                # prepare 已提交但从未进入 dispatching：确定未发送。
                return ConfirmedNotSent()
            if status == "not_sent":
                return ConfirmedNotSent()
            if status != "completed":
                return StillUnknown()
            event = (
                reader.execute(
                    select(usage_event_table)
                    .where(
                        usage_event_table.c.provider_call_id == provider_call_id,
                        usage_event_table.c.event_kind == "provider_usage",
                    )
                    .order_by(usage_event_table.c.created_at_utc.desc())
                    .limit(1)
                )
                .mappings()
                .first()
            )
            if event is None:
                # completed 行缺少 usage 事件属于账本不变量缺口：不虚构用量。
                return StillUnknown()
            return ConfirmedUsage(
                measurement=_measurement_from_event(event),
                ownership=_ownership_from_event(event),
                result=str(event["result"]),
                started_at_utc=_utc(event["started_at_utc"]),
                provider_request_id=(
                    str(event["provider_request_id"])
                    if event["provider_request_id"] is not None
                    else None
                ),
            )


def _measurement_from_event(event: Any) -> ProviderMeasurement:
    sources = event["measurement_sources"]
    return ProviderMeasurement(
        input_tokens=event["input_tokens"],
        prompt_cache_hit_tokens=event["prompt_cache_hit_tokens"],
        prompt_cache_miss_tokens=event["prompt_cache_miss_tokens"],
        output_tokens=event["output_tokens"],
        reasoning_tokens=event["reasoning_tokens"],
        image_count=event["image_count"],
        visual_input_tokens=event["visual_input_tokens"],
        embedding_input_tokens=event["embedding_input_tokens"],
        vector_count=event["vector_count"],
        measurement_sources=dict(sources) if isinstance(sources, dict) else {},
    )


def _ownership_from_event(event: Any) -> OwnershipSnapshot:
    values = dict(event["ownership_json"] or {})
    source_space_ids = values.pop("source_space_ids", None)
    return OwnershipSnapshot(
        actor_user_id=str(values.get("actor_user_id") or ""),
        actor_role_snapshot=str(values.get("actor_role_snapshot") or ""),
        actor_department_id_snapshot=values.get("actor_department_id_snapshot"),
        quota_subject_user_id=values.get("quota_subject_user_id"),
        cost_center_key=str(values.get("cost_center_key") or ""),
        space_id=values.get("space_id"),
        space_kind=values.get("space_kind"),
        space_owner_user_id=values.get("space_owner_user_id"),
        authorization_version=values.get("authorization_version"),
        fence_token=values.get("fence_token"),
        source_space_ids=tuple(source_space_ids or ()),
    )


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _select_candidates(
    engine: Engine,
    *,
    older_than_utc: datetime,
    current_utc: datetime,
    limit: int,
) -> list[dict]:
    """事务外扫描：stale unknown；dispatching 还必须已越过持久化 deadline。"""
    older_than = _utc(older_than_utc)
    current = _utc(current_utc)
    with engine.connect() as connection:
        rows = (
            connection.execute(
                select(provider_call_table)
                .where(
                    or_(
                        and_(
                            provider_call_table.c.status == "unknown",
                            provider_call_table.c.dispatching_at_utc <= older_than,
                        ),
                        and_(
                            provider_call_table.c.status == "dispatching",
                            provider_call_table.c.dispatching_at_utc <= older_than,
                            provider_call_table.c.deadline_utc <= current,
                        ),
                    )
                )
                .order_by(
                    provider_call_table.c.last_reconcile_attempt_at_utc.asc().nulls_first(),
                    provider_call_table.c.dispatching_at_utc.asc(),
                    provider_call_table.c.provider_call_id.asc(),
                )
                .limit(limit)
            )
            .mappings()
            .all()
        )
        return [dict(row) for row in rows]


def _touch_reconcile_attempt(ledger: UsageLedger, connection: Connection, call_id: str) -> None:
    """条件更新尝试时间：仅对仍为 unknown/dispatching 的调用（二轮复审 Important 2）。

    绝不给已终态化（completed/not_sent）的调用覆盖尝试时间——那会错误地让它们
    看起来仍可继续 reconcile。
    """
    connection.execute(
        update(provider_call_table)
        .where(
            and_(
                provider_call_table.c.provider_call_id == call_id,
                provider_call_table.c.status.in_(("unknown", "dispatching")),
            )
        )
        .values(last_reconcile_attempt_at_utc=ledger.clock.now_utc(connection))
    )


def _advance_state(
    ledger: UsageLedger,
    connection: Connection,
    call: dict,
    *,
    older_than_utc: datetime,
    current_utc: datetime,
) -> str | None:
    """锁内重验 stale + deadline 后推进 unknown；终态/不再 eligible 则跳过。"""
    call_id = str(call["provider_call_id"])
    if call["status"] == "unknown":
        return None
    try:
        ledger.mark_expired_dispatching_unknown_in_transaction(
            connection,
            call_id,
            older_than_utc=older_than_utc,
            current_utc=current_utc,
        )
    except PlatformError as exc:
        if exc.code != "provider_call_state_conflict":
            raise
        return "done"
    _touch_reconcile_attempt(ledger, connection, call_id)
    return None


def reconcile_unknown_calls(
    engine: Engine,
    ledger: UsageLedger,
    port: ProviderReconciliationPort,
    *,
    older_than_utc: datetime,
    limit: int = 100,
) -> int:
    """扫描 unknown/stale dispatching 调用并逐个确认；返回推进（completed/not_sent/amount_only）的条数。

    provider 查询（port.confirm）在事务外执行；每个候选一个短事务应用决策
    （connection-aware ledger 方法）。单条失败（含 port 异常与账本冲突）向上抛出，
    不吞错——调用方决定是否重跑；已推进的条目不会重复处理（幂等终态）。
    """
    current_utc = ledger.clock.now_utc()
    candidates = _select_candidates(
        engine,
        older_than_utc=older_than_utc,
        current_utc=current_utc,
        limit=limit,
    )
    advanced = 0
    for call in candidates:
        call_id = str(call["provider_call_id"])
        # 短事务 1：锁内重验 stale + deadline，再转 unknown；提交后绝不跨网络。
        with engine.begin() as connection:
            skip = _advance_state(
                ledger,
                connection,
                call,
                older_than_utc=older_than_utc,
                current_utc=current_utc,
            )
        if skip is not None:
            continue
        # 事务外：provider 查询（网络 I/O）；connection=None 表明调用方不得回写账本。
        decision = port.confirm(
            provider_call_id=call_id,
            fingerprint=str(call["request_fingerprint"]),
            connection=None,
        )
        if not isinstance(
            decision,
            (ConfirmedUsage, ConfirmedNotSent, StillUnknown, ReconciliationOnlyAmount),
        ):
            # Runtime adapters can violate their Protocol. Fail closed before
            # opening the decision transaction, without serializing/revealing
            # the invalid object or touching reconcile attempt state.
            raise PlatformError(
                "provider_reconciliation_contract_error",
                "Provider reconciliation returned an invalid decision",
                {},
                502,
                True,
            )
        # 短事务 2：应用决策（connection-aware，不嵌套 wrapper）。权威线性化点在各
        # ledger 方法内部（行锁/条件更新），此处仅作快速路径预检。
        with engine.begin() as connection:
            current = ledger._require_call(connection, call_id)  # noqa: SLF001 - 内部只读查询
            if current["status"] != "unknown":
                continue  # 已终态化（completed/not_sent）→ 跳过，避免与 amount-only 并存
            if isinstance(decision, ConfirmedUsage):
                ledger.complete_provider_call_in_transaction(
                    connection,
                    provider_call_id=call_id,
                    measurement=decision.measurement,
                    ownership=decision.ownership,
                    result=decision.result,
                    provider_request_id=decision.provider_request_id,
                    started_at_utc=decision.started_at_utc,
                )
                advanced += 1
            elif isinstance(decision, ConfirmedNotSent):
                ledger.mark_not_sent_in_transaction(connection, call_id)
                advanced += 1
            elif isinstance(decision, ReconciliationOnlyAmount):
                # claim 在 record 方法内条件执行（status='unknown' 才持锁/写入）：
                # 返回 None = 线性化失败（已终态）→ 跳过，不写 amount-only。
                outcome = ledger.record_reconciliation_amount_only(
                    connection,
                    call=current,
                    amount=decision.amount,
                    currency_code=decision.currency_code,
                    ownership=decision.ownership,
                )
                if outcome is None:
                    continue
                _id, inserted = outcome
                del _id
                if inserted:
                    advanced += 1
            elif isinstance(decision, StillUnknown):
                _touch_reconcile_attempt(ledger, connection, call_id)
    return advanced
