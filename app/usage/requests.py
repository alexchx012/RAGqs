"""QuotaRequestService：配额申请创建 / 读取 / 个人快照（Task 9）。

语义（正式 spec §5 + Task 9 约束，旧 brief 示例陷阱已审计修正）：
- create 只允许 user/minister（403 forbidden_target）；requested_pages 严格非 bool
  int 1..500（422 validation_error）；Idempotency-Key 非空且限长（422）。
- 幂等使用平台真实 `SqlAlchemyTransactionManager.reserve_idempotency /
  commit_idempotency`（platform_idempotency 表），不复制第二套幂等表逻辑。
  稳定 scope = `{user_id}:POST:/quota-requests:`，canonical request hash =
  HMAC-SHA256(b"ragqs-quota-request-v1", canonical_json({user_id, requested_pages}))。
  同 key 同指纹 → 重放返回原 201 payload（reserve 的 IdempotencyReservation.result，
  即持久化的 response_json）；同 key 异指纹 / in_progress → 409
  idempotency_key_conflict（不暴露内部状态）。
- create 在 calendar lock 之后只读取一次 DB clock：同一 `now` 同时用于
  quota_period、created_at_utc、updated_at_utc（review：防业务月界不一致，旧实现
  period 与 created_at 各读一次 clock，跨月边界时可能分属不同业务时点）。
- 同申请人当前业务月最多一条 pending：partial unique index
  `uq_quota_request_pending`（applicant_user_id, quota_period) WHERE status='pending'
  兜底；INSERT 冲突 IntegrityError → 409 pending_request_exists。捕获后立即转换为
  PlatformError 并 re-raise，由事务 __exit__ 回滚（绝不继续使用 failed transaction，
  PG aborted transaction 风险）。
- 申请创建不预留、不改 quota projection/ledger（只写 quota_request +
  platform_idempotency）。
- me：事务内 calendar.lock_or_verify → QuotaService.read_snapshot（Task 7 精确形状）
  → 非 unlimited 角色填充 pending_request（read_pending）；ops/admin 为 None。
  reset_at/period/business timezone/calendar version 服务端计算；read path 不创建
  projection（read_snapshot 缺投影返回基线且不建行）。
- read_pending 的 created_at 由 DB 回读：SQLite 返回 naive datetime（DateTime(
  timezone=True) 不保留 tz），统一 `_utc` 补 UTC 后再 isoformat，保证 RFC3339
  `+00:00` 与 create 的 created_at 逐字节一致（PG 原生 tz-aware 亦走同路径）。
- Task 10 审批（正式 spec §5 + Task 10 约束，旧 brief 示例陷阱已审计修正）：
  - summary 所有登录角色可读：仅精确 ops 统计当前业务月 pending（spec L50：admin
    的 quota_pending 为 0，旧示例把 admin 放行到真实计数已修正）；user/minister/
    admin 的 quota_pending 为 0；submission_pending 为当前角色可见范围
    （public/department:*、public、本部门）的真实 pending 投稿数。
  - list_quota_requests 仅精确 ops（403 forbidden_target，admin 也 403）；status
    仅 pending/approved/rejected/cancelled（否则 422）；created_at 升序；
    display_name 读 identity_user_table（缺失回落 applicant id）；current_usage 用
    QuotaService.read_snapshot 逐 applicant（读路径不创建投影）；created_at/
    reviewed_at RFC3339。
  - approve/reject 均要求 Idempotency-Key（非空、≤256）与 expected_version 严格
    非 bool **正整数**（422，**reserve 前**校验）；approve approved_pages 缺省
    requested、严格非 bool int 且 >=1（动态 `<= requested_pages` 在锁后校验）；
    reject 无自由文本 reason。request_id 非空且最多 64 字符；幂等 scope 是
    `approval:` + actor/action/target canonical fingerprint（固定长度且保持域隔离）；
    **审批专用 canonical hash `_approval_hash`**（HMAC-SHA256(b"ragqs-quota-approval-v1",
    canonical_json({user_id, action, target, expected_version, approved_pages}))）——
    覆盖 actor.user_id / action（approve|reject）/ target request_id /
    expected_version / approved_pages 是否省略及原始值（None 与 0/-1 由
    canonical_json typed-tag 区分，**不用 truthiness 归一化**）；同 key 任一稳定
    请求事实变化均 409 idempotency_key_conflict；同 key 同事实重放原 200 payload；
    in_progress → 409。幂等 reserve 先于状态/version 校验（task brief）。
  - approve 事务内固定顺序：reserve → 锁 quota_request 行（with_for_update）→
    校验 pending（否则 409 already_processed）→ version==expected（否则 409
    version_conflict）→ 申请人 lifecycle_status=='active'（否则 409
    quota_request_not_approvable）→ **单一 calendar lock + 单一 DB now**（锁行后
    取得一次 lock/now，`_require_approvable` 返回并复用于 period 校验 /
    append_credit 的 calendar_lock+now / reviewed_at / updated_at / audit
    occurred_at——approve/reject 均不再二次读取 clock）→ 目标期仍当前
    （calendar.period_for(now)==request.quota_period，否则同 409）→
    append_credit（namespace=quota_request/source=request_id，同一 TxManager
    connection）→ UPDATE request（version+1/approved/approver 快照/approved_pages/
    credit_entry_id/reviewed_at/updated_at）→ platform_audit INSERT（details_json={}，
    request_id=current_context().request_id or "req_system"）→
    outbox.enqueue(connection=tx.connection, ...) → commit_idempotency → 200。
    enqueue 抛错 → 整个事务回滚（含幂等 reserve）。
  - reject 同骨架：无 credit；result="quota_request_rejected"；
    event_type="quota_rejected"；返回 {id, version, status: "rejected"}。
  - _cancel_transition 是 Task 11 maintenance 的内部条件更新边界（稳定 lease/fence
    语义，仅 status='pending' 才推进 cancelled）；本任务不开放申请人取消 API。
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import UTC, datetime

from sqlalchemy import Engine, and_, func, select, update
from sqlalchemy.engine import Connection
from sqlalchemy.exc import IntegrityError

from app.documents.schema import knowledge_submissions_table
from app.identity.schema import identity_user_table
from app.identity.service import AuthPrincipal
from app.platform.context import current_context
from app.platform.database import SqlAlchemyTransactionManager, platform_audit_table
from app.platform.errors import PlatformError
from app.platform.persistence import IdempotencyConflict

from ._fingerprint import canonical_json, ledger_fingerprint
from .calendar import BusinessCalendarService, CalendarLock
from .ledger import OwnershipSnapshot
from .ports import OutboxEnqueuePort
from .quota import QuotaService
from .schema import quota_request_table

_REQUEST_KEY = b"ragqs-quota-request-v1"
# 审批专用 hash 域（Task 10 review）：与 create 的 _REQUEST_KEY 分开，payload 结构
# 也不同（见 _approval_hash），绝不复用 create 的 _request_hash。
_APPROVAL_KEY = b"ragqs-quota-approval-v1"
_ENDPOINT = "POST:/quota-requests"
# 与底层 platform_idempotency.idempotency_key String(256) 对齐：最多 256 字符
# （正式 spec 无 255 产品上限；256 接受、257 拒绝，保留非空/whitespace 策略）。
_IDEMPOTENCY_KEY_MAX_LENGTH = 256


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _require_text(value: object, name: str, max_len: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PlatformError("validation_error", f"{name} must be a non-empty string", {}, 422)
    text_value = value.strip()
    if len(text_value) > max_len:
        raise PlatformError(
            "validation_error", f"{name} must be at most {max_len} characters", {}, 422
        )
    return text_value


class QuotaRequestService:
    def __init__(
        self,
        engine: Engine,
        clock,
        calendar: BusinessCalendarService,
        quota: QuotaService,
        outbox: OutboxEnqueuePort,
    ) -> None:
        self._engine = engine
        self._clock = clock
        self.calendar = calendar
        self._quota = quota
        self._outbox = outbox
        self._tx = SqlAlchemyTransactionManager(engine, clock)

    def _request_hash(self, actor: AuthPrincipal, requested_pages: int) -> str:
        canonical = canonical_json(
            {"user_id": actor.user_id, "requested_pages": requested_pages}
        ).encode("utf-8")
        return hmac.new(_REQUEST_KEY, canonical, digestmod=hashlib.sha256).hexdigest()

    def create(self, *, actor: AuthPrincipal, requested_pages: int, idempotency_key: str) -> dict:
        if not idempotency_key or not idempotency_key.strip():
            raise PlatformError("validation_error", "Idempotency-Key is required", {}, 422)
        key = idempotency_key.strip()
        if len(key) > _IDEMPOTENCY_KEY_MAX_LENGTH:
            raise PlatformError(
                "validation_error",
                f"Idempotency-Key must be at most {_IDEMPOTENCY_KEY_MAX_LENGTH} characters",
                {},
                422,
            )
        if actor.role not in {"user", "minister"}:
            raise PlatformError(
                "forbidden_target", "Quota requests are not allowed for this role", {}, 403
            )
        if isinstance(requested_pages, bool) or not isinstance(requested_pages, int):
            raise PlatformError("validation_error", "requested_pages must be an integer", {}, 422)
        if not 1 <= requested_pages <= 500:
            raise PlatformError("validation_error", "requested_pages must be 1..500", {}, 422)
        scope = f"{actor.user_id}:{_ENDPOINT}:"
        request_hash = self._request_hash(actor, requested_pages)
        with self._tx.begin() as tx:
            try:
                reservation = tx.reserve_idempotency(
                    scope=scope, key=key, request_hash=request_hash
                )
                if reservation.replayed:
                    return dict(reservation.result)
                if reservation.in_progress:
                    # M5：spec 无 in_progress 专用 code，映射到正式 409 码，
                    # 不暴露内部状态。
                    raise PlatformError(
                        "idempotency_key_conflict",
                        "Idempotency key is already in progress",
                        {},
                        409,
                    )
                connection = tx.connection
                assert connection is not None
                lock = self.calendar.lock_or_verify(connection)
                # 只读取一次 clock：同一 now 同时计算 quota_period / created_at_utc /
                # updated_at_utc，保证 period 与 created_at 属于同一业务时点。
                now = self._clock.now_utc(connection)
                period = self.calendar.period_for(lock, now)
                request_id = f"qr_{secrets.token_urlsafe(9)}"
                try:
                    connection.execute(
                        quota_request_table.insert().values(
                            quota_request_id=request_id,
                            version=1,
                            applicant_user_id=actor.user_id,
                            applicant_role_snapshot=actor.role,
                            applicant_department_id_snapshot=actor.department_id,
                            quota_period=period,
                            business_calendar_version_id=lock.version_id,
                            requested_pages=requested_pages,
                            status="pending",
                            idempotency_fingerprint=request_hash,
                            created_at_utc=now,
                            updated_at_utc=now,
                        )
                    )
                except IntegrityError as exc:
                    # partial unique index：同申请人同月已有 pending。
                    # 立即 re-raise，由事务 __exit__ 回滚（不继续使用 failed
                    # transaction，PG 下 aborted transaction 不可再执行语句）。
                    raise PlatformError(
                        "pending_request_exists",
                        "A pending quota request already exists for this period",
                        {},
                        409,
                    ) from exc
                response = {
                    "id": request_id,
                    "version": 1,
                    "status": "pending",
                    "requested_pages": requested_pages,
                    "quota_period": period,
                    "created_at": now.isoformat(),
                }
                tx.commit_idempotency(
                    scope=scope, key=key, request_hash=request_hash, result=response
                )
                return response
            except IdempotencyConflict as exc:
                raise PlatformError(
                    "idempotency_key_conflict",
                    "Idempotency key was reused with a different request",
                    {},
                    409,
                ) from exc

    def read_pending(
        self, connection: Connection, *, user_id: str, quota_period: str
    ) -> dict | None:
        row = (
            connection.execute(
                select(quota_request_table).where(
                    and_(
                        quota_request_table.c.applicant_user_id == user_id,
                        quota_request_table.c.quota_period == quota_period,
                        quota_request_table.c.status == "pending",
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            return None
        return {
            "id": str(row["quota_request_id"]),
            "version": int(row["version"]),
            "requested_pages": int(row["requested_pages"]),
            "quota_period": str(row["quota_period"]),
            "created_at": _utc(row["created_at_utc"]).isoformat(),
        }

    def me(self, *, actor: AuthPrincipal) -> dict:
        with self._tx.begin() as tx:
            connection = tx.connection
            assert connection is not None
            # read_snapshot 内部已调用 calendar.lock_or_verify（创建/锁定日历单行并
            # 校验 timezone），此处不再重复调用。
            snapshot = self._quota.read_snapshot(
                connection, quota_subject_user_id=actor.user_id, role=actor.role
            )
            pending = None
            if actor.role not in {"ops", "admin"}:
                pending = self.read_pending(
                    connection, user_id=actor.user_id, quota_period=snapshot.quota_period
                )
        return {
            "used": snapshot.used,
            "base_limit": snapshot.base_limit,
            "extra_granted": snapshot.extra_granted,
            "effective_limit": snapshot.effective_limit,
            "unlimited": snapshot.unlimited,
            "reset_at": snapshot.reset_at.isoformat(),
            "business_timezone": snapshot.business_timezone,
            "quota_period": snapshot.quota_period,
            "business_calendar_version_id": snapshot.business_calendar_version_id,
            "pending_request": pending,
        }

    def _require_idempotency_key(self, idempotency_key: str) -> str:
        if not idempotency_key or not idempotency_key.strip():
            raise PlatformError("validation_error", "Idempotency-Key is required", {}, 422)
        key = idempotency_key.strip()
        if len(key) > _IDEMPOTENCY_KEY_MAX_LENGTH:
            raise PlatformError(
                "validation_error",
                f"Idempotency-Key must be at most {_IDEMPOTENCY_KEY_MAX_LENGTH} characters",
                {},
                422,
            )
        return key

    @staticmethod
    def _require_expected_version(value: object) -> int:
        """expected_version 严格非 bool **正整数**（Task 10 review：reserve 前校验，
        与 Pydantic strict int ge=1 对齐——True/False/0/-1/字符串/小数均 422）。"""
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise PlatformError(
                "validation_error", "expected_version must be a positive integer", {}, 422
            )
        return value

    @staticmethod
    def _require_approved_pages(value: object) -> int | None:
        """approved_pages 可选；提供时严格非 bool int 且 >=1（review：0/-1 与 None 绝不
        hash 碰撞——0/-1 在 reserve 前 422；动态 `<= requested_pages` 在锁后校验）。"""
        if value is not None and (
            isinstance(value, bool) or not isinstance(value, int) or value < 1
        ):
            raise PlatformError(
                "validation_error", "approved_pages must be a positive integer", {}, 422
            )
        return value

    def _approval_hash(
        self,
        *,
        action: str,
        actor_user_id: str,
        request_id: str,
        expected_version: int,
        approved_pages: int | None,
    ) -> str:
        """审批专用 canonical hash（Task 10 review，不复用 create `_request_hash`）。

        覆盖全部稳定请求事实：actor.user_id / action（approve|reject）/ target
        request_id / expected_version / approved_pages 是否省略及原始值——same key
        任一事实变化均 409 idempotency_key_conflict。approved_pages 用原始值（None
        与 0/-1/正整数由 canonical_json typed-tag 区分：null vs num），不使用
        `approved_pages or -1` truthiness 归一化（否则 None 与 0 同指纹，0 会重放
        None 的审批结果）。
        """
        canonical = canonical_json(
            {
                "user_id": actor_user_id,
                "action": action,
                "target": request_id,
                "expected_version": expected_version,
                "approved_pages": approved_pages,
            }
        ).encode("utf-8")
        return hmac.new(_APPROVAL_KEY, canonical, digestmod=hashlib.sha256).hexdigest()

    @staticmethod
    def _approval_scope(*, actor_user_id: str, action: str, target_id: str) -> str:
        fingerprint = ledger_fingerprint(
            "quota_approval_scope",
            {
                "actor_user_id": actor_user_id,
                "action": action,
                "target_id": target_id,
            },
        )
        return f"approval:{fingerprint}"

    def _reserve_approval(
        self,
        tx,
        *,
        scope: str,
        idempotency_key: str,
        request_hash: str,
    ) -> dict | None:
        try:
            reservation = tx.reserve_idempotency(
                scope=scope, key=idempotency_key, request_hash=request_hash
            )
        except IdempotencyConflict as exc:
            raise PlatformError(
                "idempotency_key_conflict",
                "Idempotency key was reused with a different request",
                {},
                409,
            ) from exc
        if reservation.replayed:
            return dict(reservation.result)
        if reservation.in_progress:
            raise PlatformError(
                "idempotency_key_conflict", "Idempotency key is already in progress", {}, 409
            )
        return None

    @staticmethod
    def _require_ops_approver(actor: AuthPrincipal) -> None:
        if actor.role != "ops":
            raise PlatformError("forbidden_target", "Approval is ops-only", {}, 403)

    def _require_approvable(
        self, tx, *, request_id: str, expected_version: int, actor: AuthPrincipal
    ) -> tuple[dict, CalendarLock, datetime]:
        """锁 quota_request 行并校验审批前提；返回 (row, calendar_lock, now)。

        Task 10 review：锁行后取得**一次** calendar lock + **一次** DB now 并返回，
        approve/reject 复用于 target period 校验、append_credit 的 calendar_lock/now、
        request reviewed_at/updated_at、audit occurred_at——不再二次读取 clock
        （SequenceClock 业务月边界测试实证旧多读实现确定失败）。
        """
        connection = tx.connection
        assert connection is not None
        self._require_ops_approver(actor)
        row = (
            connection.execute(
                select(quota_request_table)
                .where(quota_request_table.c.quota_request_id == request_id)
                .with_for_update()
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise PlatformError("not_found", "Quota request was not found", {}, 404)
        if row["status"] != "pending":
            raise PlatformError("already_processed", "Quota request was already processed", {}, 409)
        if int(row["version"]) != expected_version:
            raise PlatformError(
                "version_conflict", "Quota request version is no longer current", {}, 409
            )
        applicant_row = (
            connection.execute(
                select(identity_user_table).where(
                    identity_user_table.c.id == row["applicant_user_id"]
                )
            )
            .mappings()
            .one_or_none()
        )
        if applicant_row is None or applicant_row["lifecycle_status"] != "active":
            raise PlatformError(
                "quota_request_not_approvable", "Applicant is frozen or deleted", {}, 409
            )
        lock = self.calendar.lock_or_verify(connection)
        now = self._clock.now_utc(connection)
        if self.calendar.period_for(lock, now) != row["quota_period"]:
            raise PlatformError(
                "quota_request_not_approvable", "Target quota period is closed", {}, 409
            )
        return dict(row), lock, now

    def _audit(
        self,
        connection: Connection,
        *,
        actor: AuthPrincipal,
        resource_id: str,
        result: str,
        occurred_at: datetime,
    ) -> None:
        context = current_context()
        connection.execute(
            platform_audit_table.insert().values(
                actor_id=actor.user_id,
                resource_type="quota_request",
                resource_id=resource_id,
                request_id=context.request_id if context is not None else "req_system",
                occurred_at_utc=occurred_at,
                result=result,
                details_json={},
            )
        )

    def approve(
        self,
        *,
        actor: AuthPrincipal,
        request_id: str,
        expected_version: object,
        approved_pages: object | None,
        idempotency_key: str,
    ) -> dict:
        """审批通过：同一事务内 append_credit + 更新 request + 审计 + outbox enqueue。

        enqueue 抛错 → 整个事务（含幂等 reserve）回滚；提交后的投递失败不在本
        任务范围（由 outbox change 负责）。
        """
        self._require_ops_approver(actor)
        request_id = _require_text(request_id, "request_id", 64)
        key = self._require_idempotency_key(idempotency_key)
        expected_version = self._require_expected_version(expected_version)
        approved_pages = self._require_approved_pages(approved_pages)
        request_hash = self._approval_hash(
            action="approve",
            actor_user_id=actor.user_id,
            request_id=request_id,
            expected_version=expected_version,
            approved_pages=approved_pages,
        )
        scope = self._approval_scope(
            actor_user_id=actor.user_id,
            action="approve",
            target_id=request_id,
        )
        with self._tx.begin() as tx:
            try:
                replay = self._reserve_approval(
                    tx,
                    scope=scope,
                    idempotency_key=key,
                    request_hash=request_hash,
                )
                if replay is not None:
                    return replay
                row, lock, now = self._require_approvable(
                    tx, request_id=request_id, expected_version=expected_version, actor=actor
                )
                pages = row["requested_pages"] if approved_pages is None else approved_pages
                if not 1 <= pages <= int(row["requested_pages"]):
                    raise PlatformError(
                        "validation_error", "approved_pages must be 1..requested", {}, 422
                    )
                connection = tx.connection
                assert connection is not None
                ownership = OwnershipSnapshot(
                    actor_user_id=actor.user_id,
                    actor_role_snapshot=actor.role,
                    actor_department_id_snapshot=actor.department_id,
                    quota_subject_user_id=str(row["applicant_user_id"]),
                    cost_center_key=f"user:{row['applicant_user_id']}",
                )
                credit_id = self._quota.append_credit(
                    connection,
                    quota_subject_user_id=str(row["applicant_user_id"]),
                    quota_period=str(row["quota_period"]),
                    pages=pages,
                    adjustment_source_namespace="quota_request",
                    adjustment_source_id=request_id,
                    ownership=ownership,
                    calendar_lock=lock,
                    now=now,
                )
                new_version = int(row["version"]) + 1
                connection.execute(
                    update(quota_request_table)
                    .where(quota_request_table.c.quota_request_id == request_id)
                    .values(
                        version=new_version,
                        status="approved",
                        approver_user_id=actor.user_id,
                        approver_role_snapshot=actor.role,
                        approved_pages=pages,
                        credit_entry_id=credit_id,
                        reviewed_at_utc=now,
                        updated_at_utc=now,
                    )
                )
                self._audit(
                    connection,
                    actor=actor,
                    resource_id=request_id,
                    result="quota_request_approved",
                    occurred_at=now,
                )
                payload = {"request_id": request_id}
                self._outbox.enqueue(
                    connection=connection,
                    event_type="quota_approved",
                    aggregate_type="quota_request",
                    aggregate_id=request_id,
                    transition_version=new_version,
                    recipient_user_id=str(row["applicant_user_id"]),
                    occurred_at=now,
                    payload_fingerprint=ledger_fingerprint("quota_approved", payload),
                    payload=payload,
                )
                response = {
                    "id": request_id,
                    "version": new_version,
                    "status": "approved",
                    "approved_pages": pages,
                    "credit_entry_id": credit_id,
                    "quota_period": str(row["quota_period"]),
                }
                tx.commit_idempotency(
                    scope=scope,
                    key=key,
                    request_hash=request_hash,
                    result=response,
                )
                return response
            except IdempotencyConflict as exc:
                raise PlatformError(
                    "idempotency_key_conflict",
                    "Idempotency key was reused with a different request",
                    {},
                    409,
                ) from exc

    def reject(
        self,
        *,
        actor: AuthPrincipal,
        request_id: str,
        expected_version: object,
        idempotency_key: str,
    ) -> dict:
        """拒绝申请：无 credit；更新 request + 审计 + outbox enqueue 同一事务。"""
        self._require_ops_approver(actor)
        request_id = _require_text(request_id, "request_id", 64)
        key = self._require_idempotency_key(idempotency_key)
        expected_version = self._require_expected_version(expected_version)
        request_hash = self._approval_hash(
            action="reject",
            actor_user_id=actor.user_id,
            request_id=request_id,
            expected_version=expected_version,
            approved_pages=None,
        )
        scope = self._approval_scope(
            actor_user_id=actor.user_id,
            action="reject",
            target_id=request_id,
        )
        with self._tx.begin() as tx:
            try:
                replay = self._reserve_approval(
                    tx,
                    scope=scope,
                    idempotency_key=key,
                    request_hash=request_hash,
                )
                if replay is not None:
                    return replay
                row, lock, now = self._require_approvable(
                    tx, request_id=request_id, expected_version=expected_version, actor=actor
                )
                connection = tx.connection
                assert connection is not None
                new_version = int(row["version"]) + 1
                connection.execute(
                    update(quota_request_table)
                    .where(quota_request_table.c.quota_request_id == request_id)
                    .values(
                        version=new_version,
                        status="rejected",
                        approver_user_id=actor.user_id,
                        approver_role_snapshot=actor.role,
                        reviewed_at_utc=now,
                        updated_at_utc=now,
                    )
                )
                self._audit(
                    connection,
                    actor=actor,
                    resource_id=request_id,
                    result="quota_request_rejected",
                    occurred_at=now,
                )
                payload = {"request_id": request_id}
                self._outbox.enqueue(
                    connection=connection,
                    event_type="quota_rejected",
                    aggregate_type="quota_request",
                    aggregate_id=request_id,
                    transition_version=new_version,
                    recipient_user_id=str(row["applicant_user_id"]),
                    occurred_at=now,
                    payload_fingerprint=ledger_fingerprint("quota_rejected", payload),
                    payload=payload,
                )
                response = {"id": request_id, "version": new_version, "status": "rejected"}
                tx.commit_idempotency(
                    scope=scope,
                    key=key,
                    request_hash=request_hash,
                    result=response,
                )
                return response
            except IdempotencyConflict as exc:
                raise PlatformError(
                    "idempotency_key_conflict",
                    "Idempotency key was reused with a different request",
                    {},
                    409,
                ) from exc

    def summary(self, *, actor: AuthPrincipal) -> dict:
        """审批摘要：仅 ops 统计当前业务月 pending（正式 spec L50：admin 为 0）。

        submission_pending 为当前角色可见范围内的真实 pending 投稿数
        （scope 与投稿审核列表 list_approvals 一致）。
        """
        if actor.role != "ops":
            quota_pending = 0
        else:
            with self._tx.begin() as tx:
                connection = tx.connection
                assert connection is not None
                lock = self.calendar.lock_or_verify(connection)
                period = self.calendar.period_for(lock, self._clock.now_utc(connection))
                quota_pending = connection.execute(
                    select(func.count())
                    .select_from(quota_request_table)
                    .where(
                        and_(
                            quota_request_table.c.status == "pending",
                            quota_request_table.c.quota_period == period,
                        )
                    )
                ).scalar_one()
        role = str(getattr(actor, "role", ""))
        space_filter = None
        if role == "admin":
            space_filter = (
                knowledge_submissions_table.c.space_id == "public"
            ) | knowledge_submissions_table.c.space_id.like("department:%")
        elif role == "ops":
            space_filter = knowledge_submissions_table.c.space_id == "public"
        elif role == "minister":
            space_filter = knowledge_submissions_table.c.space_id == (
                f"department:{getattr(actor, 'department_id', None)}"
            )
        submission_pending = 0
        if space_filter is not None:
            with self._engine.connect() as connection:
                submission_pending = connection.execute(
                    select(func.count())
                    .select_from(knowledge_submissions_table)
                    .where(knowledge_submissions_table.c.status == "pending", space_filter)
                ).scalar_one()
        return {"quota_pending": int(quota_pending), "submission_pending": int(submission_pending)}

    def list_quota_requests(self, *, actor: AuthPrincipal, status: str) -> list[dict]:
        """审批列表：仅精确 ops；created_at 升序；current_usage 读路径不创建投影。"""
        if actor.role != "ops":
            raise PlatformError("forbidden_target", "Approval list is ops-only", {}, 403)
        if status not in {"pending", "approved", "rejected", "cancelled"}:
            raise PlatformError(
                "validation_error",
                "status must be pending, approved, rejected or cancelled",
                {},
                422,
            )
        with self._tx.begin() as tx:
            connection = tx.connection
            assert connection is not None
            rows = (
                connection.execute(
                    select(quota_request_table)
                    .where(quota_request_table.c.status == status)
                    .order_by(quota_request_table.c.created_at_utc)
                )
                .mappings()
                .all()
            )
            # 批量预取申请人显示名与 quota 快照，避免列表页逐行
            # identity/snapshot 查询。
            user_ids = {str(row["applicant_user_id"]) for row in rows}
            display_names = (
                {
                    str(user_row["id"]): str(user_row["display_name"])
                    for user_row in connection.execute(
                        select(
                            identity_user_table.c.id, identity_user_table.c.display_name
                        ).where(identity_user_table.c.id.in_(user_ids))
                    )
                    .mappings()
                    .all()
                }
                if user_ids
                else {}
            )
            snapshot_entries = {
                (str(row["applicant_user_id"]), str(row["applicant_role_snapshot"]))
                for row in rows
            }
            snapshots = self._quota.read_snapshots(connection, entries=list(snapshot_entries))
            items: list[dict] = []
            for row in rows:
                applicant_id = str(row["applicant_user_id"])
                display_name = display_names.get(applicant_id, applicant_id)
                snapshot = snapshots[(applicant_id, str(row["applicant_role_snapshot"]))]
                items.append(
                    {
                        "id": str(row["quota_request_id"]),
                        "version": int(row["version"]),
                        "status": str(row["status"]),
                        "applicant": {
                            "id": str(row["applicant_user_id"]),
                            "display_name": display_name,
                        },
                        "current_usage": {
                            "used": snapshot.used,
                            "effective_limit": snapshot.effective_limit,
                        },
                        "requested_pages": int(row["requested_pages"]),
                        "approved_pages": (
                            int(row["approved_pages"])
                            if row["approved_pages"] is not None
                            else None
                        ),
                        "quota_period": str(row["quota_period"]),
                        "created_at": _utc(row["created_at_utc"]).isoformat(),
                        "reviewed_at": (
                            _utc(row["reviewed_at_utc"]).isoformat()
                            if row["reviewed_at_utc"] is not None
                            else None
                        ),
                    }
                )
            return items

    def _cancel_transition(
        self, connection: Connection, *, request_id: str, reason: str, now: datetime
    ) -> int:
        """H5：稳定条件更新——只有 status='pending' 才推进为 cancelled（lease/fence 语义）。

        Task 11 maintenance 复用；本任务不开放申请人取消 API（spec §5：申请人不得
        主动取消，只允许自动/显式撤销路径调用）。
        """
        result = connection.execute(
            update(quota_request_table)
            .where(
                and_(
                    quota_request_table.c.quota_request_id == request_id,
                    quota_request_table.c.status == "pending",
                )
            )
            .values(
                version=quota_request_table.c.version + 1,
                status="cancelled",
                cancel_reason=reason,
                reviewed_at_utc=now,
                updated_at_utc=now,
            )
        )
        return int(result.rowcount)

    @staticmethod
    def _require_cancel_reason(reason: str) -> None:
        """cancel_reason 列 String(64)：非空且限长（Task 11：reason 合法边界）。"""
        if not reason or not reason.strip():
            raise ValueError("cancel reason must not be empty")
        if len(reason) > 64:
            raise ValueError("cancel reason must be at most 64 characters")

    def _list_pending_ids(self, connection: Connection) -> list[str]:
        """全部 pending 的 quota_request_id（revoke_all 与 worker revoke 共享同一查询）。"""
        rows = (
            connection.execute(
                select(quota_request_table.c.quota_request_id).where(
                    quota_request_table.c.status == "pending"
                )
            )
            .scalars()
            .all()
        )
        return [str(row) for row in rows]

    def list_cancel_candidates(
        self, connection: Connection, *, calendar_lock: CalendarLock, now: datetime
    ) -> list[dict]:
        """H5：取消候选仅 pending；关闭业务月优先于申请人 inactive。

        每行逐条判定 reason：`quota_period < calendar.period_for(lock, now)` →
        `period_closed`；否则申请人 `identity_user_table.lifecycle_status != "active"`
        → `applicant_inactive`；否则跳过。调用方事务内执行（不自行 begin）。
        """
        rows = (
            connection.execute(
                select(quota_request_table).where(quota_request_table.c.status == "pending")
            )
            .mappings()
            .all()
        )
        candidates: list[dict] = []
        current_period = self.calendar.period_for(calendar_lock, now)
        for row in rows:
            qr_id = str(row["quota_request_id"])
            # 关闭业务月优先：period 已关闭的申请不再看申请人状态（period_closed 是
            # 决定性理由，先判定并 continue）。
            if str(row["quota_period"]) < current_period:
                candidates.append({"quota_request_id": qr_id, "reason": "period_closed"})
                continue
            user_row = (
                connection.execute(
                    select(identity_user_table).where(
                        identity_user_table.c.id == row["applicant_user_id"]
                    )
                )
                .mappings()
                .one_or_none()
            )
            if user_row is None or user_row["lifecycle_status"] != "active":
                candidates.append({"quota_request_id": qr_id, "reason": "applicant_inactive"})
        return candidates

    def cancel_for_account(
        self, connection: Connection, *, user_id: str, reason: str, now: datetime
    ) -> int:
        """账户级取消：申请人全部 pending → cancelled（调用方事务，条件更新幂等）。"""
        self._require_cancel_reason(reason)
        affected = 0
        pending_ids = (
            connection.execute(
                select(quota_request_table.c.quota_request_id).where(
                    and_(
                        quota_request_table.c.applicant_user_id == user_id,
                        quota_request_table.c.status == "pending",
                    )
                )
            )
            .scalars()
            .all()
        )
        for qr_id in pending_ids:
            affected += self._cancel_transition(
                connection, request_id=str(qr_id), reason=reason, now=now
            )
        return affected

    def cancel_closed_periods(
        self, connection: Connection, *, calendar_lock: CalendarLock, now: datetime
    ) -> int:
        """目标业务月已关闭的 pending → cancelled（reason=period_closed）。"""
        affected = 0
        for candidate in self.list_cancel_candidates(
            connection, calendar_lock=calendar_lock, now=now
        ):
            if candidate["reason"] == "period_closed":
                affected += self._cancel_transition(
                    connection,
                    request_id=candidate["quota_request_id"],
                    reason="period_closed",
                    now=now,
                )
        return affected

    def revoke_all_pending(self, connection: Connection, *, reason: str, now: datetime) -> int:
        """部署显式撤销：全部 pending → cancelled（调用方事务，条件更新幂等）。"""
        self._require_cancel_reason(reason)
        affected = 0
        for qr_id in self._list_pending_ids(connection):
            affected += self._cancel_transition(
                connection, request_id=qr_id, reason=reason, now=now
            )
        return affected
