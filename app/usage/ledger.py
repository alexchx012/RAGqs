"""UsageLedger：provider_call 生命周期 / usage 提交（Task 5，正式 spec 修订版 + review agent-11）。

语义（正式 spec 优先于旧 task brief/plan）：
- 每个物理发送使用调用方稳定 provider_call_id；prepare/dispatching 各自独立短事务
  且发送前提交；网络 I/O 绝不持 DB 事务；每次 provider 短重试使用新 ID。
- 结果已知（成功或已收到 4xx/503 等失败）→ 独立事务原子 completed + provider_usage；
  sent=False → not_sent 无 usage；sent=True 但结果无法确认 → unknown。
- 公开 wrapper 自带短事务；另提供 connection-aware 内部方法（H7 + review agent-11）：
  `complete_provider_call_in_transaction` 锁并校验 dispatching/unknown（completed 幂等
  重放），完整幂等 fingerprint 插入/复用 usage，条件更新 completed，全部同一调用方
  事务；fence 有效时与业务结果原子提交；业务失败三者一起回滚；fence 失效时只用公开
  wrapper 独立保存 completed+usage。账本写入不以当前用户状态或 fence 为前提。
- complete/recover 先检查既有 usage：同完整 canonical fingerprint 复用 persisted ID，
  异事实 409 ledger_invariant_conflict；首次才状态转换；始终返回 persisted ID。
- 指纹覆盖所有不可变调用/usage 事实（ownership/space 授权/fence/cost center、
  provider_request_id、actual started、measurement+sources、price/currency/cost、
  result、effective_period）。recorded/completed/created 为记录时点产物，不进入指纹
  ——否则重放（时钟推进后）会因记录时间不同而误判冲突，破坏 spec L11 幂等复用。
- actual send time：`mark_dispatching(provider_call_id, started_at_provider=...)` 在即将
  物理发送的生命周期入口接受 callback/延迟值，并于事务内显式持久化稳定
  started_at_utc；completion/reconcile 必须
  要求/使用它（行内 started 缺失且未显式提供 → 422）。选价与 effective period 用
  actual started；recorded 用 DB now。dispatching_at 仅记录流转时间。
- 价格锁：同一事务内 select + lock_for_usage（使用其返回的版本对象）；若锁定前版本
  已被 close（price_interval_conflict），重新 select+lock，有限重试（3 次）。
- measurement_sources：key 必须在固定 meter 集合内；每个非 null meter 必须有合法
  source（provider_reported / client_measured / estimated）；null 与 0 严格区分。
- local usage：started_at_utc 必填（调用者稳定提供）；四元组唯一；本地 V1 无价格。
- adjustment/cost adjustment：追加式、稳定 source/allocation/referenced event；
  usage_adjustment 只允许引用原始 provider_usage/local_usage，cost_adjustment 只允许
  引用原始 provider_usage（禁止 adjustment chain）；currency 复用正式 ISO 校验。
  delta 是**有符号 correction**（正式 spec：完整计量或账单差异只能以引用原事实的
  adjustment 追加，不更新原事件）：usage delta 为 signed BigInteger（[-2**63,
  2**63-1]，非零）；cost amount_delta 为 signed Numeric(30,10)（可为负表示退款/冲减；
  amount-only 对账的非负约束不适用于 adjustment）。
  幂等优先级：同四元组已有行时先按完整 canonical fingerprint（含
  referenced_event_id/ownership/result/extra）比较，相同复用 persisted ID、异指纹
  409 ledger_invariant_conflict；引用缺失/kind/meter/currency 语义校验只在首次插入
  路径执行，不得抢先返回 404/422。
"""

from __future__ import annotations

import logging
import secrets
import uuid
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from dataclasses import field as dataclass_field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Protocol

from sqlalchemy import Engine, and_, delete, select, update
from sqlalchemy.engine import Connection

from app.platform.errors import PlatformError

from ._fingerprint import ledger_fingerprint
from ._sql import _insert_do_nothing
from .calendar import BusinessCalendarService
from .price import CostStatus, PriceCatalogService, PriceVersion, normalize_currency_code
from .schema import provider_call_table, usage_event_table, usage_reconciliation_table

_logger = logging.getLogger(__name__)

_SOURCE_WHITELIST = frozenset({"provider_reported", "client_measured", "estimated"})
_PROVIDER_METER_FIELDS = (
    "input_tokens",
    "prompt_cache_hit_tokens",
    "prompt_cache_miss_tokens",
    "output_tokens",
    "reasoning_tokens",
    "image_count",
    "visual_input_tokens",
    "embedding_input_tokens",
    "vector_count",
)
_LOCAL_METER_FIELDS = (
    "item_count",
    "page_count",
    "input_bytes",
    "gpu_milliseconds",
    "cpu_milliseconds",
    "peak_vram_bytes",
)
_ADJUSTMENT_DELTA_FIELDS = _PROVIDER_METER_FIELDS + _LOCAL_METER_FIELDS
# 计量数量上限：与 price.estimate_cost 的支持上限一致；真实读数远低于该值，
# 超限稳定 422，避免跨方言（PG BigInteger / SQLite）存储差异。
_MAX_METER_VALUE = 10**18
# BigInteger 64 位有符号范围（usage adjustment delta：signed correction，可为负）。
_BIGINT_MIN = -(2**63)
_BIGINT_MAX = 2**63 - 1
# Numeric(30,10)：绝对值 < 1e20 且 scale <= 10（金额可写性，与 estimate_cost 一致）。
_MONEY_UPPER_BOUND = Decimal("1e20")
# price select+lock 有限重试次数（锁定前被 close 的竞态窗口）。
_PRICE_LOCK_RETRIES = 3
# adjustment 允许引用的原始 event_kind（禁止 adjustment chain）。
_ALLOWED_REFERENCE_KINDS: dict[str, frozenset[str]] = {
    "usage_adjustment": frozenset({"provider_usage", "local_usage"}),
    "cost_adjustment": frozenset({"provider_usage"}),
}


class Clock(Protocol):
    def now_utc(self, connection: Connection | None = None) -> datetime: ...


@dataclass(frozen=True, slots=True)
class OwnershipSnapshot:
    actor_user_id: str
    actor_role_snapshot: str
    actor_department_id_snapshot: str | None
    quota_subject_user_id: str | None
    cost_center_key: str
    space_id: str | None = None
    space_kind: str | None = None
    space_owner_user_id: str | None = None
    authorization_version: int | None = None
    fence_token: int | None = None
    source_space_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ProviderMeasurement:
    input_tokens: int | None
    prompt_cache_hit_tokens: int | None
    prompt_cache_miss_tokens: int | None
    output_tokens: int | None
    reasoning_tokens: int | None
    image_count: int | None
    visual_input_tokens: int | None
    embedding_input_tokens: int | None
    vector_count: int | None
    measurement_sources: dict[str, str]


@dataclass(frozen=True, slots=True)
class LocalMeasurement:
    item_count: int | None
    page_count: int | None
    input_bytes: int | None
    gpu_milliseconds: int | None
    cpu_milliseconds: int | None
    peak_vram_bytes: int | None
    measurement_sources: dict[str, str] = dataclass_field(default_factory=dict)


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _as_utc(value: datetime, what: str) -> datetime:
    if not isinstance(value, datetime):
        raise PlatformError("validation_error", f"{what} must be a datetime", {}, 422)
    return _utc(value)


