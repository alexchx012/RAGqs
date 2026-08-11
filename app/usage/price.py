"""Versioned price catalog service (Task 4, formal spec revision).

价格 scope = (provider, model, operation)。一个版本持有半开区间
[effective_from_utc, effective_to_utc) 与单一 ISO 4217 币种（完整 ISO 4217 代码集，
注册时统一 uppercase）；同一 scope 同时仅一个 open 版本（effective_to_utc IS
NULL），由 partial unique index `uq_price_open_interval` 保证并仲裁首版并发注册
（数据库冲突稳定映射 `price_scope_conflict`）。

区间不重叠规则：scope 首次注册仅当该 scope 无任何历史版本；存在历史版本时必须
显式 `supersedes_version_id` 指向当前/latest 版本——若该版本 open，则在同一短事务内
锁定该行、校验新 effective_from 严格更晚、拒绝 retroactive close（旧版本既有
usage_event 的 started_at/effective_at 位于拟 close 时点或之后 → 矛盾）、一次性
close 到新 effective_from，再插入 successor；若该版本已 closed，则新 effective_from
不得早于其 effective_to（相邻但不重叠）。`select_for` 只按 [effective_from,
effective_to) 半开区间选 at 时最新版本，不做 supersedes 链过滤——未来 successor
不得让历史 select_for(at) 失效。

close/supersede 与发送竞态：ledger 在 `mark_dispatching` 的短事务中按 actual send
时点 select + `lock_for_usage`，以 price→provider_call 固定锁序锁价后才把 prepared
流转为 dispatching（事务仍在网络 I/O 前提交）。close/supersede 持有 scope/price 锁
后拒绝同 scope、started_at >= close 的 dispatching/unknown；close-first 时 dispatch
醒来重验后选择 successor，若无覆盖则发送前失败。completion/reconcile 仍按持久化
started 选价。close_version 另外保持“被任何 usage_event 引用即拒绝”的旧契约。

register 使用嵌套 savepoint（begin_nested）包含 predecessor close、header、全部
lines；任何 IntegrityError 回滚整个操作并稳定映射 price_scope_conflict（PG 连接
仍可用）；duplicate meter / 输入校验在 SQL 之前完成（422 validation_error）。
返回从 DB 回读的版本对象（lines 按 meter 稳定排序），rate 注册时严格
Decimal 有限、>=0、scale<=10、precision<=30、量化 10 位，防即时/回读金额差异。

scope 串行化：每次 register/close 先 upsert `price_catalog_scope` 行，再原子
UPDATE lock_version=lock_version+1 获取数据库写锁（PG 行锁 / SQLite 写锁，持有至
事务结束），然后在锁内重新读取 latest/history 并完成 savepoint 操作——首次注册、
successor、close 均按 scope 串行，消除 phantom / 旧 predecessor 窗口。

estimate_cost 按正式公式计价（全部 Decimal）：quantity=0 → 行金额 0；
quantity>0 → billable = max(quantity, minimum_billable_quantity)（原始单位），
blocks = ceil(billable / billing_granularity)（固定向上取整，rounding_rule 不影响
block 数），line_total = blocks * rate 按 rounding_rule 量化 6 位；总金额 half_up
量化 6 位。availability 必须合法且每个 priced meter 显式提供；complete/partial
meter 必须有非负有限 quantity（missing 不当 0，超过 _MAX_QUANTITY 稳定 422）；
unavailable 忽略（可含非零 placeholder）；额外 unpriced meter 忽略。全部未知 →
(None, "unavailable")；部分未知 → 已知金额和 + "partial"；全部已知 → "complete"。
所有运算（含最终 quantize）在动态精度的 localcontext 内，不落回默认 28 位。
严格合法 measurement_sources 由 Task 5 负责。
"""

from __future__ import annotations

import json
import secrets
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_CEILING, ROUND_DOWN, ROUND_HALF_UP, Decimal, localcontext
from typing import Literal

from sqlalchemy import Engine, or_, select, update
from sqlalchemy.engine import Connection
from sqlalchemy.exc import IntegrityError

from app.platform.errors import PlatformError
from app.platform.persistence import DatabaseClock

from ._sql import _insert_do_nothing
from .schema import (
    price_catalog_line_table,
    price_catalog_scope_table,
    price_catalog_table,
    provider_call_table,
    usage_event_table,
)

