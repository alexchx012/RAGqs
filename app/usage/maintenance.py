"""Task 11：quota maintenance worker 与受保护 CLI（H5）。

语义（正式 spec §5 + task-11-brief + 平台真实 WorkerRuntime/lease/fence）：
- 取消候选仅 pending：关闭业务月（quota_period < period_for(now)）优先于申请人
  inactive；未关闭月但用户非 active 也取消。worker 每个候选一个独立 `run_task`
  （真实 lease + fence）；list 候选在一个事务内（calendar lock + 单一 DB now），
  每个 cancel 在其 fenced transaction connection 内以 runtime/worker clock 读取
  的单一 now 调 `_cancel_transition`（稳定 `WHERE status='pending'` 条件更新、
  version 一次递增、cancel_reason/reviewed_at/updated_at 同一 now）。
- FenceViolation/LeaseUnavailable/PlatformError → deferred；成功计 completed。
- revoke_all：pending 快照逐 request task（受 limit 限制），reason=deployment_revocation；
  先 revoke 再常规 run_once，stats 合并。limit/重复运行/并发已取消均幂等。
- `run_usage_maintenance_once`/CLI 受明确维护密钥保护（沿用 settings SecretStr
  秘密模式）：任何 profile 缺失/空白密钥均拒绝执行（稳定 ValueError）；CLI
  缺失密钥均非零退出（SystemExit(2)，稳定 stderr）。密钥只来自 settings/env，
  不进参数、日志与输出。Task 12 将把本 worker 接入 runtime/lifespan wiring，
  本任务不创建任何后台循环。
- 不假设 WorkerRuntime API：`run_task(task_id, owner, callback)` 在 lease+fence
  事务内调用 `callback(context, connection)`；mutation 经
  `runtime.resolve("quota_request_service")` 的 `_cancel_transition` 执行。
"""

from __future__ import annotations

import argparse
import logging
import os
import socket
import sys
from dataclasses import dataclass
from functools import partial
from typing import NoReturn

from sqlalchemy.engine import Connection

from app.platform import runtime as platform_runtime_module
from app.platform.config import PlatformSettings, load_platform_settings
from app.platform.context import TaskContext
from app.platform.errors import PlatformError
from app.platform.persistence import FenceViolation, LeaseUnavailable
from app.platform.runtime import PlatformRuntime, build_runtime
from app.platform.worker import WorkerRuntime, create_worker_runtime

_logger = logging.getLogger(__name__)

_CANCEL_TASK_PREFIX = "usage-maintenance:cancel:"
_REVOCATION_REASON = "deployment_revocation"
_OWNER_MAX_LENGTH = 128
_INVALID_OWNER_MESSAGE = (
    "maintenance owner must be a non-empty string without surrounding whitespace"
)
_INVALID_LIMIT_MESSAGE = "maintenance limit must be a non-negative integer"
_MISSING_KEY_MESSAGE = "RAG_MAINTENANCE_KEY is required"


def _validate_maintenance_inputs(owner: object, limit: object) -> tuple[str, int]:
    if (
        not isinstance(owner, str)
        or not owner
        or owner != owner.strip()
        or len(owner) > _OWNER_MAX_LENGTH
    ):
        raise ValueError(_INVALID_OWNER_MESSAGE)
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
        raise ValueError(_INVALID_LIMIT_MESSAGE)
    return owner, limit


def _default_owner() -> str:
    prefix = "usage-maintenance:"
    suffix = f":{str(os.getpid())[-16:]}"
    hostname_limit = max(0, _OWNER_MAX_LENGTH - len(prefix) - len(suffix))
    hostname = str(socket.gethostname()).strip()[:hostname_limit]
    return f"{prefix}{hostname}{suffix}"


@dataclass(frozen=True, slots=True)
class MaintenanceStats:
    completed: int
    deferred: int


class UsageMaintenanceWorker:
    """Leased worker：list 候选 → 每候选独立 run_task 取消（lease/fence 幂等）。"""

    def __init__(self, worker_runtime: WorkerRuntime) -> None:
        self._worker_runtime = worker_runtime

    def run_once(self, *, owner: str, limit: int = 100) -> MaintenanceStats:
        validated_owner, validated_limit = _validate_maintenance_inputs(owner, limit)
        if validated_limit == 0:
            return MaintenanceStats(completed=0, deferred=0)
        runtime = self._worker_runtime.runtime
        requests = runtime.resolve("quota_request_service")
        engine = runtime.resolve("database_engine")
        clock = runtime.resolve("database_clock")
        calendar = runtime.resolve("business_calendar")
        completed = 0
        deferred = 0
        # 候选列表：调用方事务内 calendar lock + 单一 DB now（与 Task 7-10 同构）。
        with engine.begin() as connection:
            lock = calendar.lock_or_verify(connection)
            now = clock.now_utc(connection)
            candidates = requests.list_cancel_candidates(connection, calendar_lock=lock, now=now)[
                :validated_limit
            ]
        for candidate in candidates:
            qr_id = candidate["quota_request_id"]
            reason = candidate["reason"]
            try:
                # 每个候选一个独立 run_task：真实 lease 获取 + fenced transaction；
                # 同任务重跑/并发由 lease 幂等，已取消由条件更新幂等。
                self._worker_runtime.run_task(
                    f"{_CANCEL_TASK_PREFIX}{qr_id}",
                    validated_owner,
                    partial(self._cancel, quota_request_id=qr_id, reason=reason),
                )
                completed += 1
            except (FenceViolation, LeaseUnavailable, PlatformError):
                deferred += 1
        return MaintenanceStats(completed=completed, deferred=deferred)

    def _cancel(
        self,
        _context: TaskContext,
        connection: Connection,
        *,
        quota_request_id: str,
        reason: str,
    ) -> dict[str, object]:
        del _context
        # mutation callback 内 resolve service；now 取 runtime/worker clock 且使用
        # run_task 传入的 fenced transaction connection（caller transaction）——
        # 条件更新 + 事务提交都由 fenced_transaction 包裹，异常即回滚无半取消。
        runtime = self._worker_runtime.runtime
        requests = runtime.resolve("quota_request_service")
        now = runtime.resolve("database_clock").now_utc(connection)
        affected = requests._cancel_transition(  # noqa: SLF001 - internal transition reused
            connection, request_id=quota_request_id, reason=reason, now=now
        )
        return {"quota_request_id": quota_request_id, "affected": affected}


