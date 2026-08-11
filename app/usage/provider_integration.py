"""Provider 调用 + usage 账本的生产集成 wrapper（Task 5 review agent-11 #2 + 二轮/三轮复审）。

包装 `app.platform.provider.call_with_policy`：每个 attempt 使用 context 注入的新
provider_call_id 执行完整生命周期——prepare 短事务 → mark_dispatching（started
callback 在 ledger 的 dispatch 事务内、条件 UPDATE 前延迟调用并持久化）短事务 →
DB 事务外执行真实 operation（网络 I/O）→ 结果分支：
- sent=False（ProviderFailure）→ mark_not_sent（无 usage；清除 started）；
- sent=True 且有确定响应（status_code）→ completed + failed usage；
- sent=True 且结果无法确认（无 status_code）→ mark_unknown；
- 成功 → completed + usage。

确定性终态化：一旦进入 dispatching，任何已处理异常路径都必须推进终态：
- operation 的 ProviderFailure 按上述 sent/status 分支终态化；终态 callback 失败时通过
  accounting-abort 先保存 provider circuit/aggregate telemetry，再传播保存的本地错误；
- operation cancellation 先 mark_unknown；prepare/dispatch/发送前 clock 的普通异常、
  cancellation 或非法返回都属于确定未发送，已取得本次 physical call ownership 时回退
  not_sent；prepare/dispatch 只接受严格 bool；
- 普通 operation Exception 先 mark_unknown，再以强制 circuit failure 的内部
  accounting-abort 保存 unknown/provider_error，最终原样重抛最初 operation 异常；
- measurement/ownership/complete 的普通异常、cancellation 或非法返回均安全回退
  unknown；complete 结果不确定时只改写仍未完成的行，已提交 completed+usage 保持不变；
- 错误延迟到 policy accounting/telemetry 完成后按“终态 lifecycle > callback > 原始
  operation > 后续 policy/circuit/telemetry”传播同一对象；只捕获明确 CancelledError
  和 Exception，不拦截 KeyboardInterrupt/SystemExit。

所有账本写入经 `ProviderUsageLifecycle`（默认 `UsageLedgerLifecycle` 适配器，独立
短事务），网络 I/O 期间绝不持 DB 事务。真实 provider 适配器不属于本领域（spec 边界），
`operation` 由调用方提供：成功返回 raw provider value，确定失败抛 `ProviderFailure`；
`ProviderResult` 只由外层 policy 拥有，transport 返回该 envelope 属于合同错误。本模块是
可复用的生产 wrapper。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Protocol

from app.platform.errors import PlatformError
from app.platform.provider import (
    CircuitBreakerRegistry,
    ProviderCallContext,
    ProviderFailure,
    ProviderFailureAccountingAbort,
    ProviderPolicyAbort,
    ProviderPreSendDeadlineExceeded,
    ProviderResult,
    RetryPolicy,
    call_with_policy,
)
from app.usage.ledger import OwnershipSnapshot, ProviderMeasurement, UsageLedger


class ProviderUsageLifecycle(Protocol):
    """逐 attempt 的 ledger 生命周期边界（生产默认 UsageLedgerLifecycle）。

    `mark_dispatching` 接受 started callback：采样由 ledger 在 dispatch 事务内
    （连接/事务已开始、紧邻条件 UPDATE 前）延迟调用——不在 adapter 调用 ledger 前求值。
    """

    def prepare(
        self,
        *,
        provider_call_id: str,
        provider: str,
        model: str,
        operation: str,
        execution_kind: str,
        execution_id: str,
        attempt_id: str,
        resource_id: str | None,
        deadline_utc: datetime,
        request_fingerprint: str,
    ) -> bool: ...
    def mark_dispatching(
        self, provider_call_id: str, *, started_at_provider: Callable[[], datetime]
    ) -> bool: ...
    def complete(
        self,
        *,
        provider_call_id: str,
        measurement: ProviderMeasurement,
        ownership: OwnershipSnapshot,
        result: str,
        provider_request_id: str | None = None,
        started_at_utc: datetime | None = None,
    ) -> str: ...
    def mark_not_sent(self, provider_call_id: str) -> None: ...
    def mark_not_sent_if_prepared(self, provider_call_id: str) -> None: ...
    def mark_unknown(self, provider_call_id: str) -> None: ...
    def mark_unknown_if_unfinished(self, provider_call_id: str) -> None: ...


class UsageLedgerLifecycle:
    """生产适配器：把生命周期委托给 UsageLedger 公开短事务 wrapper。"""

    def __init__(self, ledger: UsageLedger) -> None:
        self._ledger = ledger

    def prepare(
        self,
        *,
        provider_call_id: str,
        provider: str,
        model: str,
        operation: str,
        execution_kind: str,
        execution_id: str,
        attempt_id: str,
        resource_id: str | None,
        deadline_utc: datetime,
        request_fingerprint: str,
    ) -> bool:
        _call_id, created = self._ledger.prepare_provider_call_with_status(
            provider=provider,
            model=model,
            operation=operation,
            execution_kind=execution_kind,
            execution_id=execution_id,
            provider_call_id=provider_call_id,
            attempt_id=attempt_id,
            resource_id=resource_id,
            deadline_utc=deadline_utc,
            request_fingerprint=request_fingerprint,
        )
        return created

    def mark_dispatching(
        self, provider_call_id: str, *, started_at_provider: Callable[[], datetime]
    ) -> bool:
        # started callback 直接透传给 ledger：由 ledger 在事务内延迟调用。
        return self._ledger.mark_dispatching(
            provider_call_id, started_at_provider=started_at_provider
        )

    def complete(
        self,
        *,
        provider_call_id: str,
        measurement: ProviderMeasurement,
        ownership: OwnershipSnapshot,
        result: str,
        provider_request_id: str | None = None,
        started_at_utc: datetime | None = None,
    ) -> str:
        return self._ledger.complete_provider_call(
            provider_call_id=provider_call_id,
            measurement=measurement,
            ownership=ownership,
            result=result,
            provider_request_id=provider_request_id,
            started_at_utc=started_at_utc,
        )

    def mark_not_sent(self, provider_call_id: str) -> None:
        self._ledger.mark_not_sent(provider_call_id)

    def mark_not_sent_if_prepared(self, provider_call_id: str) -> None:
        self._ledger.mark_not_sent_if_prepared(provider_call_id)

    def mark_unknown(self, provider_call_id: str) -> None:
        self._ledger.mark_unknown(provider_call_id)

    def mark_unknown_if_unfinished(self, provider_call_id: str) -> None:
        self._ledger.mark_unknown_if_unfinished(provider_call_id)


def _utc(value: Any, *, callback_name: str | None = None) -> datetime:
    if not isinstance(value, datetime):
        if callback_name is not None:
            raise TypeError(f"{callback_name} callback must return datetime")
        raise TypeError("datetime value must be a datetime")
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def run_provider_call_with_usage(
    *,
    operation: Callable[[ProviderCallContext, Any], Any],
    context: ProviderCallContext,
    model: str,
    lifecycle: ProviderUsageLifecycle,
    measurement_extractor: Callable[
        [Any, ProviderCallContext, ProviderFailure | None], ProviderMeasurement
    ],
    ownership_provider: Callable[[ProviderCallContext], OwnershipSnapshot],
    execution_kind: str,
    execution_id: str,
    request_fingerprint: str,
    request: Any = None,
    asynchronous: bool = False,
    policy: RetryPolicy | None = None,
    circuits: CircuitBreakerRegistry | None = None,
    now: Callable[[], datetime] | None = None,
    sleep: Callable[[float], None] | None = None,
    jitter: Callable[[float], float] | None = None,
    telemetry: Any = None,
) -> ProviderResult:
    """按 call_with_policy 重试语义执行真实 operation，并逐 attempt 记录 usage 事实。

    本地 callback 错误不伪装为 provider failure。若 callback 发生在真实
    `ProviderFailure` 之后，则通过内部 accounting-abort 让 policy 先保存当前 provider
    outcome，再按“终态 ledger 错误 > callback 错误 > operation 错误”传播原始对象。
    """

    clock_now = now or (lambda: datetime.now(UTC))
    hook_errors: list[BaseException] = []
    pending_errors: list[BaseException] = []

    def capture_hook_error(exc: BaseException) -> None:
        hook_errors.append(exc)

    def run_terminal(action: Callable[[], Any]) -> BaseException | None:
        """运行独立终态动作，并严格要求 callback 正常返回 None。"""
        try:
            if action() is not None:
                return TypeError("lifecycle terminal callback must return None")
        except asyncio.CancelledError as cancellation:
            return cancellation
        except Exception as exc:  # noqa: BLE001 - ledger hook 错误必须原样浮出
            return exc
        return None

    def abort_after_terminal(
        action: Callable[[], None],
        original_error: BaseException,
        *,
        message: str,
    ) -> None:
        """本地 lifecycle/contract/cancellation 错误：终态写错误优先，policy 保持中性。"""
        terminal_error = run_terminal(action)
        if terminal_error is not None:
            capture_hook_error(terminal_error)
        capture_hook_error(original_error)
        raise ProviderPolicyAbort(message) from terminal_error or original_error

    def finalize_error(
        ctx: ProviderCallContext,
        hook_error: BaseException,
        failure: ProviderFailure | None,
        *,
        completion_uncertain: bool = False,
    ) -> None:
        """终止 finalize 链；真实 provider failure 必须先进入 policy accounting。"""

        def terminal_action() -> Any:
            if completion_uncertain:
                return lifecycle.mark_unknown_if_unfinished(ctx.provider_call_id)
            return lifecycle.mark_unknown(ctx.provider_call_id)

        terminal_error = run_terminal(terminal_action)
        if terminal_error is not None:
            capture_hook_error(terminal_error)
        capture_hook_error(hook_error)
        if failure is not None:
            raise ProviderFailureAccountingAbort(failure) from hook_error

    def terminalize_provider_failure(
        action: Callable[[], None],
        failure: ProviderFailure,
    ) -> None:
        terminal_error = run_terminal(action)
        if terminal_error is not None:
            capture_hook_error(terminal_error)
            raise ProviderFailureAccountingAbort(failure) from terminal_error

    def finalize_usage(
        value: Any,
        ctx: ProviderCallContext,
        failure: ProviderFailure | None,
        *,
        result: str,
    ) -> None:
        """依次运行 measurement/ownership/complete；任一错误只做一次安全终态决策。"""
        try:
            measurement = measurement_extractor(value, ctx, failure)
            if not isinstance(measurement, ProviderMeasurement):
                raise TypeError("measurement callback must return ProviderMeasurement")
        except asyncio.CancelledError as cancellation:
            finalize_error(ctx, cancellation, failure)
            return
        except Exception as exc:  # noqa: BLE001 - extractor callback 错误原样浮出
            finalize_error(ctx, exc, failure)
            return
        try:
            ownership = ownership_provider(ctx)
            if not isinstance(ownership, OwnershipSnapshot):
                raise TypeError("ownership callback must return OwnershipSnapshot")
        except asyncio.CancelledError as cancellation:
            finalize_error(ctx, cancellation, failure)
            return
        except Exception as exc:  # noqa: BLE001 - ownership callback 错误原样浮出
            finalize_error(ctx, exc, failure)
            return
        try:
            usage_event_id = lifecycle.complete(
                provider_call_id=ctx.provider_call_id,
                measurement=measurement,
                ownership=ownership,
                result=result,
            )
            if not isinstance(usage_event_id, str) or not usage_event_id:
                raise TypeError("lifecycle complete must return a non-empty str")
        except asyncio.CancelledError as cancellation:
            finalize_error(
                ctx,
                cancellation,
                failure,
                completion_uncertain=True,
            )
        except Exception as exc:  # noqa: BLE001 - completion hook 错误原样浮出
            finalize_error(
                ctx,
                exc,
                failure,
                completion_uncertain=True,
            )

    def mark_not_sent_or_abort(ctx: ProviderCallContext) -> None:
        terminal_error = run_terminal(lambda: lifecycle.mark_not_sent(ctx.provider_call_id))
        if terminal_error is not None:
            capture_hook_error(terminal_error)
            raise ProviderPolicyAbort("pre-send deadline terminal hook failed") from terminal_error

    def wrapped_operation(ctx: ProviderCallContext, req: Any) -> Any:
        try:
            prepared_by_this_call = lifecycle.prepare(
                provider_call_id=ctx.provider_call_id,
                provider=ctx.provider,
                model=model,
                operation=ctx.operation,
                execution_kind=execution_kind,
                execution_id=execution_id,
                attempt_id=ctx.attempt_id,
                resource_id=ctx.resource_id,
                deadline_utc=ctx.deadline_utc,
                request_fingerprint=request_fingerprint,
            )
        except asyncio.CancelledError as cancellation:
            abort_after_terminal(
                lambda: lifecycle.mark_not_sent_if_prepared(ctx.provider_call_id),
                cancellation,
                message="pre-send prepare cancellation",
            )
        except Exception as exc:  # noqa: BLE001 - prepare 是 pre-send 账本 hook
            # Callback 可能在独立 prepare 事务提交后才失败；只改写仍为 prepared
            # 的本 physical call，缺失/已推进状态均保持不变。
            abort_after_terminal(
                lambda: lifecycle.mark_not_sent_if_prepared(ctx.provider_call_id),
                exc,
                message="pre-send prepare hook failed",
            )
        if not isinstance(prepared_by_this_call, bool):
            contract_error = TypeError("lifecycle prepare must return bool")
            abort_after_terminal(
                lambda: lifecycle.mark_not_sent_if_prepared(ctx.provider_call_id),
                contract_error,
                message="pre-send prepare returned non-bool",
            )
        if not prepared_by_this_call:
            replay_error = PlatformError(
                "provider_call_state_conflict",
                "Provider call attempt already exists and cannot be replayed for sending",
                {"provider_call_id": ctx.provider_call_id},
                409,
            )
            capture_hook_error(replay_error)
            raise ProviderPolicyAbort("pre-send lifecycle replay") from replay_error
        try:
            post_prepare_now = _utc(clock_now(), callback_name="clock")
        except asyncio.CancelledError as cancellation:
            abort_after_terminal(
                lambda: lifecycle.mark_not_sent(ctx.provider_call_id),
                cancellation,
                message="post-prepare clock cancellation",
            )
        except Exception as exc:  # noqa: BLE001 - clock/返回类型是本地基础设施合同
            abort_after_terminal(
                lambda: lifecycle.mark_not_sent(ctx.provider_call_id),
                exc,
                message="post-prepare clock failure",
            )
        if post_prepare_now >= _utc(ctx.deadline_utc):
            mark_not_sent_or_abort(ctx)
            raise ProviderPreSendDeadlineExceeded
        try:
            # started callback 由 ledger 在 dispatch 事务内延迟求值和重验。
            dispatch_committed = lifecycle.mark_dispatching(
                ctx.provider_call_id,
                started_at_provider=lambda: _utc(clock_now(), callback_name="clock"),
            )
        except asyncio.CancelledError as cancellation:
            abort_after_terminal(
                lambda: lifecycle.mark_not_sent(ctx.provider_call_id),
                cancellation,
                message="dispatch cancellation",
            )
        except Exception as exc:  # noqa: BLE001 - dispatch 是 pre-send ledger callback
            abort_after_terminal(
                lambda: lifecycle.mark_not_sent(ctx.provider_call_id),
                exc,
                message="pre-send dispatch hook failed",
            )
        if not isinstance(dispatch_committed, bool):
            contract_error = TypeError("lifecycle dispatch must return bool")
            abort_after_terminal(
                lambda: lifecycle.mark_not_sent(ctx.provider_call_id),
                contract_error,
                message="pre-send dispatch returned non-bool",
            )
        if not dispatch_committed:
            raise ProviderPreSendDeadlineExceeded
        try:
            final_pre_send_now = _utc(clock_now(), callback_name="clock")
        except asyncio.CancelledError as cancellation:
            abort_after_terminal(
                lambda: lifecycle.mark_not_sent(ctx.provider_call_id),
                cancellation,
                message="final pre-send clock cancellation",
            )
        except Exception as exc:  # noqa: BLE001 - clock/返回类型是本地基础设施合同
            abort_after_terminal(
                lambda: lifecycle.mark_not_sent(ctx.provider_call_id),
                exc,
                message="final pre-send clock failure",
            )
        if final_pre_send_now >= _utc(ctx.deadline_utc):
            mark_not_sent_or_abort(ctx)
            raise ProviderPreSendDeadlineExceeded
        try:
            value = operation(ctx, req)
        except asyncio.CancelledError as cancellation:
            abort_after_terminal(
                lambda: lifecycle.mark_unknown(ctx.provider_call_id),
                cancellation,
                message="operation cancellation",
            )
        except ProviderFailure as failure:
            if failure.sent:
                if failure.status_code is not None:
                    finalize_usage(None, ctx, failure, result="failed")
                else:
                    terminalize_provider_failure(
                        lambda: lifecycle.mark_unknown(ctx.provider_call_id),
                        failure,
                    )
            else:
                terminalize_provider_failure(
                    lambda: lifecycle.mark_not_sent(ctx.provider_call_id),
                    failure,
                )
            raise
        except Exception as exc:
            synthetic_failure = ProviderFailure(
                "provider_error",
                status_code=None,
                retryable=False,
                sent=True,
            )
            pending_errors.append(exc)
            terminal_error = run_terminal(lambda: lifecycle.mark_unknown(ctx.provider_call_id))
            if terminal_error is not None:
                capture_hook_error(terminal_error)
                raise ProviderFailureAccountingAbort(
                    synthetic_failure,
                    force_circuit_failure=True,
                ) from terminal_error
            raise ProviderFailureAccountingAbort(
                synthetic_failure,
                force_circuit_failure=True,
            ) from exc
        if isinstance(value, ProviderResult):
            contract_error = TypeError(
                "provider transport must return a raw value, not ProviderResult"
            )
            abort_after_terminal(
                lambda: lifecycle.mark_unknown(ctx.provider_call_id),
                contract_error,
                message="provider transport returned policy result",
            )
        finalize_usage(value, ctx, None, result="succeeded")
        return value

    try:
        result = call_with_policy(
            wrapped_operation,
            context,
            request,
            asynchronous=asynchronous,
            policy=policy,
            circuits=circuits,
            now=clock_now,
            sleep=sleep,
            jitter=jitter,
            telemetry=telemetry,
        )
    except asyncio.CancelledError:
        if hook_errors:
            raise hook_errors[0] from None
        if pending_errors:
            raise pending_errors[0] from None
        raise
    except Exception:
        if hook_errors:
            raise hook_errors[0] from None
        if pending_errors:
            raise pending_errors[0] from None
        raise
    if hook_errors:
        raise hook_errors[0]
    if pending_errors:
        raise pending_errors[0]
    return result