_ROUNDINGS = {"floor": ROUND_DOWN, "ceil": ROUND_CEILING, "half_up": ROUND_HALF_UP}
_MONEY_QUANTUM = Decimal("0.000001")
_RATE_QUANTUM = Decimal("0.0000000001")
_RATE_MAX_DIGITS = 30  # Numeric(30,10)：量化 10 位后最多 30 位有效数字
_INT32_MAX = 2**31 - 1
_AVAILABILITY = frozenset({"complete", "partial", "unavailable"})
# 支持数量上限（文档化）：真实 meter 读数远低于 1e18；超过则稳定 422，避免
# 计价上下文精度无限膨胀。配合 estimate 内动态精度，任何通过校验的有限输入
# 都不会在 Decimal 运算/quantize 时抛 InvalidOperation。
_MAX_QUANTITY = Decimal("1000000000000000000")  # 1e18

CostStatus = Literal["complete", "partial", "unavailable"]

# ISO 4217 现行货币代码全集（含基金/贵金属等非流通代码，如 XAU/XDR/XXX；
# 2026-01-01 现行清单核对：新增 XCG（Caribbean guilder，替代 ANG）、XAD；
# 已移除撤销代码 ANG（2025-03-31）、BGN（2025-12-31）、SLL（2022-04 被 SLE
# 替代，SLE 现行保留；SLL 为历史代码）。
# 正式 spec 要求合法 ISO 4217 code；注册时统一 uppercase。维护方式：随 ISO 4217
# 更新在此追加/删除并补测试（新增货币如 2024-04 的 ZWG、2025-03 的 XCG 已包含）。
_ISO4217_CURRENCIES = frozenset(
    {
        "AED",
        "AFN",
        "ALL",
        "AMD",
        "AOA",
        "ARS",
        "AUD",
        "AWG",
        "AZN",
        "BAM",
        "BBD",
        "BDT",
        "BHD",
        "BIF",
        "BMD",
        "BND",
        "BOB",
        "BOV",
        "BRL",
        "BSD",
        "BTN",
        "BWP",
        "BYN",
        "BZD",
        "CAD",
        "CDF",
        "CHE",
        "CHF",
        "CHW",
        "CLF",
        "CLP",
        "CNY",
        "COP",
        "COU",
        "CRC",
        "CUP",
        "CVE",
        "CZK",
        "DJF",
        "DKK",
        "DOP",
        "DZD",
        "EGP",
        "ERN",
        "ETB",
        "EUR",
        "FJD",
        "FKP",
        "GBP",
        "GEL",
        "GHS",
        "GIP",
        "GMD",
        "GNF",
        "GTQ",
        "GYD",
        "HKD",
        "HNL",
        "HTG",
        "HUF",
        "IDR",
        "ILS",
        "INR",
        "IQD",
        "IRR",
        "ISK",
        "JMD",
        "JOD",
        "JPY",
        "KES",
        "KGS",
        "KHR",
        "KMF",
        "KPW",
        "KRW",
        "KWD",
        "KYD",
        "KZT",
        "LAK",
        "LBP",
        "LKR",
        "LRD",
        "LSL",
        "LYD",
        "MAD",
        "MDL",
        "MGA",
        "MKD",
        "MMK",
        "MNT",
        "MOP",
        "MRU",
        "MUR",
        "MVR",
        "MWK",
        "MXN",
        "MXV",
        "MYR",
        "MZN",
        "NAD",
        "NGN",
        "NIO",
        "NOK",
        "NPR",
        "NZD",
        "OMR",
        "PAB",
        "PEN",
        "PGK",
        "PHP",
        "PKR",
        "PLN",
        "PYG",
        "QAR",
        "RON",
        "RSD",
        "RUB",
        "RWF",
        "SAR",
        "SBD",
        "SCR",
        "SDG",
        "SEK",
        "SGD",
        "SHP",
        "SLE",
        "SOS",
        "SRD",
        "SSP",
        "STN",
        "SVC",
        "SYP",
        "SZL",
        "THB",
        "TJS",
        "TMT",
        "TND",
        "TOP",
        "TRY",
        "TTD",
        "TWD",
        "TZS",
        "UAH",
        "UGX",
        "USD",
        "USN",
        "UYI",
        "UYU",
        "UYW",
        "UZS",
        "VED",
        "VES",
        "VND",
        "VUV",
        "WST",
        "XAD",
        "XAF",
        "XAG",
        "XAU",
        "XBA",
        "XBB",
        "XBC",
        "XBD",
        "XCD",
        "XCG",
        "XDR",
        "XOF",
        "XPD",
        "XPF",
        "XPT",
        "XSU",
        "XTS",
        "XUA",
        "XXX",
        "YER",
        "ZAR",
        "ZMW",
        "ZWG",
    }
)