def _require_text(value, name: str, max_len: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PlatformError("validation_error", f"{name} must be a non-empty string", {}, 422)
    text_value = value.strip()
    if len(text_value) > max_len:
        raise PlatformError(
            "validation_error", f"{name} must be at most {max_len} characters", {}, 422
        )
    return text_value


def _optional_text(value, name: str, max_len: int) -> str | None:
    if value is None:
        return None
    return _require_text(value, name, max_len)


def _ownership_json(ownership: OwnershipSnapshot) -> dict[str, Any]:
    """Serialize ownership as JSON facts without changing historic empty snapshots."""
    values = asdict(ownership)
    source_space_ids = values.pop("source_space_ids")
    if source_space_ids:
        values["source_space_ids"] = list(source_space_ids)
    return values


def _require_replay_generation(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise PlatformError(
            "validation_error", "replay_generation must be a non-negative integer", {}, 422
        )
    return value


def _matches(table: Any, index_elements: list[str], values: dict[str, Any]):
    return and_(*[table.c[name] == values[name] for name in index_elements])


def _validate_money_amount(amount: Decimal, what: str) -> Decimal:
    """金额校验：有限 Decimal 且适配 Numeric(30,10)（abs < 1e20、scale <= 10）。"""
    if not isinstance(amount, Decimal) or not amount.is_finite():
        raise PlatformError("validation_error", f"{what} must be a finite Decimal", {}, 422)
    if amount >= _MONEY_UPPER_BOUND or amount <= -_MONEY_UPPER_BOUND:
        raise PlatformError(
            "validation_error", f"{what} must fit the Numeric(30,10) storage range", {}, 422
        )
    if amount.as_tuple().exponent < -10:  # type: ignore[operator]
        raise PlatformError(
            "validation_error",
            f"{what} scale must not exceed 10 decimal places",
            {},
            422,
        )
    return amount


def _stable_reconciliation_id(provider_call_id: str) -> str:
    """固定长度对账行 ID：rc_（3 字符）+ UUID5 hex（32 字符）= 35 <= 64。"""
    digest = uuid.uuid5(uuid.NAMESPACE_DNS, f"provider_call:{provider_call_id}").hex
    return f"rc_{digest}"


def _assert_reconciliation_content(existing: Mapping[str, Any], values: Mapping[str, Any]) -> None:
    """对账行内容比对：amount_only_json / ownership_json 必须一致，否则账本不变量错误。"""
    if existing["amount_only_json"] != values.get("amount_only_json") or existing[
        "ownership_json"
    ] != values.get("ownership_json"):
        raise PlatformError(
            "ledger_invariant_conflict",
            "Reconciliation fact does not match the existing ledger row",
            {},
            409,
        )


class UsageLedger:
    def __init__(
        self,
        engine: Engine,
        clock: Clock,
        calendar: BusinessCalendarService,
        prices: PriceCatalogService,
        *,
        invariant_alert_port: Any | None = None,
    ) -> None:
        self._engine = engine
        self.clock = clock
        self.calendar = calendar
        self.prices = prices
        self._invariant_alert_port = invariant_alert_port

    # ---- 内部 connection 方法（H7：供调用方业务事务内联） ----

    def _event_fingerprint(self, kind: str, payload: Mapping) -> str:
        return ledger_fingerprint(kind, dict(payload))

    def _alert_usage_invariant_conflict(
        self, exc: PlatformError, *, provider_call_id: str | None = None
    ) -> None:
        """A45：唯一键异指纹冲突的回滚后 best-effort ops 告警。

        只能在 wrapper 边界（engine.begin 已回滚、连接已归还）调用：adapter 用独立
        短事务发布，失败仅记日志——绝不掩盖原 409，也绝不影响回滚语义。只对
        {"index": [...]} 形状的 usage_event 指纹冲突告警（state/id-reuse 冲突除外）。
        """
        if self._invariant_alert_port is None or exc.code != "ledger_invariant_conflict":
            return
        index = exc.details.get("index")
        if not isinstance(index, list) or not index:
            return
        try:
            self._invariant_alert_port.publish_usage_ledger_invariant_conflict(
                unique_key_fields=[str(name) for name in index],
                provider_call_id=provider_call_id,
            )
        except Exception:
            _logger.warning("usage ledger invariant alert could not be published", exc_info=True)

    def _require_call(self, connection: Connection, provider_call_id: str) -> dict[str, Any]:
        row = (
            connection.execute(
                select(provider_call_table).where(
                    provider_call_table.c.provider_call_id == provider_call_id
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise PlatformError("provider_call_not_found", "Provider call was not found", {}, 404)
        return dict(row)

    def _insert_usage_once(
        self, connection: Connection, *, index_elements: list[str], values: dict[str, Any]
    ) -> str:
        """C3：先查既有行（唯一键），存在→比完整 canonical fingerprint；不存在→insert。

        始终返回 persisted 行的 usage_event_id（重放复用 / 首次插入 / 并发竞态回读）。
        """
        existing = (
            connection.execute(
                select(usage_event_table).where(_matches(usage_event_table, index_elements, values))
            )
            .mappings()
            .one_or_none()
        )
        if existing is not None:
            if existing["event_fingerprint"] != values["event_fingerprint"]:
                raise PlatformError(
                    "ledger_invariant_conflict",
                    "Usage event fingerprint does not match the existing ledger row",
                    {"index": index_elements},
                    409,
                )
            return str(existing["usage_event_id"])
        inserted = _insert_do_nothing(connection, usage_event_table, values, index_elements)
        if not inserted:
            # 并发竞态（PG：ON CONFLICT DO NOTHING 等待先行事务提交后返回 0）：
            # 回读既有行并比对指纹。
            existing = (
                connection.execute(
                    select(usage_event_table).where(
                        _matches(usage_event_table, index_elements, values)
                    )
                )
                .mappings()
                .one()
            )
            if existing["event_fingerprint"] != values["event_fingerprint"]:
                raise PlatformError(
                    "ledger_invariant_conflict",
                    "Usage event fingerprint does not match the existing ledger row",
                    {"index": index_elements},
                    409,
                )
            return str(existing["usage_event_id"])
        persisted = connection.execute(
            select(usage_event_table.c.usage_event_id).where(
                _matches(usage_event_table, index_elements, values)
            )
        ).scalar_one()
        return str(persisted)

    def _insert_reconciliation_once(
        self, connection: Connection, *, index_elements: list[str], values: dict[str, Any]
    ) -> tuple[str, bool]:
        """对账分组稳定事实：先查后插，返回 (persisted_id, inserted_new)。

        同内容重放返回既有行 (id, False)；异内容（amount/currency/ownership）是账本
        不变量错误 409 ledger_invariant_conflict。usage_reconciliation 无 fingerprint
        列，按内容（amount_only_json + ownership_json）比较。
        """
        existing = (
            connection.execute(
                select(usage_reconciliation_table).where(
                    _matches(usage_reconciliation_table, index_elements, values)
                )
            )
            .mappings()
            .one_or_none()
        )
        if existing is not None:
            _assert_reconciliation_content(dict(existing), values)
            return str(existing["id"]), False
        inserted = _insert_do_nothing(
            connection, usage_reconciliation_table, values, index_elements
        )
        if not inserted:
            existing = (
                connection.execute(
                    select(usage_reconciliation_table).where(
                        _matches(usage_reconciliation_table, index_elements, values)
                    )
                )
                .mappings()
                .one()
            )
            _assert_reconciliation_content(dict(existing), values)
            return str(existing["id"]), False
        return str(values["id"]), True

    @staticmethod
    def _validate_ownership(ownership: OwnershipSnapshot) -> None:
        _require_text(ownership.actor_user_id, "actor_user_id", 64)
        _require_text(ownership.actor_role_snapshot, "actor_role_snapshot", 32)
        _optional_text(ownership.actor_department_id_snapshot, "actor_department_id_snapshot", 64)
        _require_text(ownership.cost_center_key, "cost_center_key", 128)
        _optional_text(ownership.quota_subject_user_id, "quota_subject_user_id", 64)
        _optional_text(ownership.space_id, "space_id", 128)
        _optional_text(ownership.space_kind, "space_kind", 32)
        _optional_text(ownership.space_owner_user_id, "space_owner_user_id", 64)
        if ownership.authorization_version is not None and (
            isinstance(ownership.authorization_version, bool)
            or not isinstance(ownership.authorization_version, int)
        ):
            raise PlatformError(
                "validation_error", "authorization_version must be an integer", {}, 422
            )
        if ownership.fence_token is not None and (
            isinstance(ownership.fence_token, bool) or not isinstance(ownership.fence_token, int)
        ):
            raise PlatformError("validation_error", "fence_token must be an integer", {}, 422)
        if not isinstance(ownership.source_space_ids, tuple) or any(
            not isinstance(space_id, str) or not space_id.strip()
            for space_id in ownership.source_space_ids
        ):
            raise PlatformError(
                "validation_error", "source_space_ids must be a tuple of non-empty strings", {}, 422
            )

    @staticmethod
    def _validate_meter_values(
        fields: tuple[str, ...], values: Mapping[str, Any], what: str
    ) -> None:
        for field in fields:
            value = values.get(field)
            if value is None:
                continue
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or value < 0
                or value > _MAX_METER_VALUE
            ):
                raise PlatformError(
                    "validation_error",
                    f"{what} {field!r} must be a non-negative integer or null",
                    {},
                    422,
                )

    def _validate_provider_measurement(self, measurement: ProviderMeasurement) -> None:
        if not isinstance(measurement.measurement_sources, dict):
            raise PlatformError(
                "validation_error", "measurement_sources must be an object", {}, 422
            )
        unknown_keys = set(measurement.measurement_sources) - set(_PROVIDER_METER_FIELDS)
        if unknown_keys:
            raise PlatformError(
                "validation_error",
                f"measurement_sources contains unknown meter keys: {sorted(unknown_keys)}",
                {},
                422,
            )
        self._validate_meter_values(_PROVIDER_METER_FIELDS, asdict(measurement), "meter")

    def _validate_local_measurement(self, measurement: LocalMeasurement) -> None:
        self._validate_meter_values(_LOCAL_METER_FIELDS, asdict(measurement), "meter")

    def _availability(self, measurement: ProviderMeasurement) -> dict[str, CostStatus]:
        """每个非 null meter 必须有合法 source；source 值全部白名单。null→unavailable。"""
        for field in _PROVIDER_METER_FIELDS:
            if getattr(measurement, field) is not None:
                source = measurement.measurement_sources.get(field)
                if source not in _SOURCE_WHITELIST:
                    raise PlatformError(
                        "validation_error",
                        f"measurement_sources must provide a legal source for non-null "
                        f"meter {field!r}",
                        {},
                        422,
                    )
        for source in measurement.measurement_sources.values():
            if source not in _SOURCE_WHITELIST:
                raise PlatformError("validation_error", "Invalid measurement source value", {}, 422)
        result: dict[str, CostStatus] = {}
        for field in _PROVIDER_METER_FIELDS:
            value = getattr(measurement, field)
            if value is None:
                result[field] = "unavailable"
            elif measurement.measurement_sources.get(field) == "estimated":
                result[field] = "partial"
            else:
                result[field] = "complete"
        return result

    def _measured(self, measurement: ProviderMeasurement) -> dict[str, Decimal]:
        """非 None meter 才计价（null 严格区分 0，未知绝不当 0）。"""
        result: dict[str, Decimal] = {}
        for field in _PROVIDER_METER_FIELDS:
            value = getattr(measurement, field)
            if value is not None:
                result[field] = Decimal(str(value))
        return result

    def _locked_price(
        self,
        connection: Connection,
        *,
        provider: str,
        model: str,
        operation: str,
        at_utc: datetime,
    ) -> PriceVersion:
        """同一事务内 select + lock_for_usage；锁定前版本被 close → 重新 select+lock（有限重试）。

        使用 lock_for_usage 返回的（锁定后）版本对象计价，不丢弃锁定返回值。
        """
        attempts = 0
        while True:
            attempts += 1
            price = self.prices.select_for(
                connection,
                provider=provider,
                model=model,
                operation=operation,
                at_utc=at_utc,
            )
            try:
                return self.prices.lock_for_usage(connection, price.id, at_utc)
            except PlatformError as exc:
                if exc.code != "price_interval_conflict" or attempts >= _PRICE_LOCK_RETRIES:
                    raise

    def _resolve_started(
        self, call: Mapping[str, Any], started_at_utc: datetime | None
    ) -> datetime:
        """actual send time：唯一权威是 mark_dispatching 持久化的行值（二轮复审 Important 3）。

        显式传入若与持久值不一致必须拒绝（不能静默覆盖）；持久值缺失时显式值可作兜底
        （兼容旧数据），两者皆无 → 422。
        """
        row_started = call["started_at_utc"]
        if row_started is not None:
            persisted = _utc(row_started)
            if started_at_utc is not None:
                explicit = _as_utc(started_at_utc, "started_at_utc")
                if explicit != persisted:
                    raise PlatformError(
                        "validation_error",
                        "started_at_utc does not match the persisted send time",
                        {"persisted_started_at_utc": str(persisted)},
                        422,
                    )
            return persisted
        if started_at_utc is not None:
            return _as_utc(started_at_utc, "started_at_utc")
        raise PlatformError(
            "validation_error",
            "started_at_utc is required: mark_dispatching must persist the actual send time",
            {},
            422,
        )

    def _complete_usage(
        self,
        connection: Connection,
        *,
        provider_call_id: str,
        measurement: ProviderMeasurement,
        ownership: OwnershipSnapshot,
        result: str,
        provider_request_id: str | None,
        started_at_utc: datetime | None,
    ) -> str:
        """connection-aware：写 provider_usage 事实（价格锁 + 指纹判定），不做状态流转。

        供调用方业务事务内联（fence 有效时与 synthetic 业务结果同事务）；公开 wrapper
        在首次插入后才做状态转换。始终返回 persisted 行的 usage_event_id。
        """
        call = self._require_call(connection, provider_call_id)
        result = _require_text(result, "result", 32)
        self._validate_ownership(ownership)
        self._validate_provider_measurement(measurement)
        now = self.clock.now_utc(connection)
        started = self._resolve_started(call, started_at_utc)
        lock = self.calendar.lock_or_verify(connection)
        lock_period = self.calendar.period_for(lock, started)
        recorded_period = self.calendar.period_for(lock, now)
        availability = self._availability(measurement)
        measured = self._measured(measurement)
        # actual started 选价 + 同事务锁价（禁 close 竞态）；使用锁定后的版本对象。
        price = self._locked_price(
            connection,
            provider=call["provider"],
            model=call["model"],
            operation=call["operation"],
            at_utc=started,
        )
        amount, cost_status = self.prices.estimate_cost(
            price,
            measured,
            availability=availability,
        )
        replay_generation = _require_replay_generation(call["replay_generation"])
        fingerprint_payload = {
            "provider_call_id": provider_call_id,
            "provider": call["provider"],
            "model": call["model"],
            "operation": call["operation"],
            "execution_kind": call["execution_kind"],
            "execution_id": call["execution_id"],
            "attempt_id": call["attempt_id"],
            "generation_id": call["generation_id"],
            "resource_id": call["resource_id"],
            "deadline_utc": _utc(call["deadline_utc"]),
            "ownership": _ownership_json(ownership),
            "provider_request_id": provider_request_id,
            "started_at_utc": started,
            "measurement": asdict(measurement),
            "result": result,
            "price_version_id": price.id,
            "currency_code": price.currency_code,
            "estimated_cost_amount": amount,
            "estimated_cost_status": cost_status,
            "effective_period": lock_period,
        }
        if replay_generation:
            fingerprint_payload["replay_generation"] = replay_generation
        fingerprint = self._event_fingerprint(
            "provider_usage",
            fingerprint_payload,
        )
        event_id = f"ue_{secrets.token_urlsafe(9)}"
        persisted_id = self._insert_usage_once(
            connection,
            index_elements=["provider_call_id"],
            values={
                "usage_event_id": event_id,
                "event_kind": "provider_usage",
                "provider_call_id": provider_call_id,
                "provider": call["provider"],
                "model": call["model"],
                "operation": call["operation"],
                "provider_request_id": provider_request_id,
                "price_version_id": price.id,
                "currency_code": price.currency_code,
                "estimated_cost_amount": amount,
                "estimated_cost_status": cost_status,
                "input_tokens": measurement.input_tokens,
                "prompt_cache_hit_tokens": measurement.prompt_cache_hit_tokens,
                "prompt_cache_miss_tokens": measurement.prompt_cache_miss_tokens,
                "output_tokens": measurement.output_tokens,
                "reasoning_tokens": measurement.reasoning_tokens,
                "image_count": measurement.image_count,
                "visual_input_tokens": measurement.visual_input_tokens,
                "embedding_input_tokens": measurement.embedding_input_tokens,
                "vector_count": measurement.vector_count,
                "measurement_sources": measurement.measurement_sources,
                "execution_kind": call["execution_kind"],
                "execution_id": call["execution_id"],
                "attempt_id": call["attempt_id"],
                "generation_id": call["generation_id"],
                "resource_id": call["resource_id"],
                "replay_generation": replay_generation,
                "cost_center_key": ownership.cost_center_key,
                "result": result,
                "event_fingerprint": fingerprint,
                "ownership_json": _ownership_json(ownership),
                "started_at_utc": started,
                "completed_at_utc": now,
                "effective_calendar_version_id": lock.version_id,
                "effective_at_utc": started,
                "effective_period": lock_period,
                "recorded_calendar_version_id": lock.version_id,
                "recorded_at_utc": now,
                "recorded_period": recorded_period,
                "created_at_utc": now,
            },
        )
        return persisted_id

    def complete_provider_call_in_transaction(
        self,
        connection: Connection,
        *,
        provider_call_id: str,
        measurement: ProviderMeasurement,
        ownership: OwnershipSnapshot,
        result: str,
        provider_request_id: str | None = None,
        started_at_utc: datetime | None = None,
    ) -> str:
        """connection-aware（review agent-11 #1 + 二轮复审 #2）：行锁 provider_call →
        移除既有 amount-only（互斥/升级）→ 完整幂等指纹插入/复用 usage → 条件更新
        completed；全部同一调用方事务。

        - 行锁：`SELECT ... FOR UPDATE`（PG 行锁；SQLite 忽略子句，由首个写获得写锁），
          使 amount-only 的 claim UPDATE 与本方法串行化——二者互斥，终态 usage 与
          amount-only 绝不同时提交。
        - amount-only 升级：若对账已记录 amount-only（调用仍 unknown），本方法删除该
          对账行并写完整 usage（完整计量取代仅金额事实）。
        - 业务失败/并发冲突时整个调用方事务回滚（含 usage 与状态）；fence 失效时调用方
          可用公开 wrapper（独立短事务）保存 completed+usage。
        """
        row = (
            connection.execute(
                select(provider_call_table.c.status)
                .where(provider_call_table.c.provider_call_id == provider_call_id)
                .with_for_update()
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise PlatformError("provider_call_not_found", "Provider call was not found", {}, 404)
        if row["status"] not in ("dispatching", "unknown", "completed"):
            raise PlatformError(
                "provider_call_state_conflict",
                "Provider call is not dispatching or unknown",
                {},
                409,
            )
        # 终态 usage 与 amount-only 互斥：任何既有 amount-only 在本事务内被移除（升级）。
        connection.execute(
            delete(usage_reconciliation_table).where(
                and_(
                    usage_reconciliation_table.c.provider_call_id == provider_call_id,
                    usage_reconciliation_table.c.reconciliation_kind == "amount_only",
                )
            )
        )
        event_id = self._complete_usage(
            connection,
            provider_call_id=provider_call_id,
            measurement=measurement,
            ownership=ownership,
            result=result,
            provider_request_id=provider_request_id,
            started_at_utc=started_at_utc,
        )
        updated = connection.execute(
            update(provider_call_table)
            .where(
                and_(
                    provider_call_table.c.provider_call_id == provider_call_id,
                    provider_call_table.c.status.in_(("dispatching", "unknown")),
                )
            )
            .values(status="completed", completed_at_utc=self.clock.now_utc(connection))
        ).rowcount
        if (
            updated != 1
            and self._require_call(connection, provider_call_id)["status"] != "completed"
        ):
            raise PlatformError(
                "provider_call_state_conflict",
                "Provider call is not in the expected state",
                {},
                409,
            )
        return event_id

    def mark_not_sent_in_transaction(self, connection: Connection, provider_call_id: str) -> None:
        """connection-aware：prepared/dispatching/unknown → not_sent。

        同时清除 unknown_at 与 started_at_utc：schema 约束要求 not_sent 行既无
        unknown_at 也无 started_at（从未实际发送，二轮复审 Important 3）。
        同一事务删除该调用既有 amount-only 对账事实（三轮复审 R6：与 completion 升级
        删除语义一致——amount-only 不得与 not_sent/completed 终态共存）。
        """
        # 先以 provider_call 条件 UPDATE 建立与 amount-only claim 相同的线性化顺序：
        # - 本事务先赢时，后续 amount claim 等待后因 status!=unknown 返回 0；
        # - amount claim 先赢时，本 UPDATE 等待其提交，再更新状态并删除刚提交的 amount。
        # 不得先 DELETE：当尚无 amount 行时 DELETE 不会锁住 provider_call，PG 下会留下
        # not_sent + amount_only 的并发交错。
        updated = connection.execute(
            update(provider_call_table)
            .where(
                and_(
                    provider_call_table.c.provider_call_id == provider_call_id,
                    provider_call_table.c.status.in_(("prepared", "dispatching", "unknown")),
                )
            )
            .values(
                status="not_sent",
                not_sent_at_utc=self.clock.now_utc(connection),
                unknown_at_utc=None,
                started_at_utc=None,
            )
        ).rowcount
        if (
            updated != 1
            and self._require_call(connection, provider_call_id)["status"] != "not_sent"
        ):
            raise PlatformError(
                "provider_call_state_conflict",
                "Provider call is not in the expected state",
                {},
                409,
            )
        # 更新/确认 not_sent 后仍持有 provider_call 锁；删除先前或刚提交的 amount-only。
        connection.execute(
            delete(usage_reconciliation_table).where(
                and_(
                    usage_reconciliation_table.c.provider_call_id == provider_call_id,
                    usage_reconciliation_table.c.reconciliation_kind == "amount_only",
                )
            )
        )

    def mark_unknown_in_transaction(
        self,
        connection: Connection,
        provider_call_id: str,
        *,
        allow_terminal: bool = False,
    ) -> bool:
        """connection-aware：dispatching → unknown；返回是否本次流转。

        默认只允许已 unknown 幂等。`allow_terminal=True` 仅供 complete callback 抛错后的
        不确定提交清理：若事务其实已提交 completed/not_sent，则保持既有终态且不重复写入。
        """
        updated = connection.execute(
            update(provider_call_table)
            .where(
                and_(
                    provider_call_table.c.provider_call_id == provider_call_id,
                    provider_call_table.c.status == "dispatching",
                )
            )
            .values(status="unknown", unknown_at_utc=self.clock.now_utc(connection))
        ).rowcount
        if updated == 1:
            return True
        status = self._require_call(connection, provider_call_id)["status"]
        if status == "unknown" or (allow_terminal and status in {"completed", "not_sent"}):
            return False
        raise PlatformError(
            "provider_call_state_conflict",
            "Provider call is not in the expected state",
            {},
            409,
        )

    def mark_expired_dispatching_unknown_in_transaction(
        self,
        connection: Connection,
        provider_call_id: str,
        *,
        older_than_utc: datetime,
        current_utc: datetime,
    ) -> bool:
        """仅当 dispatch 已 stale 且 persisted deadline 已过时，原子流转 unknown。

        条件 UPDATE 是 PG 行锁后的权威重验；若等待期间调用已终态化则稳定冲突，若被
        evidence-driven 路径先转 unknown 则幂等返回 False。
        """
        updated = connection.execute(
            update(provider_call_table)
            .where(
                and_(
                    provider_call_table.c.provider_call_id == provider_call_id,
                    provider_call_table.c.status == "dispatching",
                    provider_call_table.c.dispatching_at_utc <= _utc(older_than_utc),
                    provider_call_table.c.deadline_utc <= _utc(current_utc),
                )
            )
            .values(status="unknown", unknown_at_utc=self.clock.now_utc(connection))
        ).rowcount
        if updated == 1:
            return True
        status = self._require_call(connection, provider_call_id)["status"]
        if status == "unknown":
            return False
        raise PlatformError(
            "provider_call_state_conflict",
            "Provider call is not eligible for automatic reconciliation",
            {},
            409,
        )

    def record_reconciliation_amount_only(
        self,
        connection: Connection,
        *,
        call: Mapping[str, Any],
        amount: Decimal,
        currency_code: str,
        ownership: OwnershipSnapshot,
    ) -> tuple[str, bool] | None:
        """amount-only 对账入组（connection-aware）：稳定事实 + 内容指纹判定。

        线性化 claim（二轮复审 Important 2）：先条件 UPDATE provider_call 的
        `last_reconcile_attempt_at_utc`（仅当 status='unknown'），作为锁持有点——
        PG 行锁 / SQLite 写锁与 completion 的 FOR UPDATE/写入互斥；条件失败（行已被
        completed/not_sent 终态化）→ 返回 None，不写 amount-only。终态 usage 与
        amount-only 由该条件串行化，绝不并存。

        校验：amount 有限且适配 Numeric(30,10) 且**非负**（成本原始金额为非负，负差异
        由 adjustment 表达）；currency 复用正式 ISO 验证；ownership 完整校验。
        effective 用 actual started/dispatch time，recorded 用 DB now。
        返回 (persisted_id, inserted_new) 或 None（调用已终态，跳过）；同内容重放
        (id, False)，异内容 409（整体事务回滚含 claim）。
        """
        amount = _validate_money_amount(amount, "amount")
        if amount < 0:
            raise PlatformError("validation_error", "amount must be non-negative", {}, 422)
        currency = normalize_currency_code(currency_code)
        self._validate_ownership(ownership)
        call_id = str(call["provider_call_id"])
        # 线性化 claim：仅当调用仍为 unknown 时持有锁并触碰尝试时间。
        claimed = connection.execute(
            update(provider_call_table)
            .where(
                and_(
                    provider_call_table.c.provider_call_id == call_id,
                    provider_call_table.c.status == "unknown",
                )
            )
            .values(last_reconcile_attempt_at_utc=self.clock.now_utc(connection))
        ).rowcount
        if claimed != 1:
            return None  # 已终态化（completed/not_sent）→ 跳过，不写 amount-only
        started = _utc(call["started_at_utc"] or call["dispatching_at_utc"])
        now = self.clock.now_utc(connection)
        lock = self.calendar.lock_or_verify(connection)
        effective_period = self.calendar.period_for(lock, started)
        recorded_period = self.calendar.period_for(lock, now)
        return self._insert_reconciliation_once(
            connection,
            index_elements=["id"],
            values={
                "id": _stable_reconciliation_id(call_id),
                "provider_call_id": call_id,
                "provider": call["provider"],
                "model": call["model"],
                "operation": call["operation"],
                "execution_kind": call["execution_kind"],
                "execution_id": call["execution_id"],
                "attempt_id": call["attempt_id"],
                "reconciliation_kind": "amount_only",
                "amount_only_json": {
                    "amount": str(amount),
                    "currency_code": currency,
                },
                "ownership_json": _ownership_json(ownership),
                "effective_calendar_version_id": lock.version_id,
                "effective_at_utc": started,
                "effective_period": effective_period,
                "recorded_calendar_version_id": lock.version_id,
                "recorded_at_utc": now,
                "recorded_period": recorded_period,
                "created_at_utc": now,
            },
        )

    @staticmethod
    def _validate_reference_compatibility(
        event_kind: str, ref: Mapping[str, Any], extra_values: Mapping[str, Any]
    ) -> None:
        """按被引用事件类型校验 adjustment 字段兼容（二轮复审 Important 4）。

        - usage_adjustment：delta meter 必须属于被引用事件的 meter 集合
          （provider_usage → provider meters；local_usage → local meters）。
        - cost_adjustment：currency 必须与被引用 provider usage 的 currency 一致
          （本规格无跨币种模型，不得放行）。
        """
        if event_kind == "usage_adjustment":
            referenced_kind = ref["event_kind"]
            allowed_meters = (
                set(_PROVIDER_METER_FIELDS)
                if referenced_kind == "provider_usage"
                else set(_LOCAL_METER_FIELDS)
            )
            for field in extra_values:
                if field not in allowed_meters:
                    raise PlatformError(
                        "validation_error",
                        f"usage_adjustment on a {referenced_kind} event may not adjust "
                        f"meter {field!r}",
                        {},
                        422,
                    )
        elif event_kind == "cost_adjustment":
            referenced_currency = ref["currency_code"]
            if extra_values.get("currency_code") != referenced_currency:
                raise PlatformError(
                    "validation_error",
                    "cost_adjustment currency must match the referenced provider usage "
                    "currency (no cross-currency model)",
                    {"referenced_currency_code": referenced_currency},
                    422,
                )

    def _append_adjustment(
        self,
        *,
        event_kind: str,
        referenced_event_id: str,
        adjustment_source_namespace: str,
        adjustment_source_id: str,
        adjustment_allocation_key: str,
        ownership: OwnershipSnapshot,
        result: str,
        extra_values: dict[str, Any],
        connection: Connection | None = None,
    ) -> str:
        """追加式 adjustment：继承被引用事件的 effective 事实；recorded 用当前时间。

        唯一键 (event_kind, adjustment_source_namespace, adjustment_source_id,
        adjustment_allocation_key)。幂等优先级（Task 6 review R2）：同四元组已有行时
        先按完整 canonical fingerprint（含 referenced_event_id/ownership/result/extra）
        比较——相同复用 persisted ID，异指纹统一 409 ledger_invariant_conflict；
        引用缺失/kind/meter/currency 语义校验只影响首次插入，不得抢先于幂等判定返回
        404/422（否则同一 source key 换引用会错误否定既有事实）。首次插入才校验：
        只允许引用原始 event_kind（禁止 adjustment chain）、meter 与引用事件类别兼容、
        cost_adjustment 的 currency 与被引用 provider usage 一致。
        """
        if connection is not None:
            return self._append_adjustment_in_transaction(
                connection,
                event_kind=event_kind,
                referenced_event_id=referenced_event_id,
                adjustment_source_namespace=adjustment_source_namespace,
                adjustment_source_id=adjustment_source_id,
                adjustment_allocation_key=adjustment_allocation_key,
                ownership=ownership,
                result=result,
                extra_values=extra_values,
            )
        try:
            with self._engine.begin() as owned_connection:
                return self._append_adjustment_in_transaction(
                    owned_connection,
                    event_kind=event_kind,
                    referenced_event_id=referenced_event_id,
                    adjustment_source_namespace=adjustment_source_namespace,
                    adjustment_source_id=adjustment_source_id,
                    adjustment_allocation_key=adjustment_allocation_key,
                    ownership=ownership,
                    result=result,
                    extra_values=extra_values,
                )
        except PlatformError as exc:
            self._alert_usage_invariant_conflict(exc)
            raise

    def _append_adjustment_in_transaction(
        self,
        connection: Connection,
        *,
        event_kind: str,
        referenced_event_id: str,
        adjustment_source_namespace: str,
        adjustment_source_id: str,
        adjustment_allocation_key: str,
        ownership: OwnershipSnapshot,
        result: str,
        extra_values: dict[str, Any],
    ) -> str:
        """Append an adjustment using a caller-owned transaction when required."""
        referenced_event_id = _require_text(referenced_event_id, "referenced_event_id", 64)
        adjustment_source_namespace = _require_text(
            adjustment_source_namespace, "adjustment_source_namespace", 64
        )
        adjustment_source_id = _require_text(adjustment_source_id, "adjustment_source_id", 128)
        adjustment_allocation_key = _require_text(
            adjustment_allocation_key, "adjustment_allocation_key", 64
        )
        allowed_kinds = _ALLOWED_REFERENCE_KINDS[event_kind]
        fingerprint = self._event_fingerprint(
            event_kind,
            {
                "referenced_event_id": referenced_event_id,
                "source": (
                    adjustment_source_namespace,
                    adjustment_source_id,
                    adjustment_allocation_key,
                ),
                "ownership": _ownership_json(ownership),
                "result": result,
                "extra": extra_values,
            },
        )
        index_elements = [
            "event_kind",
            "adjustment_source_namespace",
            "adjustment_source_id",
            "adjustment_allocation_key",
        ]
        index_values = {
            "event_kind": event_kind,
            "adjustment_source_namespace": adjustment_source_namespace,
            "adjustment_source_id": adjustment_source_id,
            "adjustment_allocation_key": adjustment_allocation_key,
        }
        now = self.clock.now_utc(connection)
        lock = self.calendar.lock_or_verify(connection)
        # 1) 四元组幂等判定（先于引用事件语义校验）：同指纹复用 persisted ID，
        #    异指纹 409（不同 referenced_event_id/ownership/result/currency/extra
        #    均落入异指纹分支）。
        existing = (
            connection.execute(
                select(usage_event_table).where(
                    _matches(usage_event_table, index_elements, index_values)
                )
            )
            .mappings()
            .one_or_none()
        )
        if existing is not None:
            if existing["event_fingerprint"] != fingerprint:
                raise PlatformError(
                    "ledger_invariant_conflict",
                    "Usage event fingerprint does not match the existing ledger row",
                    {"index": index_elements},
                    409,
                )
            return str(existing["usage_event_id"])
        # 2) 首次插入：引用事件语义校验（存在性/kind/meter/currency）。
        ref = (
            connection.execute(
                select(usage_event_table).where(
                    usage_event_table.c.usage_event_id == referenced_event_id
                )
            )
            .mappings()
            .one_or_none()
        )
        if ref is None:
            raise PlatformError(
                "usage_event_not_found", "Referenced usage event was not found", {}, 404
            )
        if ref["event_kind"] not in allowed_kinds:
            raise PlatformError(
                "validation_error",
                f"{event_kind} may only reference original "
                f"{sorted(allowed_kinds)} events (got {ref['event_kind']!r})",
                {},
                422,
            )
        self._validate_reference_compatibility(event_kind, dict(ref), extra_values)
        event_id = f"ue_{secrets.token_urlsafe(9)}"
        persisted_id = self._insert_usage_once(
            connection,
            index_elements=index_elements,
            values={
                "usage_event_id": event_id,
                "event_kind": event_kind,
                "adjustment_source_namespace": adjustment_source_namespace,
                "adjustment_source_id": adjustment_source_id,
                "adjustment_allocation_key": adjustment_allocation_key,
                "referenced_usage_event_id": referenced_event_id,
                "execution_kind": ref["execution_kind"],
                "execution_id": ref["execution_id"],
                "attempt_id": ref["attempt_id"],
                "generation_id": ref["generation_id"],
                "resource_id": ref["resource_id"],
                "replay_generation": ref["replay_generation"],
                "cost_center_key": ownership.cost_center_key,
                "result": result,
                "event_fingerprint": fingerprint,
                "ownership_json": _ownership_json(ownership),
                "started_at_utc": ref["effective_at_utc"],
                "completed_at_utc": now,
                "effective_calendar_version_id": ref["effective_calendar_version_id"],
                "effective_at_utc": ref["effective_at_utc"],
                "effective_period": ref["effective_period"],
                "recorded_calendar_version_id": lock.version_id,
                "recorded_at_utc": now,
                "recorded_period": self.calendar.period_for(lock, now),
                "created_at_utc": now,
                **extra_values,
            },
        )
        return persisted_id

    # ---- 公开 wrapper（自带短事务） ----

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
    ) -> str:
        """准备 provider call，并保留既有 public wrapper 的 call-id 返回契约。"""
        call_id, _created = self.prepare_provider_call_with_status(
            provider=provider,
            model=model,
            operation=operation,
            execution_kind=execution_kind,
            execution_id=execution_id,
            provider_call_id=provider_call_id,
            attempt_id=attempt_id,
            generation_id=generation_id,
            resource_id=resource_id,
            deadline_utc=deadline_utc,
            request_fingerprint=request_fingerprint,
            replay_generation=replay_generation,
        )
        return call_id

    def prepare_provider_call_with_status(
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
    ) -> tuple[str, bool]:
        """准备 provider call，并返回 ``(call_id, created_by_this_call)``。"""
        provider = _require_text(provider, "provider", 64)
        model = _require_text(model, "model", 128)
        operation = _require_text(operation, "operation", 64)
        execution_kind = _require_text(execution_kind, "execution_kind", 32)
        execution_id = _require_text(execution_id, "execution_id", 128)
        request_fingerprint = _require_text(request_fingerprint, "request_fingerprint", 128)
        attempt_id = _optional_text(attempt_id, "attempt_id", 128)
        generation_id = _optional_text(generation_id, "generation_id", 128)
        if attempt_id is None and generation_id is None:
            raise PlatformError(
                "validation_error",
                "attempt_id or generation_id is required",
                {},
                422,
            )
        resource_id = _optional_text(resource_id, "resource_id", 256)
        replay_generation = _require_replay_generation(replay_generation)
        deadline = _as_utc(deadline_utc, "deadline_utc")
        if provider_call_id is not None:
            provider_call_id = _optional_text(provider_call_id, "provider_call_id", 64)
        with self._engine.begin() as connection:
            call_id = provider_call_id or f"pc_{secrets.token_urlsafe(9)}"
            now = self.clock.now_utc(connection)
            inserted = _insert_do_nothing(
                connection,
                provider_call_table,
                {
                    "provider_call_id": call_id,
                    "provider": provider,
                    "model": model,
                    "operation": operation,
                    "execution_kind": execution_kind,
                    "execution_id": execution_id,
                    "attempt_id": attempt_id,
                    "generation_id": generation_id,
                    "resource_id": resource_id,
                    "replay_generation": replay_generation,
                    "request_fingerprint": request_fingerprint,
                    "deadline_utc": deadline,
                    "status": "prepared",
                    "prepared_at_utc": now,
                    "created_at_utc": now,
                },
                ["provider_call_id"],
            )
            if inserted:
                return call_id, True
            # 重放：全部不可变字段必须一致（review agent-11 #7）。
            existing = self._require_call(connection, call_id)
            if _utc(existing["deadline_utc"]) != deadline:
                raise PlatformError(
                    "ledger_invariant_conflict",
                    "Provider call id reused with a different deadline_utc",
                    {"field": "deadline_utc"},
                    409,
                )
            immutable_fields = {
                "provider": provider,
                "model": model,
                "operation": operation,
                "execution_kind": execution_kind,
                "execution_id": execution_id,
                "attempt_id": attempt_id,
                "generation_id": generation_id,
                "resource_id": resource_id,
                "replay_generation": replay_generation,
                "request_fingerprint": request_fingerprint,
            }
            for field, value in immutable_fields.items():
                if existing[field] != value:
                    raise PlatformError(
                        "ledger_invariant_conflict",
                        f"Provider call id reused with a different {field}",
                        {"field": field},
                        409,
                    )
            return call_id, False

    def mark_dispatching(
        self,
        provider_call_id: str,
        *,
        started_at_provider: Callable[[], datetime] | datetime,
    ) -> bool:
        """锁价并重采样发送时刻，稳定后流转 dispatching 或过期为 not_sent。

        callable 的每个 started 样本都必须由覆盖该时刻的 price lock 保护；每次锁返回后
        重新采样，新样本仍落在已锁价格的半开区间即稳定并持久化最新时刻，跨价格区间才
        按新样本再次锁价。每轮同时用 ledger current clock 和 dynamic started 复查
        persisted deadline，避免锁等待跨过截止时刻。为防恶意 callback 持续跨价格版本，
        最多执行八轮锁后采样，超限以本地合同
        错误 fail closed。固定锁序始终为 price→provider_call。返回 True 表示已提交
        dispatching；False 表示 deadline 已过并在同一事务提交 not_sent。
        """
        with self._engine.begin() as connection:
            if isinstance(started_at_provider, datetime):
                started = _as_utc(started_at_provider, "started_at_utc")
                callable_provider = None
            elif callable(started_at_provider):
                callable_provider = started_at_provider
                started = _as_utc(callable_provider(), "started_at_utc")
            else:
                raise PlatformError(
                    "validation_error",
                    "started_at_provider must be a datetime or a callable returning one",
                    {},
                    422,
                )
            call = self._require_call(connection, provider_call_id)
            if call["status"] != "prepared":
                raise PlatformError(
                    "provider_call_state_conflict",
                    "Provider call is not in the expected state",
                    {},
                    409,
                )
            price_scope = {
                "provider": str(call["provider"]),
                "model": str(call["model"]),
                "operation": str(call["operation"]),
            }
            deadline = _utc(call["deadline_utc"])
            ready = started
            stabilized = callable_provider is None
            for _sample_number in range(8):
                if ready >= deadline:
                    self.mark_not_sent_in_transaction(connection, provider_call_id)
                    return False
                locked_price = self._locked_price(
                    connection,
                    **price_scope,
                    at_utc=ready,
                )
                # Price-row lock waits can cross the absolute deadline. The fixed
                # input remains an immutable fact; callable input is sampled again.
                if _as_utc(self.clock.now_utc(connection), "current_utc") >= deadline:
                    self.mark_not_sent_in_transaction(connection, provider_call_id)
                    return False
                if callable_provider is None:
                    break
                post_lock = _as_utc(callable_provider(), "started_at_utc")
                if post_lock >= deadline:
                    self.mark_not_sent_in_transaction(connection, provider_call_id)
                    return False
                locked_from = _utc(locked_price.effective_from_utc)
                locked_to = (
                    None
                    if locked_price.effective_to_utc is None
                    else _utc(locked_price.effective_to_utc)
                )
                if locked_from <= post_lock and (locked_to is None or post_lock < locked_to):
                    ready = post_lock
                    stabilized = True
                    break
                ready = post_lock
            if not stabilized:
                raise PlatformError(
                    "validation_error",
                    "started_at_provider did not stabilize during price locking",
                    {},
                    422,
                )
            updated = connection.execute(
                update(provider_call_table)
                .where(
                    and_(
                        provider_call_table.c.provider_call_id == provider_call_id,
                        provider_call_table.c.status == "prepared",
                    )
                )
                .values(
                    status="dispatching",
                    dispatching_at_utc=self.clock.now_utc(connection),
                    started_at_utc=ready,
                )
            ).rowcount
            if updated != 1:
                raise PlatformError(
                    "provider_call_state_conflict",
                    "Provider call is not in the expected state",
                    {},
                    409,
                )
            return True

    def complete_provider_call(
        self,
        *,
        provider_call_id: str,
        measurement: ProviderMeasurement,
        ownership: OwnershipSnapshot,
        result: str,
        provider_request_id: str | None = None,
        started_at_utc: datetime | None = None,
    ) -> str:
        """结果已知（成功或已收到 4xx/503 等失败）：原子 completed + provider_usage。"""
        try:
            with self._engine.begin() as connection:
                return self.complete_provider_call_in_transaction(
                    connection,
                    provider_call_id=provider_call_id,
                    measurement=measurement,
                    ownership=ownership,
                    result=result,
                    provider_request_id=provider_request_id,
                    started_at_utc=started_at_utc,
                )
        except PlatformError as exc:
            self._alert_usage_invariant_conflict(exc, provider_call_id=provider_call_id)
            raise

    def mark_not_sent(self, provider_call_id: str) -> None:
        """确定未发送：not_sent，无 usage；已 not_sent 幂等 no-op。"""
        with self._engine.begin() as connection:
            self.mark_not_sent_in_transaction(connection, provider_call_id)

    def mark_not_sent_if_prepared(self, provider_call_id: str) -> None:
        """prepare callback 结果不确定时，仅安全终态化仍为 prepared 的行。

        行不存在表示 prepare 未提交；其他状态表示本 physical call 已由别的已知边界推进，
        均保持不变。PostgreSQL 上先锁行再采样终态时间，避免与 dispatch 并发改写。
        """
        with self._engine.begin() as connection:
            status = connection.execute(
                select(provider_call_table.c.status)
                .where(provider_call_table.c.provider_call_id == provider_call_id)
                .with_for_update()
            ).scalar_one_or_none()
            if status != "prepared":
                return
            updated = connection.execute(
                update(provider_call_table)
                .where(
                    and_(
                        provider_call_table.c.provider_call_id == provider_call_id,
                        provider_call_table.c.status == "prepared",
                    )
                )
                .values(
                    status="not_sent",
                    not_sent_at_utc=self.clock.now_utc(connection),
                    unknown_at_utc=None,
                    started_at_utc=None,
                )
            ).rowcount
            if updated != 1:
                raise PlatformError(
                    "provider_call_state_conflict",
                    "Provider call is not in the expected state",
                    {},
                    409,
                )

    def mark_unknown(self, provider_call_id: str) -> None:
        """结果无法确认 → unknown；已 unknown 幂等 no-op（stale dispatching 转 unknown 可重放）。"""
        with self._engine.begin() as connection:
            self.mark_unknown_in_transaction(connection, provider_call_id)

    def mark_unknown_if_unfinished(self, provider_call_id: str) -> None:
        """complete callback 结果不确定时，仅把仍 dispatching 的调用推进 unknown。

        callback 若已提交 completed/not_sent 后才抛错，本方法保持该终态，不重复写入。
        """
        with self._engine.begin() as connection:
            self.mark_unknown_in_transaction(
                connection,
                provider_call_id,
                allow_terminal=True,
            )

    def recover_unknown_call(
        self,
        *,
        provider_call_id: str,
        measurement: ProviderMeasurement,
        ownership: OwnershipSnapshot,
        result: str,
        provider_request_id: str | None = None,
        started_at_utc: datetime | None = None,
    ) -> str:
        """对账恢复：unknown → completed + usage（幂等，同指纹复用 persisted ID）。"""
        try:
            with self._engine.begin() as connection:
                return self.complete_provider_call_in_transaction(
                    connection,
                    provider_call_id=provider_call_id,
                    measurement=measurement,
                    ownership=ownership,
                    result=result,
                    provider_request_id=provider_request_id,
                    started_at_utc=started_at_utc,
                )
        except PlatformError as exc:
            self._alert_usage_invariant_conflict(exc, provider_call_id=provider_call_id)
            raise

    def submit_local_usage(
        self,
        *,
        execution_kind: str,
        execution_id: str,
        stage: str,
        resource_kind: str,
        measurement: LocalMeasurement,
        ownership: OwnershipSnapshot,
        result: str,
        started_at_utc: datetime,
        replay_generation: int = 0,
    ) -> str:
        """本地昂贵阶段聚合 usage：四元组唯一；本地 V1 无价格金额。

        started_at_utc 必填（调用者稳定提供 actual started）；指纹含它，recorded 用
        DB now（moving clock 重放仍复用 persisted ID）。
        """
        execution_kind = _require_text(execution_kind, "execution_kind", 32)
        execution_id = _require_text(execution_id, "execution_id", 128)
        stage = _require_text(stage, "stage", 64)
        resource_kind = _require_text(resource_kind, "resource_kind", 32)
        result = _require_text(result, "result", 32)
        replay_generation = _require_replay_generation(replay_generation)
        self._validate_ownership(ownership)
        self._validate_local_measurement(measurement)
        started = _as_utc(started_at_utc, "started_at_utc")
        try:
            with self._engine.begin() as connection:
                now = self.clock.now_utc(connection)
                lock = self.calendar.lock_or_verify(connection)
                lock_period = self.calendar.period_for(lock, started)
                recorded_period = self.calendar.period_for(lock, now)
                measurement_payload = asdict(measurement)
                measurement_sources = measurement_payload.pop("measurement_sources", {})
                fingerprint_payload = {
                    "scope": (execution_kind, execution_id, stage, resource_kind),
                    "ownership": _ownership_json(ownership),
                    "started_at_utc": started,
                    "measurement": measurement_payload,
                    "result": result,
                    "effective_period": lock_period,
                }
                if measurement_sources:
                    fingerprint_payload["measurement_sources"] = measurement_sources
                if replay_generation:
                    fingerprint_payload["replay_generation"] = replay_generation
                fingerprint = self._event_fingerprint("local_usage", fingerprint_payload)
                event_id = f"ue_{secrets.token_urlsafe(9)}"
                persisted_id = self._insert_usage_once(
                    connection,
                    index_elements=["execution_kind", "execution_id", "stage", "resource_kind"],
                    values={
                        "usage_event_id": event_id,
                        "event_kind": "local_usage",
                        "execution_kind": execution_kind,
                        "execution_id": execution_id,
                        "stage": stage,
                        "resource_kind": resource_kind,
                        "replay_generation": replay_generation,
                        "cost_center_key": ownership.cost_center_key,
                        "item_count": measurement.item_count,
                        "page_count": measurement.page_count,
                        "input_bytes": measurement.input_bytes,
                        "gpu_milliseconds": measurement.gpu_milliseconds,
                        "cpu_milliseconds": measurement.cpu_milliseconds,
                        "peak_vram_bytes": measurement.peak_vram_bytes,
                        "result": result,
                        "event_fingerprint": fingerprint,
                        "ownership_json": _ownership_json(ownership),
                        "started_at_utc": started,
                        "completed_at_utc": now,
                        "effective_calendar_version_id": lock.version_id,
                        "effective_at_utc": started,
                        "effective_period": lock_period,
                        "recorded_calendar_version_id": lock.version_id,
                        "recorded_at_utc": now,
                        "recorded_period": recorded_period,
                        "created_at_utc": now,
                    },
                )
                return persisted_id
        except PlatformError as exc:
            self._alert_usage_invariant_conflict(exc)
            raise

    def append_usage_adjustment(
        self,
        *,
        referenced_event_id: str,
        adjustment_source_namespace: str,
        adjustment_source_id: str,
        adjustment_allocation_key: str,
        deltas: dict[str, int],
        ownership: OwnershipSnapshot,
        result: str = "adjusted",
    ) -> str:
        """追加 usage 计量差异（有符号 correction）：引用原始 provider/local 事件。

        唯一键四元组；同指纹重放复用 persisted ID，异指纹 409。delta 为非零 signed
        BigInteger（[-2**63, 2**63-1]，负值表达计量冲减），键必须在被引用事件类别的
        固定 meter 集合内。输入字段基本校验在事务前完成。
        """
        namespace = _require_text(adjustment_source_namespace, "adjustment_source_namespace", 64)
        source_id = _require_text(adjustment_source_id, "adjustment_source_id", 128)
        allocation = _require_text(adjustment_allocation_key, "adjustment_allocation_key", 64)
        result = _require_text(result, "result", 32)
        if not isinstance(deltas, dict) or not deltas:
            raise PlatformError("validation_error", "deltas must be a non-empty object", {}, 422)
        for field, value in deltas.items():
            if field not in _ADJUSTMENT_DELTA_FIELDS:
                raise PlatformError(
                    "validation_error", f"Unknown delta meter field {field!r}", {}, 422
                )
            if isinstance(value, bool) or not isinstance(value, int) or value == 0:
                raise PlatformError(
                    "validation_error",
                    f"delta {field!r} must be a non-zero integer",
                    {},
                    422,
                )
            # signed correction：允许负 delta（计量差异追加式表达），完整 64 位有符号范围。
            if value < _BIGINT_MIN or value > _BIGINT_MAX:
                raise PlatformError(
                    "validation_error",
                    f"delta {field!r} must fit the BigInteger range",
                    {},
                    422,
                )
        self._validate_ownership(ownership)
        return self._append_adjustment(
            event_kind="usage_adjustment",
            referenced_event_id=referenced_event_id,
            adjustment_source_namespace=namespace,
            adjustment_source_id=source_id,
            adjustment_allocation_key=allocation,
            ownership=ownership,
            result=result,
            extra_values=dict(deltas),
        )

    def append_cost_adjustment(
        self,
        *,
        referenced_event_id: str,
        adjustment_source_namespace: str,
        adjustment_source_id: str,
        adjustment_allocation_key: str,
        amount_delta: Decimal,
        currency_code: str,
        ownership: OwnershipSnapshot,
        result: str = "cost_adjusted",
    ) -> str:
        """追加成本差异（有符号 correction）：只引用原始 provider_usage。

        amount_delta 可为负（退款/冲减）；currency 必须与被引用 provider usage 一致
        （无跨币种模型）。唯一键四元组；同指纹重放复用 persisted ID，异指纹 409。
        """
        namespace = _require_text(adjustment_source_namespace, "adjustment_source_namespace", 64)
        source_id = _require_text(adjustment_source_id, "adjustment_source_id", 128)
        allocation = _require_text(adjustment_allocation_key, "adjustment_allocation_key", 64)
        result = _require_text(result, "result", 32)
        # amount_delta 是**有符号 correction**：可为负表示退款/冲减（正式 spec：完整
        # 计量或账单差异只能以引用原事实的 cost_adjustment 追加；接口命名 amount_delta
        # 即有符号）。amount-only 对账行（record_reconciliation_amount_only）的非负约束
        # 不适用于 adjustment——那是“原始金额事实”，本方法是“差异修正”。
        amount = _validate_money_amount(amount_delta, "amount_delta")
        currency = normalize_currency_code(currency_code)
        self._validate_ownership(ownership)
        return self._append_adjustment(
            event_kind="cost_adjustment",
            referenced_event_id=referenced_event_id,
            adjustment_source_namespace=namespace,
            adjustment_source_id=source_id,
            adjustment_allocation_key=allocation,
            ownership=ownership,
            result=result,
            extra_values={
                "estimated_cost_amount": amount,
                "currency_code": currency,
                "estimated_cost_status": "complete",
            },
        )

    def list_stale_dispatching(self, *, older_than_utc: datetime, limit: int = 100) -> list[dict]:
        """列出同时满足 stale 与 persisted deadline 已过的 dispatching call。"""
        with self._engine.connect() as connection:
            current = _utc(self.clock.now_utc(connection))
            rows = (
                connection.execute(
                    select(provider_call_table)
                    .where(
                        and_(
                            provider_call_table.c.status == "dispatching",
                            provider_call_table.c.dispatching_at_utc <= _utc(older_than_utc),
                            provider_call_table.c.deadline_utc <= current,
                        )
                    )
                    .order_by(provider_call_table.c.dispatching_at_utc)
                    .limit(limit)
                )
                .mappings()
                .all()
            )
            return [dict(row) for row in rows]