def _require_maintenance_key(settings: PlatformSettings) -> None:
    """Require a non-blank maintenance key for every maintenance entry point."""
    if settings.maintenance_key is None or not settings.maintenance_key.get_secret_value().strip():
        raise ValueError(_MISSING_KEY_MESSAGE)


def _build_usage_runtime(settings: PlatformSettings) -> PlatformRuntime:
    """Build the same default runtime graph used by the API process."""
    return build_runtime(settings)


def run_usage_maintenance_once(
    settings: PlatformSettings,
    *,
    runtime: PlatformRuntime | None = None,
    owner: str | None = None,
    revoke_all: bool = False,
    limit: int = 100,
) -> MaintenanceStats:
    _require_maintenance_key(settings)
    resolved_owner = _default_owner() if owner is None else owner
    validated_owner, validated_limit = _validate_maintenance_inputs(resolved_owner, limit)
    if validated_limit == 0:
        return MaintenanceStats(completed=0, deferred=0)

    owns_runtime = runtime is None
    active_runtime = runtime if runtime is not None else _build_usage_runtime(settings)
    worker_runtime = create_worker_runtime(settings, runtime=active_runtime)
    try:
        # 启动时锁定/校验业务日历（H3）：失败早于任何取消任务（时区不匹配 fail-fast）。
        # 与 app lifespan 共享同一 helper（M4），避免启动与 CLI 的 lock 逻辑漂移。
        platform_runtime_module.ensure_business_calendar_locked(worker_runtime.runtime)
        revoked = (
            _revoke_all(worker_runtime, validated_owner, validated_limit)
            if revoke_all
            else (MaintenanceStats(completed=0, deferred=0))
        )
        regular = UsageMaintenanceWorker(worker_runtime).run_once(
            owner=validated_owner,
            limit=validated_limit,
        )
        return MaintenanceStats(
            completed=revoked.completed + regular.completed,
            deferred=revoked.deferred + regular.deferred,
        )
    finally:
        if owns_runtime:
            worker_runtime.close()
            active_runtime.close()


def _revoke_all(worker_runtime: WorkerRuntime, owner: str, limit: int) -> MaintenanceStats:
    """部署显式撤销：pending 快照逐 request task（受 limit 限制）。

    revoke 覆盖 pending（不只自动取消候选）：与 `revoke_all_pending` 共享
    `_list_pending_ids` 查询，但每个 request 独立 run_task 保证 lease/fence。
    """
    validated_owner, validated_limit = _validate_maintenance_inputs(owner, limit)
    if validated_limit == 0:
        return MaintenanceStats(completed=0, deferred=0)
    requests = worker_runtime.runtime.resolve("quota_request_service")
    engine = worker_runtime.runtime.resolve("database_engine")
    worker = UsageMaintenanceWorker(worker_runtime)
    with engine.begin() as connection:
        pending_ids = requests._list_pending_ids(connection)  # noqa: SLF001 - shared query
    completed = 0
    deferred = 0
    for qr_id in pending_ids[:validated_limit]:
        try:
            worker_runtime.run_task(
                f"{_CANCEL_TASK_PREFIX}{qr_id}",
                validated_owner,
                partial(worker._cancel, quota_request_id=qr_id, reason=_REVOCATION_REASON),
            )
            completed += 1
        except (FenceViolation, LeaseUnavailable, PlatformError):
            deferred += 1
    return MaintenanceStats(completed=completed, deferred=deferred)


class _SafeArgumentParser(argparse.ArgumentParser):
    def error(self, _message: str) -> NoReturn:
        self.exit(2, f"{self.prog}: invalid arguments\n")


def main(argv: list[str] | None = None) -> None:
    parser = _SafeArgumentParser(prog="ragqs-usage-maintenance")
    parser.add_argument(
        "--revoke-all",
        action="store_true",
        help="cancel all pending quota requests (deployment revocation)",
    )
    args = parser.parse_args(argv)
    try:
        settings = load_platform_settings()
    except ValueError:
        # 配置加载边界只暴露安全 ValueError；CLI 仍统一固定 stderr。
        print("ragqs-usage-maintenance: configuration error", file=sys.stderr)
        raise SystemExit(2) from None
    try:
        _require_maintenance_key(settings)
    except ValueError:
        # 受保护 CLI fail-closed：密钥只经 settings/env 提供，绝不进参数/日志。
        print("ragqs-usage-maintenance: RAG_MAINTENANCE_KEY is required", file=sys.stderr)
        raise SystemExit(2) from None
    stats = run_usage_maintenance_once(settings, revoke_all=args.revoke_all)
    _logger.info("usage maintenance completed=%s deferred=%s", stats.completed, stats.deferred)


if __name__ == "__main__":
    main()