@dataclass(frozen=True, slots=True)
class PriceLine:
    meter: str
    unit: str
    rate: Decimal
    billing_granularity: int
    minimum_billable_quantity: int
    rounding_rule: str


@dataclass(frozen=True, slots=True)
class PriceVersion:
    id: str
    provider: str
    model: str
    operation: str
    currency_code: str
    effective_from_utc: datetime
    effective_to_utc: datetime | None
    supersedes_version_id: str | None
    lines: tuple[PriceLine, ...]


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


def _normalize_currency(value) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PlatformError("validation_error", "currency_code is required", {}, 422)
    currency = value.strip().upper()
    if currency not in _ISO4217_CURRENCIES:
        raise PlatformError(
            "validation_error",
            f"currency_code must be a valid ISO 4217 currency code (got {currency!r})",
            {},
            422,
        )
    return currency


def normalize_currency_code(value) -> str:
    """公开货币校验：复用注册时同一 ISO 4217 验证器（ledger/reconcile 共用）。"""
    return _normalize_currency(value)


def _coerce_rate(value) -> Decimal:
    if value is None:
        raise PlatformError("validation_error", "rate is required", {}, 422)
    try:
        rate = Decimal(str(value))
    except Exception as exc:
        raise PlatformError(
            "validation_error", "rate must be a valid decimal number", {}, 422
        ) from exc
    if not rate.is_finite():
        raise PlatformError("validation_error", "rate must be finite", {}, 422)
    if rate < 0:
        raise PlatformError("validation_error", "rate must be >= 0", {}, 422)
    _sign, digits, exponent = rate.as_tuple()
    if not isinstance(digits, tuple) or not isinstance(exponent, int):
        raise PlatformError("validation_error", "rate must be finite", {}, 422)
    if exponent < -10:
        raise PlatformError(
            "validation_error", "rate scale must not exceed 10 decimal places", {}, 422
        )
    # Numeric(30,10)：量化 10 位后总位数 = len(digits) + exponent + 10 <= 30
    if len(digits) + exponent > 20:
        raise PlatformError("validation_error", "rate must fit Numeric(30,10) precision", {}, 422)
    with localcontext() as ctx:
        ctx.prec = 40
        return rate.quantize(_RATE_QUANTUM)


def _coerce_int(value, name: str, *, minimum: int) -> int:
    if isinstance(value, bool) or value is None:
        raise PlatformError("validation_error", f"{name} must be an integer", {}, 422)
    if isinstance(value, int):
        result = value
    elif isinstance(value, float):
        if not value.is_integer():
            raise PlatformError("validation_error", f"{name} must be an integer", {}, 422)
        result = int(value)
    elif isinstance(value, Decimal):
        if not value.is_finite() or value != value.to_integral_value():
            raise PlatformError("validation_error", f"{name} must be an integer", {}, 422)
        result = int(value)
    else:
        raise PlatformError("validation_error", f"{name} must be an integer", {}, 422)
    if result < minimum:
        raise PlatformError("validation_error", f"{name} must be >= {minimum}", {}, 422)
    if result > _INT32_MAX:
        raise PlatformError("validation_error", f"{name} must be at most {_INT32_MAX}", {}, 422)
    return result


def _measured_quantity(measured: Mapping[str, Decimal], meter: str) -> Decimal:
    value = measured.get(meter)
    if value is None:
        raise PlatformError(
            "validation_error",
            f"measured quantity is missing for meter {meter!r}",
            {},
            422,
        )
    try:
        quantity = Decimal(str(value))
    except Exception as exc:
        raise PlatformError(
            "validation_error",
            f"measured quantity for meter {meter!r} must be a decimal",
            {},
            422,
        ) from exc
    if not quantity.is_finite() or quantity < 0:
        raise PlatformError(
            "validation_error",
            f"measured quantity for meter {meter!r} must be non-negative and finite",
            {},
            422,
        )
    return quantity


def _scope_conflict(message: str) -> PlatformError:
    return PlatformError("price_scope_conflict", message, {}, 409)


def _close_conflict(message: str) -> PlatformError:
    return PlatformError("price_close_conflict", message, {}, 409)


