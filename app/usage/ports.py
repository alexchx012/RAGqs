"""Protocols for future domain callers (ingestion/chat/eval/graph) and outbox change.

本模块一次性完整定义全部领域端口契约（H8）；本模块只声明契约，测试适配器用于
[PORT] 验收。生产 quota adapter 由 outbox owner 在 runtime 组装并保持同事务 fail-closed；
本领域不拥有 outbox 表。
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import TYPE_CHECKING, Literal, Protocol

from sqlalchemy.engine import Connection

# 运行时 import（global namespace 可解析）：calendar/ledger 均不反向 import 本模块，
# 无循环。datetime 本就运行时引入。get_type_hints(PublicationDebitPort.record) 依赖
# 这些名字在模块 globals 中解析为精确类。
from .calendar import CalendarLock
from .ledger import OwnershipSnapshot

if TYPE_CHECKING:
    # 仅类型检查期引入：reconcile 反向 import 本模块（`from .ports import ...`），
    # 必须留在 TYPE_CHECKING 避免运行时循环。
    from .reconcile import ConfirmedNotSent, ConfirmedUsage, ReconciliationOnlyAmount, StillUnknown


PublicationTerminalStatus = Literal["succeeded", "failed", "cancelled", "dead_letter"]


class UsageSubmissionPort(Protocol):
    """调用方提交实际 provider/local 消耗事实的边界（H7：含 local/recover/adjustment）。"""

    def prepare_provider_call(
        self,
        *,
        provider: str,
        model: str,
        operation: str,
        execution_kind: str,
        execution_id: str,
        provider_call_id: str | None = None,
        attempt_id: str | None = None,
        generation_id: str | None = None,
        resource_id: str | None = None,
        deadline_utc: datetime,
        request_fingerprint: str,
        replay_generation: int = 0,
    ) -> str: ...
    def mark_dispatching(
        self,
        provider_call_id: str,
        *,
        started_at_provider: Callable[[], datetime] | datetime,
    ) -> bool: ...
    def complete_provider_call(
        self,
        *,
        provider_call_id: str,
        measurement: object,
        ownership: object,
        result: str,
        provider_request_id: str | None = None,
        started_at_utc: object | None = None,
    ) -> str: ...
    def mark_not_sent(self, provider_call_id: str) -> None: ...
    def mark_unknown(self, provider_call_id: str) -> None: ...
    def submit_local_usage(
        self,
        *,
        execution_kind: str,
        execution_id: str,
        stage: str,
        resource_kind: str,
        measurement: object,
        ownership: object,
        result: str,
        started_at_utc: object,
        replay_generation: int = 0,
    ) -> str: ...
    def recover_unknown_call(
        self,
        *,
        provider_call_id: str,
        measurement: object,
        ownership: object,
        result: str,
        provider_request_id: str | None = None,
        started_at_utc: object | None = None,
    ) -> str: ...
    def append_usage_adjustment(
        self,
        *,
        referenced_event_id: str,
        adjustment_source_namespace: str,
        adjustment_source_id: str,
        adjustment_allocation_key: str,
        deltas: dict[str, int],
        ownership: object,
        result: str = "adjusted",
    ) -> str: ...
    def append_cost_adjustment(
        self,
        *,
        referenced_event_id: str,
        adjustment_source_namespace: str,
        adjustment_source_id: str,
        adjustment_allocation_key: str,
        amount_delta: object,
        currency_code: str,
        ownership: object,
        result: str = "cost_adjusted",
    ) -> str: ...


class DirectIngestGatePort(Protocol):
    """直接入库受理门禁（仅余额检查，不预留/冻结）。"""

    def check(
        self, connection: Connection, *, quota_subject_user_id: str, pages: int, role: str
    ) -> None: ...


class PublicationDebitPort(Protocol):
    """publication 业务终态事务内的 fail-closed debit 提交。

    `publication_status` 是业务 publication 终态而非 provider-call 终态；只有
    `succeeded` 可扣额，`failed`/`cancelled`/`dead_letter` 必须保留既有 usage 并返回
    None。使用精确领域类型（OwnershipSnapshot/CalendarLock/datetime）；前两者为模块
    global namespace 的运行时 import（无循环，get_type_hints 可解析），datetime 本
    就运行时引入。实现方须以同签名结构化兼容，并在强入口提供运行时校验（非法类型
    稳定 422，不泄漏 AttributeError/TypeError）。
    """

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
    ) -> str | None: ...


class ProviderReconciliationPort(Protocol):
    """对账确认端口：provider 查询在事务外执行（connection=None，不得回写账本）。"""

    def confirm(
        self, *, provider_call_id: str, fingerprint: str, connection: Connection | None
    ) -> ConfirmedUsage | ConfirmedNotSent | StillUnknown | ReconciliationOnlyAmount: ...


class OutboxEnqueuePort(Protocol):
    """Transactional outbox enqueue：同一事务内调用；enqueue 失败 = 事务失败。"""

    def enqueue(
        self,
        *,
        connection: Connection,
        event_type: Literal["quota_approved", "quota_rejected"],
        aggregate_type: Literal["quota_request"],
        aggregate_id: str,
        transition_version: int,
        recipient_user_id: str,
        occurred_at: datetime,
        payload_fingerprint: str,
        payload: dict,
    ) -> None: ...


class NoopOutboxEnqueuePort:
    """测试适配器：把事件追加到内存列表（04 子计划的 RecordingOutboxPort 提供同事务外部表版本）。"""

    def __init__(self) -> None:
        self.events: list[dict] = []

    def enqueue(
        self,
        *,
        connection,
        event_type,
        aggregate_type,
        aggregate_id,
        transition_version,
        recipient_user_id,
        occurred_at,
        payload_fingerprint,
        payload,
    ) -> None:
        del connection
        self.events.append(
            {
                "event_type": event_type,
                "aggregate_type": aggregate_type,
                "aggregate_id": aggregate_id,
                "transition_version": transition_version,
                "recipient_user_id": recipient_user_id,
                "occurred_at": occurred_at,
                "payload_fingerprint": payload_fingerprint,
                "payload": payload,
            }
        )


class UnavailableOutboxEnqueuePort:
    """显式故障注入适配器：fail-closed（H6）；Noop 仅测试使用。"""

    def enqueue(
        self,
        *,
        connection,
        event_type,
        aggregate_type,
        aggregate_id,
        transition_version,
        recipient_user_id,
        occurred_at,
        payload_fingerprint,
        payload,
    ) -> None:
        del connection, event_type, aggregate_type, aggregate_id
        del transition_version, recipient_user_id, occurred_at, payload_fingerprint, payload
        from app.platform.errors import PlatformError

        raise PlatformError(
            "quota_event_outbox_unavailable",
            "Outbox enqueue is not configured",
            {},
            503,
            True,
        )