class PriceCatalogService:
    def __init__(self, engine: Engine, clock: DatabaseClock) -> None:
        self._engine = engine
        self._clock = clock

    # -- 注册 ---------------------------------------------------------------

    def register(
        self,
        connection: Connection,
        *,
        provider: str,
        model: str,
        operation: str,
        currency_code: str,
        lines: list[dict],
        effective_from_utc: datetime,
        supersedes_version_id: str | None = None,
    ) -> PriceVersion:
        """注册价格版本；全部输入校验在 SQL 之前完成（422）。

        supersedes_version_id 为 None 时仅允许 scope 尚无任何历史版本；否则必须指向
        当前/latest 版本。整个操作（scope lock + predecessor close + header + lines）
        包裹在嵌套 savepoint 中，任何 IntegrityError 整体回滚并稳定映射
        price_scope_conflict。
        """
        provider = _require_text(provider, "provider", 64)
        model = _require_text(model, "model", 128)
        operation = _require_text(operation, "operation", 64)
        currency = _normalize_currency(currency_code)
        effective_from = _as_utc(effective_from_utc, "effective_from_utc")
        parsed = self._parse_lines(lines)
        if supersedes_version_id is not None:
            supersedes_version_id = _require_text(
                supersedes_version_id, "supersedes_version_id", 64
            )
        now = self._clock.now_utc(connection)
        version_id = f"price_{secrets.token_urlsafe(9)}"
        try:
            # scope 写锁必须在 savepoint 之外：UPDATE lock_version+1 获取数据库写锁
            # （PG 行锁 / SQLite 写锁），持有至事务结束；savepoint 内的写操作不会
            # 让锁失效（SQLite 实证：锁在 savepoint 内获取则不持锁，在外部获取则持有）。
            self._scope_lock(connection, provider, model, operation, now)
            with connection.begin_nested():
                if supersedes_version_id is None:
                    self._ensure_no_history(connection, provider, model, operation)
                else:
                    self._close_predecessor(
                        connection,
                        provider,
                        model,
                        operation,
                        effective_from,
                        supersedes_version_id,
                    )
                connection.execute(
                    price_catalog_table.insert().values(
                        id=version_id,
                        provider=provider,
                        model=model,
                        operation=operation,
                        currency_code=currency,
                        effective_from_utc=effective_from,
                        effective_to_utc=None,
                        supersedes_version_id=supersedes_version_id,
                        created_at_utc=now,
                    )
                )
                for line in parsed:
                    connection.execute(
                        price_catalog_line_table.insert().values(
                            id=f"pl_{secrets.token_urlsafe(9)}",
                            price_version_id=version_id,
                            meter=line.meter,
                            unit=line.unit,
                            rate=line.rate,
                            billing_granularity=line.billing_granularity,
                            minimum_billable_quantity=line.minimum_billable_quantity,
                            rounding_rule=line.rounding_rule,
                        )
                    )
        except IntegrityError as exc:
            # savepoint 已回滚：无部分 header/line/old-close，外层事务/PG 连接仍可用。
            # 输入侧（duplicate meter、rate 越界等）已在 SQL 前校验为 422，此处残余
            # IntegrityError 均为 scope 级唯一性冲突。
            raise _scope_conflict("A price version conflict occurred for this scope") from exc
        # 返回从 DB 回读的对象（lines 按 meter 稳定排序），防即时/回读金额差异
        return self._read_version(connection, version_id)

    @staticmethod
    def _scope_lock(
        connection: Connection, provider: str, model: str, operation: str, now: datetime
    ) -> None:
        """按 scope 获取数据库写锁：预留基础行 + 原子 lock_version+1。

        scope id 为 `scope_<UUID5>`：对 canonical JSON tuple [provider, model,
        operation]（separators 无空白）取 UUID5（固定长度 42 < 64，无分隔歧义——
        绝不做字符串直接拼接，`(a_b,c,d)` 与 `(a,b_c,d)` 得到不同 id）。

        scope row INSERT 放入独立 nested savepoint 并稳定映射（真实 PG IntegrityError
        整体回滚该 savepoint，不毒化 outer transaction）；随后 scope lock UPDATE 在
        savepoint 之外执行，持有数据库写锁（PG 行锁 / SQLite 写锁）至事务结束。
        所有 register/close 对同一 scope 在此串行——锁内重新读取 latest/history
        不会看到并发写入，消除 phantom / 旧 predecessor 窗口。
        """
        scope_id = "scope_" + str(
            uuid.uuid5(
                uuid.NAMESPACE_DNS,
                json.dumps([provider, model, operation], separators=(",", ":")),
            )
        )
        try:
            with connection.begin_nested():
                _insert_do_nothing(
                    connection,
                    price_catalog_scope_table,
                    {
                        "id": scope_id,
                        "provider": provider,
                        "model": model,
                        "operation": operation,
                        "lock_version": 0,
                        "created_at_utc": now,
                    },
                    ["provider", "model", "operation"],
                )
        except IntegrityError as exc:
            # 独立 savepoint 已回滚：scope 行无残留，outer transaction/PG 连接仍可用
            raise _scope_conflict("Price scope row could not be reserved") from exc
        connection.execute(
            update(price_catalog_scope_table)
            .where(
                price_catalog_scope_table.c.provider == provider,
                price_catalog_scope_table.c.model == model,
                price_catalog_scope_table.c.operation == operation,
            )
            .values(lock_version=price_catalog_scope_table.c.lock_version + 1)
        )

    @staticmethod
    def _parse_lines(lines) -> list[PriceLine]:
        if not isinstance(lines, list) or not lines:
            raise PlatformError(
                "validation_error", "Price version needs at least one line", {}, 422
            )
        parsed: list[PriceLine] = []
        meters: list[str] = []
        for line in lines:
            if not isinstance(line, dict):
                raise PlatformError(
                    "validation_error", "Each price line must be an object", {}, 422
                )
            meter = _require_text(line.get("meter"), "meter", 32)
            unit = _require_text(line.get("unit"), "unit", 32)
            rate = _coerce_rate(line.get("rate"))
            granularity = _coerce_int(
                line.get("billing_granularity", 1), "billing_granularity", minimum=1
            )
            minimum_billable = _coerce_int(
                line.get("minimum_billable_quantity", 1),
                "minimum_billable_quantity",
                minimum=0,
            )
            rule = line.get("rounding_rule", "floor")
            if not isinstance(rule, str) or rule not in _ROUNDINGS:
                raise PlatformError(
                    "validation_error", "rounding_rule must be floor, ceil or half_up", {}, 422
                )
            meters.append(meter)
            parsed.append(
                PriceLine(
                    meter=meter,
                    unit=unit,
                    rate=rate,
                    billing_granularity=granularity,
                    minimum_billable_quantity=minimum_billable,
                    rounding_rule=rule,
                )
            )
        if len(set(meters)) != len(meters):
            raise PlatformError(
                "validation_error",
                "Each meter may appear at most once per price version",
                {},
                422,
            )
        return parsed

    @staticmethod
    def _ensure_no_history(
        connection: Connection, provider: str, model: str, operation: str
    ) -> None:
        existing = connection.execute(
            select(price_catalog_table.c.id)
            .where(
                price_catalog_table.c.provider == provider,
                price_catalog_table.c.model == model,
                price_catalog_table.c.operation == operation,
            )
            .limit(1)
        ).first()
        if existing is not None:
            raise _scope_conflict(
                "This scope already has price versions; "
                "register with supersedes_version_id of the latest version"
            )

    def _close_predecessor(
        self,
        connection: Connection,
        provider: str,
        model: str,
        operation: str,
        effective_from: datetime,
        supersedes_version_id: str,
    ) -> None:
        target = (
            connection.execute(
                select(
                    price_catalog_table.c.id,
                    price_catalog_table.c.provider,
                    price_catalog_table.c.model,
                    price_catalog_table.c.operation,
                ).where(price_catalog_table.c.id == supersedes_version_id)
            )
            .mappings()
            .one_or_none()
        )
        if target is None:
            raise PlatformError("price_not_found", "Price version was not found", {}, 404)
        if (target["provider"], target["model"], target["operation"]) != (
            provider,
            model,
            operation,
        ):
            raise PlatformError(
                "validation_error",
                "supersedes_version_id must belong to the same price scope",
                {},
                422,
            )
        # 原子 latest：同一语句内按 scope 查询 ORDER BY effective_from DESC LIMIT 1
        # 并 FOR UPDATE 锁定该行（open/closed 都可能是 latest）。锁定后重新确认其 ID
        # 即传入的 supersedes/latest——锁定期间任何竞争者都无法越过该行（PG 行锁），
        # 若读取时该行已不是 latest，则说明传入的是旧 predecessor，稳定 409。
        # 不做「先无锁查 latest 再锁旧 predecessor」的两步式。
        latest = (
            connection.execute(
                select(
                    price_catalog_table.c.id,
                    price_catalog_table.c.effective_from_utc,
                    price_catalog_table.c.effective_to_utc,
                )
                .where(
                    price_catalog_table.c.provider == provider,
                    price_catalog_table.c.model == model,
                    price_catalog_table.c.operation == operation,
                )
                .order_by(price_catalog_table.c.effective_from_utc.desc())
                .limit(1)
                .with_for_update()
            )
            .mappings()
            .one_or_none()
        )
        if latest is None or latest["id"] != supersedes_version_id:
            raise _scope_conflict("Must supersede the latest version of this scope")
        if latest["effective_to_utc"] is None:
            if effective_from <= _utc(latest["effective_from_utc"]):
                raise _scope_conflict(
                    "A successor effective_from must be later than the version it supersedes"
                )
        else:
            if effective_from < _utc(latest["effective_to_utc"]):
                raise _scope_conflict(
                    "A successor effective_from must not be earlier than "
                    "the superseded version's effective_to"
                )
        self._reject_retroactive_close(connection, supersedes_version_id, effective_from)
        if latest["effective_to_utc"] is None:
            self._reject_pending_dispatches(
                connection,
                provider=provider,
                model=model,
                operation=operation,
                close_at_utc=effective_from,
            )
            connection.execute(
                update(price_catalog_table)
                .where(price_catalog_table.c.id == supersedes_version_id)
                .values(effective_to_utc=effective_from)
            )

    @staticmethod
    def _reject_pending_dispatches(
        connection: Connection,
        *,
        provider: str,
        model: str,
        operation: str,
        close_at_utc: datetime,
    ) -> None:
        """拒绝覆盖同 scope 尚未终态、且 started 落在拟关闭时点或之后的发送。"""
        pending = connection.execute(
            select(provider_call_table.c.provider_call_id)
            .where(
                provider_call_table.c.provider == provider,
                provider_call_table.c.model == model,
                provider_call_table.c.operation == operation,
                provider_call_table.c.status.in_(("dispatching", "unknown")),
                provider_call_table.c.started_at_utc >= close_at_utc,
            )
            .limit(1)
        ).first()
        if pending is not None:
            raise _close_conflict(
                "Price interval has dispatching or unknown provider calls at or after "
                "the close time"
            )

    @staticmethod
    def _reject_any_usage(connection: Connection, price_version_id: str) -> None:
        """standalone close 契约：该版本被任何 usage_event 引用即拒绝。"""
        used = connection.execute(
            select(usage_event_table.c.usage_event_id)
            .where(usage_event_table.c.price_version_id == price_version_id)
            .limit(1)
        ).first()
        if used is not None:
            raise _close_conflict(
                "Price version is referenced by usage events; "
                "append a superseding version instead"
            )

    @staticmethod
    def _reject_retroactive_close(
        connection: Connection, price_version_id: str, close_at_utc: datetime
    ) -> None:
        """拒绝把版本 close 到既有 usage_event 事实所在的时点或之后。

        事件按 started_at_utc / effective_at_utc 任一 >= 拟 close 时点即冲突：该事件
        位于 [effective_from, close_at) 之外，close 后选价将与其绑定的 price_version_id
        矛盾（恰 close_at 不属于旧版，half-open）。
        """
        used = connection.execute(
            select(usage_event_table.c.usage_event_id)
            .where(
                usage_event_table.c.price_version_id == price_version_id,
                or_(
                    usage_event_table.c.started_at_utc >= close_at_utc,
                    usage_event_table.c.effective_at_utc >= close_at_utc,
                ),
            )
            .limit(1)
        ).first()
        if used is not None:
            raise _close_conflict(
                "Price version has usage events at or after the close time; "
                "choose a later effective_from instead"
            )

    # -- 关闭 ---------------------------------------------------------------

    def close_version(
        self, connection: Connection, version_id: str, effective_to_utc: datetime
    ) -> None:
        """standalone close：先按 scope 获取写锁，再锁该行，按旧契约关闭。

        该版本被任何 usage_event 引用 → price_close_conflict/409（提示用 superseding
        版本）；已关闭版本不可再改区间；close 时点必须严格晚于 effective_from。
        """
        effective_to = _as_utc(effective_to_utc, "effective_to_utc")
        row = (
            connection.execute(
                select(
                    price_catalog_table.c.id,
                    price_catalog_table.c.provider,
                    price_catalog_table.c.model,
                    price_catalog_table.c.operation,
                    price_catalog_table.c.effective_from_utc,
                    price_catalog_table.c.effective_to_utc,
                ).where(price_catalog_table.c.id == version_id)
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise PlatformError("price_not_found", "Price version was not found", {}, 404)
        now = self._clock.now_utc(connection)
        self._scope_lock(
            connection,
            str(row["provider"]),
            str(row["model"]),
            str(row["operation"]),
            now,
        )
        locked = (
            connection.execute(
                select(
                    price_catalog_table.c.id,
                    price_catalog_table.c.effective_from_utc,
                    price_catalog_table.c.effective_to_utc,
                )
                .where(price_catalog_table.c.id == version_id)
                .with_for_update()
            )
            .mappings()
            .one_or_none()
        )
        if locked is None:  # pragma: no cover - 上方已确认存在
            raise PlatformError("price_not_found", "Price version was not found", {}, 404)
        if locked["effective_to_utc"] is not None:
            raise _close_conflict(
                "Price version is already closed; its interval cannot be modified"
            )
        if effective_to <= _utc(locked["effective_from_utc"]):
            raise _close_conflict("Close time must be after the version effective_from")
        self._reject_any_usage(connection, version_id)
        self._reject_pending_dispatches(
            connection,
            provider=str(row["provider"]),
            model=str(row["model"]),
            operation=str(row["operation"]),
            close_at_utc=effective_to,
        )
        connection.execute(
            update(price_catalog_table)
            .where(price_catalog_table.c.id == version_id)
            .values(effective_to_utc=effective_to)
        )

    # -- usage 写入锁 -------------------------------------------------------

    def lock_for_usage(
        self, connection: Connection, price_version_id: str, actual_send_at: datetime
    ) -> PriceVersion:
        """ledger 写 usage_event 前调用：锁定 price 行并验证时间落在版本区间。

        同一事务内对 price_catalog 行 FOR UPDATE，与 close_version/supersede 对
        同一行的锁定互斥：close 先赢 → 本调用看到已关闭区间 → price_interval_conflict，
        ledger 需重新选价；本调用先赢 → close 等待后看到 usage 引用 → 拒绝。
        返回锁定后的版本（Task 5 直接使用）。禁止持 DB 事务跨网络。
        """
        at = _as_utc(actual_send_at, "actual_send_at")
        row = (
            connection.execute(
                select(price_catalog_table)
                .where(price_catalog_table.c.id == price_version_id)
                .with_for_update()
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise PlatformError("price_not_found", "Price version was not found", {}, 404)
        effective_from = _utc(row["effective_from_utc"])
        effective_to = (
            _utc(row["effective_to_utc"]) if row["effective_to_utc"] is not None else None
        )
        if at < effective_from or (effective_to is not None and at >= effective_to):
            raise PlatformError(
                "price_interval_conflict",
                "Usage time falls outside the version effective interval",
                {
                    "price_version_id": price_version_id,
                    "effective_from_utc": str(effective_from),
                    "effective_to_utc": str(effective_to) if effective_to is not None else None,
                },
                409,
            )
        return self._build_version(connection, row)

    # -- 查询 ---------------------------------------------------------------

    def select_for(
        self,
        connection: Connection,
        *,
        provider: str,
        model: str,
        operation: str,
        at_utc: datetime,
    ) -> PriceVersion:
        """返回 at_utc 时点生效的最新版本（半开区间 [effective_from, effective_to)）。

        不做 supersedes 链过滤：未来 successor 不得让历史 select_for(at) 失效。
        """
        at = _as_utc(at_utc, "at_utc")
        row = (
            connection.execute(
                select(price_catalog_table)
                .where(
                    price_catalog_table.c.provider == provider,
                    price_catalog_table.c.model == model,
                    price_catalog_table.c.operation == operation,
                    price_catalog_table.c.effective_from_utc <= at,
                    or_(
                        price_catalog_table.c.effective_to_utc.is_(None),
                        price_catalog_table.c.effective_to_utc > at,
                    ),
                )
                .order_by(price_catalog_table.c.effective_from_utc.desc())
                .limit(1)
            )
            .mappings()
            .first()
        )
        if row is None:
            raise PlatformError(
                "price_not_found", "No price version covers the scope at the given time", {}, 404
            )
        return self._build_version(connection, row)

    def _read_version(self, connection: Connection, version_id: str) -> PriceVersion:
        row = (
            connection.execute(
                select(price_catalog_table).where(price_catalog_table.c.id == version_id)
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise PlatformError("price_not_found", "Price version was not found", {}, 404)
        return self._build_version(connection, row)

    @staticmethod
    def _build_version(connection: Connection, row) -> PriceVersion:
        lines = (
            connection.execute(
                select(price_catalog_line_table)
                .where(price_catalog_line_table.c.price_version_id == row["id"])
                .order_by(price_catalog_line_table.c.meter)
            )
            .mappings()
            .all()
        )
        effective_to = row["effective_to_utc"]
        return PriceVersion(
            id=str(row["id"]),
            provider=str(row["provider"]),
            model=str(row["model"]),
            operation=str(row["operation"]),
            currency_code=str(row["currency_code"]),
            effective_from_utc=_utc(row["effective_from_utc"]),
            effective_to_utc=_utc(effective_to) if effective_to is not None else None,
            supersedes_version_id=row["supersedes_version_id"],
            lines=tuple(
                PriceLine(
                    meter=str(line["meter"]),
                    unit=str(line["unit"]),
                    rate=Decimal(str(line["rate"])),
                    billing_granularity=int(line["billing_granularity"]),
                    minimum_billable_quantity=int(line["minimum_billable_quantity"]),
                    rounding_rule=str(line["rounding_rule"]),
                )
                for line in lines
            ),
        )

    # -- 计价 ---------------------------------------------------------------

    @staticmethod
    def estimate_cost(
        price: PriceVersion,
        measured: Mapping[str, Decimal],
        *,
        availability: Mapping[str, CostStatus],
    ) -> tuple[Decimal | None, CostStatus]:
        """逐 meter 计价（正式公式）。

        quantity=0 → 行金额 0；quantity>0 → billable = max(quantity, minimum)（原始
        单位）→ blocks = ceil(billable / billing_granularity)（固定向上取整，
        rounding_rule 不影响 block 数）→ line_total = blocks * rate 按
        rounding_rule 量化 6 位；总金额 half_up 量化 6 位。
        availability 必须合法且每个 priced meter 显式提供；complete/partial 的
        quantity 必须非负有限且 <= _MAX_QUANTITY（超过稳定 422；missing 不当 0）；
        unavailable 忽略（可含非零 placeholder）；额外 unpriced meter 忽略。
        未知绝不当 0。

        精度保障：所有 Decimal 运算（含最终 quantize）都在一个 localcontext 内，
        其 precision 由实际输入的位数动态计算（数量位数 + rate 最大 30 位 + 裕量），
        绝不落回默认 28 位导致 InvalidOperation；_MAX_QUANTITY 保证位数有界。
        金额可写性：Numeric(30,10) 上限（绝对值 < 1e20，scale 10）在返回前校验，
        超限稳定 `estimated_cost_exceeds_limit`/422，不允许 PG numeric overflow。
        """
        for line in price.lines:
            avail = availability.get(line.meter)
            if avail not in _AVAILABILITY:
                raise PlatformError(
                    "validation_error",
                    f"availability must provide a legal status for priced meter {line.meter!r}",
                    {},
                    422,
                )
        quantities: dict[str, Decimal] = {}
        max_quantity_digits = 1
        for line in price.lines:
            avail = availability[line.meter]
            if avail == "unavailable":
                continue
            quantity = _measured_quantity(measured, line.meter)
            if quantity > _MAX_QUANTITY:
                raise PlatformError(
                    "validation_error",
                    f"measured quantity for meter {line.meter!r} exceeds the supported maximum",
                    {},
                    422,
                )
            quantities[line.meter] = quantity
            max_quantity_digits = max(max_quantity_digits, len(quantity.as_tuple().digits))
        # 动态精度：数量位数 + rate 最大 30 位 + 求和/取整/舍入裕量；有界（数量 <= 1e18）
        prec = max(60, max_quantity_digits + _RATE_MAX_DIGITS + 30)
        total = Decimal("0")
        status: CostStatus = "complete"
        known = False
        with localcontext() as ctx:
            ctx.prec = prec
            for line in price.lines:
                avail = availability[line.meter]
                if avail == "unavailable":
                    status = "partial"
                    continue
                known = True
                if avail == "partial":
                    status = "partial"
                quantity = quantities[line.meter]
                if quantity == 0:
                    line_total = Decimal("0")
                else:
                    billable = max(quantity, Decimal(line.minimum_billable_quantity))
                    blocks = (billable / Decimal(line.billing_granularity)).to_integral_value(
                        rounding=ROUND_CEILING
                    )
                    line_total = blocks * line.rate
                line_total = line_total.quantize(
                    _MONEY_QUANTUM, rounding=_ROUNDINGS[line.rounding_rule]
                )
                total += line_total
            if not known:
                return None, "unavailable"
            amount = total.quantize(_MONEY_QUANTUM, rounding=ROUND_HALF_UP)
            # Numeric(30,10) 可写性：金额绝对值至多 20 位整数且 scale 量化 10。
            # 在写入 usage_event.estimated_cost_amount 前拒绝，避免 PG numeric overflow。
            if amount >= Decimal("1e20"):
                raise PlatformError(
                    "estimated_cost_exceeds_limit",
                    "Estimated cost exceeds the Numeric(30,10) storage limit",
                    {"estimated_cost_amount": str(amount)},
                    422,
                )
            return amount, status
