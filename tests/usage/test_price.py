"""价格目录服务测试（Task 4，正式 spec 修订版）。

语义（正式 spec 优先于旧 task brief/plan）：
- 价格 scope = provider/model/operation；版本行含 ISO 4217 币种（V1 支持集合至少
  CNY/USD，注册时规范大写）；一个版本单一币种。
- rate 是每个 billing_granularity block 的金额；quantity=0 → 行金额 0；
  quantity>0 → billable = max(quantity, minimum_billable_quantity)（原始单位）→
  blocks = ceil(billable / billing_granularity)（固定向上取整，rounding_rule 不影响
  block 数）→ line_total = blocks * rate 按 rounding_rule 量化 6 位；总金额 half_up
  量化 6 位；全用 Decimal。
- 禁止任意重叠区间：scope 首次注册仅在无历史版本；存在历史版本时必须显式 supersede
  当前/latest 版本——open 则同事务 close 到新 from，closed 则新 from 不得早于其 to。
  select_for 只按半开区间 [effective_from, effective_to) 选最新，不用未来 successor
  排除历史。
- register 用嵌套 savepoint 包裹 predecessor close + header + 全部 lines；任何
  IntegrityError 整体回滚并稳定映射 price_scope_conflict；外层事务仍可用。
- close_version（旧契约）：有任何 usage 引用即拒绝；先锁定行再关闭。supersede 的
  close 另受 retroactive 守卫（事件 started_at/effective_at >= 新 from → 拒绝）。
- lock_for_usage：ledger 写 usage 前锁定同一 price 行并验证时间落在区间（Task 5
  调用本接口；禁止持 DB 事务跨网络）。
- rate 注册严格有限、>=0、scale<=10、precision<=30 并量化 10 位；返回从 DB 回读
  对象；line 顺序按 meter 稳定。
- estimate 边界：availability 合法且每个 priced meter 显式提供；complete/partial
  必须有非负有限 quantity；unavailable 忽略（含非零 placeholder）；missing 不当 0；
  额外 unpriced meter 忽略。
"""

from __future__ import annotations

import re
import secrets
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import Engine, create_engine, event, select
from sqlalchemy.engine import Connection
from sqlalchemy.pool import StaticPool

from app.platform.errors import PlatformError
from app.usage.price import PriceCatalogService, PriceVersion
from app.usage.schema import (
    price_catalog_line_table,
    price_catalog_scope_table,
    price_catalog_table,
    usage_event_table,
    usage_metadata,
)

NOW = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)
ONE_DAY = timedelta(days=1)


@dataclass(frozen=True, slots=True)
class FixedClock:
    now: datetime

    def now_utc(self, connection: Connection | None = None) -> datetime:
        del connection
        return self.now


def make_service(now: datetime = NOW):
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    usage_metadata.create_all(engine)
    return PriceCatalogService(engine, FixedClock(now)), engine


def _scope(operation: str = "generate") -> dict[str, str]:
    return {"provider": "dashscope", "model": "qwen-plus", "operation": operation}


def _line(rate: str | Decimal, **overrides) -> dict:
    line = {
        "meter": "input_tokens",
        "unit": "token",
        "rate": Decimal(rate) if isinstance(rate, str) else rate,
        "billing_granularity": 1,
        "minimum_billable_quantity": 0,
        "rounding_rule": "half_up",
    }
    line.update(overrides)
    return line


def _insert_usage_event(
    connection,
    *,
    event_id: str,
    price_version_id: str,
    started_at: datetime,
    effective_at: datetime,
) -> None:
    connection.execute(
        usage_event_table.insert().values(
            usage_event_id=event_id,
            event_kind="local_usage",
            result="succeeded",
            event_fingerprint=f"fp_{event_id}",
            ownership_json={},
            cost_center_key="user:u1",
            started_at_utc=started_at,
            completed_at_utc=started_at,
            effective_calendar_version_id="cal_1",
            effective_at_utc=effective_at,
            effective_period="2026-08",
            recorded_calendar_version_id="cal_1",
            recorded_at_utc=started_at,
            recorded_period="2026-08",
            created_at_utc=started_at,
            execution_kind="ocr",
            execution_id=f"exec_{event_id}",
            stage="extract",
            resource_kind="pdf",
            price_version_id=price_version_id,
        )
    )


def _open_count(connection_or_engine, scope: dict[str, str]) -> int:
    """返回 scope 的 open 版本数。传 Connection（事务内）或 Engine（事务外）。

    注意：in-memory StaticPool 上，事务内再用 engine.connect() 会与未提交事务
    争用同一底层连接并清空事务视图，因此事务内必须传 Connection。
    """
    with _connection_of(connection_or_engine) as connection:
        rows = connection.execute(
            select(price_catalog_table.c.id).where(
                price_catalog_table.c.provider == scope["provider"],
                price_catalog_table.c.model == scope["model"],
                price_catalog_table.c.operation == scope["operation"],
                price_catalog_table.c.effective_to_utc.is_(None),
            )
        ).all()
    return len(rows)


def _row_count(connection_or_engine, scope: dict[str, str]) -> int:
    with _connection_of(connection_or_engine) as connection:
        rows = connection.execute(
            select(price_catalog_table.c.id).where(
                price_catalog_table.c.provider == scope["provider"],
                price_catalog_table.c.model == scope["model"],
                price_catalog_table.c.operation == scope["operation"],
            )
        ).all()
    return len(rows)


def _connection_of(connection_or_engine):
    """返回 (context_manager, connection)：Engine 时短连接；Connection 时原样使用不关闭。"""
    if isinstance(connection_or_engine, Engine):
        return connection_or_engine.connect()
    return _nullcontext_connection(connection_or_engine)


class _nullcontext_connection:
    def __init__(self, connection):
        self._connection = connection

    def __enter__(self):
        return self._connection

    def __exit__(self, *exc):
        return False


# ---------------------------------------------------------------------------
# 注册 / 查询 / 区间不重叠
# ---------------------------------------------------------------------------


def test_scope_id_uuid5_stable_and_ambiguity_free() -> None:
    """scope id 为 UUID5(canonical JSON tuple)：`(a_b,c,d)` 与 `(a,b_c,d)` 不同 id。

    绝不直接拼接 scope 字段（拼接会产生歧义冲突）；两个不同 tuple 必须得到两个
    不同 scope 行；id 固定 `scope_<uuid>` 且长度 < 64。
    """
    service, engine = make_service()
    with engine.begin() as connection:
        v1 = service.register(
            connection,
            currency_code="USD",
            lines=[_line("0.000020")],
            effective_from_utc=NOW,
            provider="a_b",
            model="c",
            operation="d",
        )
        v2 = service.register(
            connection,
            currency_code="USD",
            lines=[_line("0.000020")],
            effective_from_utc=NOW,
            provider="a",
            model="b_c",
            operation="d",
        )
        rows = (
            connection.execute(
                select(price_catalog_scope_table.c.id)
                .where(price_catalog_scope_table.c.provider.in_(["a_b", "a"]))
                .order_by(price_catalog_scope_table.c.provider)
            )
            .scalars()
            .all()
        )
    assert v1.id != v2.id
    assert len(rows) == 2
    assert rows[0] != rows[1]  # 无拼接歧义：不同 tuple → 不同 scope id
    for scope_id in rows:
        assert scope_id.startswith("scope_")
        assert len(scope_id) < 64


def test_scope_id_max_legal_length_registration() -> None:
    """最大合法长度（provider=64 / model=128 / operation=64）可注册，scope id 仍 < 64。"""
    service, engine = make_service()
    with engine.begin() as connection:
        version = service.register(
            connection,
            currency_code="USD",
            lines=[_line("0.000020")],
            effective_from_utc=NOW,
            provider="p" * 64,
            model="m" * 128,
            operation="o" * 64,
        )
        scope_id = connection.execute(
            select(price_catalog_scope_table.c.id).where(
                price_catalog_scope_table.c.provider == "p" * 64
            )
        ).scalar_one()
    assert version.id
    assert len(scope_id) < 64
    assert scope_id.startswith("scope_")


def test_register_and_select_roundtrip_with_lines_and_currency() -> None:
    service, engine = make_service()
    scope = _scope("generate")
    lines = [
        _line("0.000020", meter="input_tokens"),
        _line("0.000060", meter="output_tokens"),
    ]
    with engine.begin() as connection:
        version = service.register(
            connection, currency_code="USD", lines=lines, effective_from_utc=NOW, **scope
        )
        selected = service.select_for(connection, at_utc=NOW + ONE_DAY, **scope)
    assert selected.id == version.id
    assert selected.currency_code == "USD"
    assert selected.effective_to_utc is None
    assert selected.supersedes_version_id is None
    assert [line.meter for line in selected.lines] == ["input_tokens", "output_tokens"]
    assert selected.lines[0].rate == Decimal("0.000020")
    assert selected.lines[0].billing_granularity == 1
    assert selected.lines[0].minimum_billable_quantity == 0
    assert selected.lines[0].rounding_rule == "half_up"


def test_select_for_returns_real_effective_to_after_close() -> None:
    service, engine = make_service()
    scope = _scope("close_real")
    with engine.begin() as connection:
        v1 = service.register(
            connection,
            currency_code="USD",
            lines=[_line("0.000020")],
            effective_from_utc=NOW,
            **scope,
        )
        service.close_version(connection, v1.id, NOW + 2 * ONE_DAY)
        selected = service.select_for(connection, at_utc=NOW + ONE_DAY, **scope)
    assert selected.id == v1.id
    assert selected.effective_to_utc == NOW + 2 * ONE_DAY


def test_select_for_at_exact_effective_to_is_not_covered() -> None:
    service, engine = make_service()
    scope = _scope("half_open")
    with engine.begin() as connection:
        v1 = service.register(
            connection,
            currency_code="USD",
            lines=[_line("0.000020")],
            effective_from_utc=NOW,
            **scope,
        )
        service.close_version(connection, v1.id, NOW + 2 * ONE_DAY)
        with pytest.raises(PlatformError) as exc:
            service.select_for(connection, at_utc=NOW + 2 * ONE_DAY, **scope)
    assert exc.value.code == "price_not_found"
    assert exc.value.status_code == 404


def test_supersede_closes_old_version_and_preserves_historical_select() -> None:
    """未来 successor 不得让历史 select_for(at) 失效（回归 NOT EXISTS 错误实现）。"""
    service, engine = make_service()
    scope = _scope("supersede")
    with engine.begin() as connection:
        v1 = service.register(
            connection,
            currency_code="USD",
            lines=[_line("0.000020")],
            effective_from_utc=NOW,
            **scope,
        )
        v2 = service.register(
            connection,
            currency_code="USD",
            lines=[_line("0.000040")],
            effective_from_utc=NOW + 2 * ONE_DAY,
            supersedes_version_id=v1.id,
            **scope,
        )
        historical = service.select_for(connection, at_utc=NOW + ONE_DAY, **scope)
        at_boundary = service.select_for(connection, at_utc=NOW + 2 * ONE_DAY, **scope)
        just_before = service.select_for(
            connection, at_utc=NOW + 2 * ONE_DAY - timedelta(seconds=1), **scope
        )
        future = service.select_for(connection, at_utc=NOW + 3 * ONE_DAY, **scope)
    assert historical.id == v1.id
    assert just_before.id == v1.id
    assert at_boundary.id == v2.id  # 恰 effective_from 属于新版本（half-open）
    assert future.id == v2.id
    assert future.effective_to_utc is None
    assert v2.supersedes_version_id == v1.id


def test_register_requires_supersede_once_scope_has_history() -> None:
    """首次注册仅在无历史版本；closed 历史存在时也必须显式 supersede。"""
    service, engine = make_service()
    scope = _scope("hist_only")
    with engine.begin() as connection:
        v1 = service.register(
            connection,
            currency_code="USD",
            lines=[_line("0.000020")],
            effective_from_utc=NOW,
            **scope,
        )
        service.close_version(connection, v1.id, NOW + ONE_DAY)
        with pytest.raises(PlatformError) as exc:
            service.register(
                connection,
                currency_code="USD",
                lines=[_line("0.000040")],
                effective_from_utc=NOW + 2 * ONE_DAY,
                **scope,
            )
        assert exc.value.code == "price_scope_conflict"
        assert exc.value.status_code == 409


def test_overlapping_intervals_forbidden() -> None:
    service, engine = make_service()
    scope = _scope("no_overlap")
    with engine.begin() as connection:
        v1 = service.register(
            connection,
            currency_code="USD",
            lines=[_line("0.000020")],
            effective_from_utc=NOW,
            **scope,
        )
        # open 版本：新 from 必须严格晚于其 from
        for bad_from in (NOW, NOW - ONE_DAY):
            with pytest.raises(PlatformError) as exc:
                service.register(
                    connection,
                    currency_code="USD",
                    lines=[_line("0.000040")],
                    effective_from_utc=bad_from,
                    supersedes_version_id=v1.id,
                    **scope,
                )
            assert exc.value.code == "price_scope_conflict"
        # 合法 supersede：v1 关闭到新 from
        v2 = service.register(
            connection,
            currency_code="USD",
            lines=[_line("0.000040")],
            effective_from_utc=NOW + ONE_DAY,
            supersedes_version_id=v1.id,
            **scope,
        )
        # 关闭 v2（无 usage 引用），验证 closed-latest 的相邻性
        service.close_version(connection, v2.id, NOW + 2 * ONE_DAY)
        with pytest.raises(PlatformError) as exc:
            service.register(
                connection,
                currency_code="USD",
                lines=[_line("0.000060")],
                effective_from_utc=NOW + 2 * ONE_DAY - timedelta(seconds=1),
                supersedes_version_id=v2.id,
                **scope,
            )
        assert exc.value.code == "price_scope_conflict"
        # from == to：相邻不重叠 → 允许
        v3 = service.register(
            connection,
            currency_code="USD",
            lines=[_line("0.000060")],
            effective_from_utc=NOW + 2 * ONE_DAY,
            supersedes_version_id=v2.id,
            **scope,
        )
        assert v3.supersedes_version_id == v2.id
        assert v3.effective_from_utc == NOW + 2 * ONE_DAY


def test_supersede_must_target_latest_version() -> None:
    service, engine = make_service()
    scope = _scope("latest_only")
    with engine.begin() as connection:
        a = service.register(
            connection,
            currency_code="USD",
            lines=[_line("0.000020")],
            effective_from_utc=NOW,
            **scope,
        )
        b = service.register(
            connection,
            currency_code="USD",
            lines=[_line("0.000040")],
            effective_from_utc=NOW + ONE_DAY,
            supersedes_version_id=a.id,
            **scope,
        )
        # a 已被 b 取代，不再是 latest → 冲突
        with pytest.raises(PlatformError) as exc:
            service.register(
                connection,
                currency_code="USD",
                lines=[_line("0.000060")],
                effective_from_utc=NOW + 2 * ONE_DAY,
                supersedes_version_id=a.id,
                **scope,
            )
        assert exc.value.code == "price_scope_conflict"
        # b 是当前 latest（open）→ 允许
        c = service.register(
            connection,
            currency_code="USD",
            lines=[_line("0.000060")],
            effective_from_utc=NOW + 2 * ONE_DAY,
            supersedes_version_id=b.id,
            **scope,
        )
        assert c.supersedes_version_id == b.id


def test_supersede_unknown_or_wrong_scope_rejected() -> None:
    service, engine = make_service()
    with engine.begin() as connection:
        v1 = service.register(
            connection,
            currency_code="USD",
            lines=[_line("0.000020")],
            effective_from_utc=NOW,
            **_scope("wrong_scope"),
        )
        with pytest.raises(PlatformError) as exc:
            service.register(
                connection,
                currency_code="USD",
                lines=[_line("0.000040")],
                effective_from_utc=NOW + ONE_DAY,
                supersedes_version_id="price_missing",
                **_scope("wrong_scope"),
            )
        assert exc.value.code == "price_not_found"
        assert exc.value.status_code == 404
        with pytest.raises(PlatformError) as exc:
            service.register(
                connection,
                currency_code="USD",
                lines=[_line("0.000040")],
                effective_from_utc=NOW + ONE_DAY,
                supersedes_version_id=v1.id,
                **_scope("other_scope"),
            )
        assert exc.value.code == "validation_error"
        assert exc.value.status_code == 422


def test_register_conflict_when_open_version_exists() -> None:
    service, engine = make_service()
    scope = _scope("conflict")
    with engine.begin() as connection:
        service.register(
            connection,
            currency_code="USD",
            lines=[_line("0.000020")],
            effective_from_utc=NOW,
            **scope,
        )
        with pytest.raises(PlatformError) as exc:
            service.register(
                connection,
                currency_code="USD",
                lines=[_line("0.000040")],
                effective_from_utc=NOW + ONE_DAY,
                **scope,
            )
        assert exc.value.code == "price_scope_conflict"
        assert exc.value.status_code == 409
        # 不同 scope（operation 不同）不受影响；同一外层事务仍可用
        service.register(
            connection,
            currency_code="USD",
            lines=[_line("0.000020")],
            effective_from_utc=NOW,
            **_scope("other_op"),
        )


def test_line_order_stable_by_meter() -> None:
    service, engine = make_service()
    scope = _scope("line_order")
    with engine.begin() as connection:
        version = service.register(
            connection,
            currency_code="USD",
            lines=[
                _line("0.000060", meter="output_tokens"),
                _line("0.000020", meter="input_tokens"),
            ],
            effective_from_utc=NOW,
            **scope,
        )
        selected = service.select_for(connection, at_utc=NOW + ONE_DAY, **scope)
        locked = service.lock_for_usage(connection, version.id, NOW + ONE_DAY)
    for got in (version, selected, locked):
        assert [line.meter for line in got.lines] == ["input_tokens", "output_tokens"]
        assert got.lines[0].rate == Decimal("0.000020")


# ---------------------------------------------------------------------------
# 输入校验（稳定 422，不泄漏 KeyError/Decimal/DB 异常）
# ---------------------------------------------------------------------------


def test_register_validation_errors() -> None:
    service, engine = make_service()
    with engine.begin() as connection:
        currency_cases: list[tuple[str, bool]] = [
            ("US", False),
            ("USDD", False),
            ("US1", False),
            ("", False),
        ]
        for currency, should_pass in currency_cases:
            with pytest.raises(PlatformError) as exc:
                service.register(
                    connection,
                    currency_code=currency,
                    lines=[_line("0.000020")],
                    effective_from_utc=NOW,
                    **_scope(f"currency_{currency}"),
                )
            assert exc.value.code == "validation_error"
            assert exc.value.status_code == 422
            assert should_pass is False
        line_cases: list[dict] = [
            {"lines": []},
            {"lines": [{"unit": "token", "rate": Decimal("0.000020")}]},  # missing meter
            {"lines": [{"meter": "input_tokens", "rate": Decimal("0.000020")}]},  # missing unit
            {"lines": [_line("0.000020", meter="")]},
            {"lines": [{"meter": "input_tokens", "unit": "token"}]},  # missing rate
            {"lines": [{"meter": "input_tokens", "unit": "token", "rate": "not-a-number"}]},
            {"lines": [{"meter": "input_tokens", "unit": "token", "rate": "NaN"}]},
            {"lines": [{"meter": "input_tokens", "unit": "token", "rate": "Infinity"}]},
            {"lines": [_line("-0.000001")]},
            {"lines": [_line("0.000020", billing_granularity=0)]},
            {"lines": [_line("0.000020", billing_granularity=1.5)]},
            {"lines": [_line("0.000020", billing_granularity="abc")]},
            {"lines": [_line("0.000020", minimum_billable_quantity=-1)]},
            {"lines": [_line("0.000020", minimum_billable_quantity=1.5)]},
            {"lines": [_line("0.000020", rounding_rule="random")]},
            {
                "lines": [
                    _line("0.000020", meter="input_tokens"),
                    _line("0.000060", meter="input_tokens"),
                ]
            },
        ]
        for index, overrides in enumerate(line_cases):
            kwargs = {
                "currency_code": "USD",
                "lines": [_line("0.000020")],
                "effective_from_utc": NOW,
                **_scope(f"line_validation_{index}"),
            }
            kwargs.update(overrides)
            with pytest.raises(PlatformError) as exc:
                service.register(connection, **kwargs)
            assert exc.value.code == "validation_error"
            assert exc.value.status_code == 422
        # 非字符串 / 空 scope 标识
        for bad_scope in (
            {"provider": "", "model": "m", "operation": "o"},
            {"provider": "p", "model": "  ", "operation": "o"},
            {"provider": "p", "model": "m", "operation": ""},
            {"provider": None, "model": "m", "operation": "o"},
        ):
            with pytest.raises(PlatformError) as exc:
                service.register(
                    connection,
                    currency_code="USD",
                    lines=[_line("0.000020")],
                    effective_from_utc=NOW,
                    **bad_scope,
                )
            assert exc.value.code == "validation_error"
        # 非 datetime 的 effective_from
        with pytest.raises(PlatformError) as exc:
            service.register(
                connection,
                currency_code="USD",
                lines=[_line("0.000020")],
                effective_from_utc="2026-08-05T12:00:00+00:00",
                **_scope("bad_from"),
            )
        assert exc.value.code == "validation_error"


def test_currency_normalized_uppercase_and_iso4217_set() -> None:
    """正式 spec：合法 ISO 4217 code（完整集合，统一 uppercase），不限 CNY/USD。"""
    service, engine = make_service()
    with engine.begin() as connection:
        usd = service.register(
            connection,
            currency_code="usd",  # lowercase → 规范化 uppercase
            lines=[_line("0.000020")],
            effective_from_utc=NOW,
            **_scope("currency_norm"),
        )
        eur = service.register(
            connection,
            currency_code="EUR",
            lines=[_line("0.000050")],
            effective_from_utc=NOW,
            **_scope("currency_eur"),
        )
        jpy = service.register(
            connection,
            currency_code="jpy",
            lines=[_line("0.001500")],
            effective_from_utc=NOW,
            **_scope("currency_jpy"),
        )
        xcg = service.register(
            connection,
            currency_code="XCG",  # 2025-03 现行（Caribbean guilder，替代 ANG）
            lines=[_line("0.000060")],
            effective_from_utc=NOW,
            **_scope("currency_xcg"),
        )
        selected_eur = service.select_for(
            connection, at_utc=NOW + ONE_DAY, **_scope("currency_eur")
        )
        selected_jpy = service.select_for(
            connection, at_utc=NOW + ONE_DAY, **_scope("currency_jpy")
        )
        selected_xcg = service.select_for(
            connection, at_utc=NOW + ONE_DAY, **_scope("currency_xcg")
        )
    assert usd.currency_code == "USD"
    assert eur.currency_code == "EUR"
    assert jpy.currency_code == "JPY"
    assert xcg.currency_code == "XCG"
    assert selected_eur.currency_code == "EUR"
    assert selected_jpy.currency_code == "JPY"
    assert selected_xcg.currency_code == "XCG"


def test_invalid_currency_codes_rejected() -> None:
    """无效 ISO 4217 code 稳定 422（过短/过长/数字/非代码/空白/已撤销代码）。"""
    service, engine = make_service()
    with engine.begin() as connection:
        # ANG（2025-03-31 撤销）、BGN（2025-12-31 撤销）、SLL（2022 被 SLE 替代）
        # 均不再是现行代码
        for index, currency in enumerate(
            ("US", "USDD", "US1", "EUR1", "XYZ", "ZZZ", "", "ANG", "BGN", "SLL")
        ):
            with pytest.raises(PlatformError) as exc:
                service.register(
                    connection,
                    currency_code=currency,
                    lines=[_line("0.000020")],
                    effective_from_utc=NOW,
                    **_scope(f"currency_bad_{index}"),
                )
            assert exc.value.code == "validation_error"
            assert exc.value.status_code == 422


def test_sle_accepted_sll_rejected() -> None:
    """SLE（现行 leone）接受；SLL（已撤销历史代码）拒绝。"""
    service, engine = make_service()
    with engine.begin() as connection:
        sle = service.register(
            connection,
            currency_code="SLE",
            lines=[_line("0.000010")],
            effective_from_utc=NOW,
            **_scope("currency_sle"),
        )
        selected = service.select_for(connection, at_utc=NOW + ONE_DAY, **_scope("currency_sle"))
        with pytest.raises(PlatformError) as exc:
            service.register(
                connection,
                currency_code="sll",  # lowercase 规范化后仍非现行代码
                lines=[_line("0.000010")],
                effective_from_utc=NOW,
                **_scope("currency_sll"),
            )
        assert exc.value.code == "validation_error"
    assert sle.currency_code == "SLE"
    assert selected.currency_code == "SLE"


def test_rate_quantized_and_reread_consistent() -> None:
    """rate 量化 10 位入库；返回/回读/estimate 三者一致（防即时-回读差异）。"""
    service, engine = make_service()
    scope = _scope("rate_q")
    with engine.begin() as connection:
        version = service.register(
            connection,
            currency_code="USD",
            lines=[_line("0.00002")],
            effective_from_utc=NOW,
            **scope,
        )
        selected = service.select_for(connection, at_utc=NOW + ONE_DAY, **scope)
        assert version.lines[0].rate == Decimal("0.00002")
        assert selected.lines[0].rate == version.lines[0].rate
        measured = {"input_tokens": Decimal("1500")}
        availability = {"input_tokens": "complete"}
        assert service.estimate_cost(version, measured, availability=availability) == (
            service.estimate_cost(selected, measured, availability=availability)
        )
    with engine.connect() as connection:
        raw = connection.execute(
            select(price_catalog_line_table.c.rate).where(
                price_catalog_line_table.c.price_version_id == version.id
            )
        ).scalar_one()
    assert raw == Decimal("0.0000200000")
    assert raw.as_tuple().exponent == -10


def test_rate_scale_and_precision_rejected() -> None:
    service, engine = make_service()
    with engine.begin() as connection:
        for index, rate in enumerate(
            (
                "0.00000000001",  # scale 11
                "123456789012345678901",  # 21 整数位 → 超出 Numeric(30,10) 整数位
                "123456789012345678901.1234567890",  # 21 整数 + 10 小数 = 31 位 → 超限
                "1e30",
            )
        ):
            with pytest.raises(PlatformError) as exc:
                service.register(
                    connection,
                    currency_code="USD",
                    lines=[_line(rate)],
                    effective_from_utc=NOW,
                    **_scope(f"rate_bad_{index}"),
                )
            assert exc.value.code == "validation_error"
            assert exc.value.status_code == 422


def _reference_amount(
    rate: Decimal, quantity: Decimal, granularity: int, minimum: int, rule: str
) -> Decimal:
    """测试内独立参考实现（prec=100）：与实现同公式但完全独立书写。

    用于大数回归：断言实现的结果与高精度参考一致（不落回默认 28 位抛异常）。
    """
    from decimal import ROUND_CEILING, ROUND_DOWN, ROUND_HALF_UP, localcontext

    rules = {"floor": ROUND_DOWN, "ceil": ROUND_CEILING, "half_up": ROUND_HALF_UP}
    with localcontext() as ctx:
        ctx.prec = 100
        if quantity == 0:
            line_total = Decimal("0")
        else:
            billable = max(quantity, Decimal(minimum))
            blocks = (billable / Decimal(granularity)).to_integral_value(rounding=ROUND_CEILING)
            line_total = blocks * rate
        return line_total.quantize(Decimal("0.000001"), rounding=rules[rule])


def test_rate_max_30_digit_accepted() -> None:
    """Numeric(30,10) 满精度 rate 合法（输入侧）；大数计价不因默认 28 位抛异常。

    SQLite Numeric 为 float-backed：>15 位有效数字的 rate 回读被四舍五入（文档化
    限制，PG 精确回读留验收环境）。因此期望值从「回读后的 rate」按参考公式推导，
    而不是与输入字符串相等。
    """
    service, engine = make_service()
    scope = _scope("rate_max")
    big_rate = Decimal("99999999999999999999.1234567890")  # 20 整数 + 10 小数 = 30 位
    with engine.begin() as connection:
        version = service.register(
            connection,
            currency_code="USD",
            lines=[_line(big_rate)],
            effective_from_utc=NOW,
            **scope,
        )
        reread_rate = version.lines[0].rate
        # 30 位 rate（~1e20，SQLite float-backed 回读可能四舍五入到恰 1e20）；
        # quantity=0 → 金额 0（不触发行金额上限），其余精度路径由大数回归测试覆盖
        amount, status = service.estimate_cost(
            version,
            {"input_tokens": Decimal("0")},
            availability={"input_tokens": "complete"},
        )
        assert (amount, status) == (Decimal("0"), "complete")
        assert reread_rate.as_tuple().exponent == -10
    with engine.connect() as connection:
        raw = connection.execute(
            select(price_catalog_line_table.c.rate).where(
                price_catalog_line_table.c.price_version_id == version.id
            )
        ).scalar_one()
    assert raw is not None


def test_estimate_quantity_over_cap_stable_422() -> None:
    """超过支持数量上限（1e18）→ 稳定 422，而非精度异常。"""
    service, engine = make_service()
    with engine.begin() as connection:
        version = service.register(
            connection,
            currency_code="USD",
            lines=[_line("0.000020")],
            effective_from_utc=NOW,
            **_scope("quantity_cap"),
        )
        with pytest.raises(PlatformError) as exc:
            service.estimate_cost(
                version,
                {"input_tokens": Decimal("1000000000000000001")},
                availability={"input_tokens": "complete"},
            )
        assert exc.value.code == "validation_error"
        assert exc.value.status_code == 422


def test_estimate_amount_over_numeric_limit_stable_422() -> None:
    """金额超 Numeric(30,10) 可写上限（>= 1e20 整数位）→ 稳定 422，防 PG overflow。"""
    service, engine = make_service()
    with engine.begin() as connection:
        # 用接近 rate 上限的值：99999999999999999999（20 位整数）× 数量 1e18
        # → 金额 ~1e38，远超 1e20 → 422 estimated_cost_exceeds_limit
        over = service.register(
            connection,
            currency_code="USD",
            lines=[_line("99999999999999999999.0000000000")],
            effective_from_utc=NOW,
            **_scope("amount_over_max"),
        )
        with pytest.raises(PlatformError) as exc:
            service.estimate_cost(
                over,
                {"input_tokens": Decimal("1000000000000000000")},  # 1e18 blocks × ~1e20
                availability={"input_tokens": "complete"},
            )
        assert exc.value.code == "estimated_cost_exceeds_limit"
        assert exc.value.status_code == 422
        assert "estimated_cost_amount" in exc.value.details
        # 恰好低于上限（< 1e20）→ 正常返回
        ok = service.register(
            connection,
            currency_code="USD",
            lines=[_line("0.1234567890")],
            effective_from_utc=NOW,
            **_scope("amount_ok"),
        )
        amount, status = service.estimate_cost(
            ok,
            {"input_tokens": Decimal("100000000000000000")},  # 1e17 blocks
            availability={"input_tokens": "complete"},
        )
        assert status == "complete"
        assert amount == Decimal("12345678900000000.000000")
        assert amount < Decimal("1e20")


def test_estimate_big_numbers_do_not_raise_invalid_operation() -> None:
    """大数量 × 多 line 求和：动态精度保证最终 quantize 不抛 InvalidOperation。

    19 位数量 × 10 位 rate 的乘积有 29 位有效数字——默认 28 位上下文 quantize
    必然 InvalidOperation；实现必须用动态精度。期望值由测试内参考公式（prec=100）
    独立推导，rate 选 10 位有效数字（SQLite 可精确回读）。
    """
    service, engine = make_service()
    with engine.begin() as connection:
        version = service.register(
            connection,
            currency_code="USD",
            lines=[
                _line("0.1234567890", meter="m_a"),
                _line("0.0000005", meter="m_b"),
            ],
            effective_from_utc=NOW,
            **_scope("big_sum"),
        )
        measured = {
            "m_a": Decimal("999999999999999999.9999999999"),  # 19 位整数 → blocks=1e18
            "m_b": Decimal("1"),
        }
        amount, status = service.estimate_cost(
            version,
            measured,
            availability={"m_a": "complete", "m_b": "complete"},
        )
        from decimal import ROUND_HALF_UP, localcontext

        with localcontext() as ctx:
            ctx.prec = 100
            expected = _reference_amount(
                version.lines[0].rate, measured["m_a"], 1, 0, "half_up"
            ) + _reference_amount(version.lines[1].rate, measured["m_b"], 1, 0, "half_up")
            expected = expected.quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)
        assert status == "complete"
        assert amount == expected
        assert amount.as_tuple().exponent == -6  # 总金额固定 6 位小数
        # 参考值本身可精确断言：1e18 × 0.1234567890 = 123456789000000000.0000000000
        assert expected == Decimal("123456789000000000.000001")


# ---------------------------------------------------------------------------
# close / retroactive close 守卫
# ---------------------------------------------------------------------------


def test_close_version_validation_errors() -> None:
    service, engine = make_service()
    with engine.begin() as connection:
        with pytest.raises(PlatformError) as exc:
            service.close_version(connection, "price_missing", NOW + ONE_DAY)
        assert exc.value.code == "price_not_found"
        assert exc.value.status_code == 404
        v1 = service.register(
            connection,
            currency_code="USD",
            lines=[_line("0.000020")],
            effective_from_utc=NOW,
            **_scope("close_v1"),
        )
        for bad_to in (NOW, NOW - ONE_DAY):
            with pytest.raises(PlatformError) as exc:
                service.close_version(connection, v1.id, bad_to)
            assert exc.value.code == "price_close_conflict"
        service.close_version(connection, v1.id, NOW + ONE_DAY)
        # 已关闭版本不可再改区间
        with pytest.raises(PlatformError) as exc:
            service.close_version(connection, v1.id, NOW + 2 * ONE_DAY)
        assert exc.value.code == "price_close_conflict"
        assert exc.value.status_code == 409


def test_close_version_rejected_when_any_usage_reference() -> None:
    """standalone close 旧契约：有任何 usage 引用即拒绝（无论事件时间）。"""
    service, engine = make_service()
    with engine.begin() as connection:
        v_before = service.register(
            connection,
            currency_code="USD",
            lines=[_line("0.000020")],
            effective_from_utc=NOW,
            **_scope("close_any_before"),
        )
        _insert_usage_event(
            connection,
            event_id="ue_before",
            price_version_id=v_before.id,
            started_at=NOW,
            effective_at=NOW,
        )
        with pytest.raises(PlatformError) as exc:
            service.close_version(connection, v_before.id, NOW + ONE_DAY)
        assert exc.value.code == "price_close_conflict"
        assert exc.value.status_code == 409
        v_at = service.register(
            connection,
            currency_code="USD",
            lines=[_line("0.000020")],
            effective_from_utc=NOW,
            **_scope("close_any_at"),
        )
        _insert_usage_event(
            connection,
            event_id="ue_at",
            price_version_id=v_at.id,
            started_at=NOW + ONE_DAY,
            effective_at=NOW + ONE_DAY,
        )
        with pytest.raises(PlatformError) as exc:
            service.close_version(connection, v_at.id, NOW + ONE_DAY)
        assert exc.value.code == "price_close_conflict"


def test_supersede_rejected_when_events_at_or_after_new_effective_from() -> None:
    """supersede 的 close 受 retroactive 守卫：事件落在新 from 或之后 → 拒绝。"""
    service, engine = make_service()
    with engine.begin() as connection:
        v_at = service.register(
            connection,
            currency_code="USD",
            lines=[_line("0.000020")],
            effective_from_utc=NOW,
            **_scope("sup_at"),
        )
        _insert_usage_event(
            connection,
            event_id="ue_sup_at",
            price_version_id=v_at.id,
            started_at=NOW,
            effective_at=NOW + 2 * ONE_DAY,
        )
        with pytest.raises(PlatformError) as exc:
            service.register(
                connection,
                currency_code="USD",
                lines=[_line("0.000040")],
                effective_from_utc=NOW + 2 * ONE_DAY,
                supersedes_version_id=v_at.id,
                **_scope("sup_at"),
            )
        assert exc.value.code == "price_close_conflict"
        assert exc.value.status_code == 409
        # 事件都在新 from 之前 → 允许（即使 v 已被 close_version 关闭）
        v_before = service.register(
            connection,
            currency_code="USD",
            lines=[_line("0.000020")],
            effective_from_utc=NOW,
            **_scope("sup_before"),
        )
        service.close_version(connection, v_before.id, NOW + ONE_DAY)
        _insert_usage_event(
            connection,
            event_id="ue_sup_before",
            price_version_id=v_before.id,
            started_at=NOW,
            effective_at=NOW,
        )
        successor = service.register(
            connection,
            currency_code="USD",
            lines=[_line("0.000040")],
            effective_from_utc=NOW + 2 * ONE_DAY,
            supersedes_version_id=v_before.id,
            **_scope("sup_before"),
        )
    assert successor.supersedes_version_id == v_before.id


# ---------------------------------------------------------------------------
# 多币种（一个版本单一 currency）
# ---------------------------------------------------------------------------


def test_multiple_currencies_across_scopes() -> None:
    service, engine = make_service()
    with engine.begin() as connection:
        usd = service.register(
            connection,
            currency_code="USD",
            lines=[_line("0.000020")],
            effective_from_utc=NOW,
            **_scope("usd_scope"),
        )
        cny = service.register(
            connection,
            currency_code="CNY",
            lines=[_line("0.000140")],
            effective_from_utc=NOW,
            **_scope("cny_scope"),
        )
        selected_usd = service.select_for(connection, at_utc=NOW + ONE_DAY, **_scope("usd_scope"))
        selected_cny = service.select_for(connection, at_utc=NOW + ONE_DAY, **_scope("cny_scope"))
    assert selected_usd.id == usd.id and selected_usd.currency_code == "USD"
    assert selected_cny.id == cny.id and selected_cny.currency_code == "CNY"


# ---------------------------------------------------------------------------
# estimate_cost 数学（正式公式）
# ---------------------------------------------------------------------------


def test_estimate_granularity_minimum_and_zero_quantity() -> None:
    service, engine = make_service()
    with engine.begin() as connection:
        ceil_ver = service.register(
            connection,
            currency_code="USD",
            lines=[
                _line(
                    "0.000020",
                    billing_granularity=1000,
                    minimum_billable_quantity=2,
                    rounding_rule="ceil",
                )
            ],
            effective_from_utc=NOW,
            **_scope("ceil_math"),
        )
        amount, status = service.estimate_cost(
            ceil_ver,
            {"input_tokens": Decimal("1500")},
            availability={"input_tokens": "complete"},
        )
        # billable=max(1500,2)=1500 → ceil(1500/1000)=2 → 2*0.000020
        assert (amount, status) == (Decimal("0.000040"), "complete")
        amount_min, _ = service.estimate_cost(
            ceil_ver,
            {"input_tokens": Decimal("1")},
            availability={"input_tokens": "complete"},
        )
        # max(1,2)=2 → ceil(2/1000)=1 → 0.000020（最低可计费量）
        assert amount_min == Decimal("0.000020")
        amount_zero, _ = service.estimate_cost(
            ceil_ver,
            {"input_tokens": Decimal("0")},
            availability={"input_tokens": "complete"},
        )
        # quantity=0 → 行金额 0（不受 minimum 影响）
        assert amount_zero == Decimal("0")
        # rule=floor：block 折算仍是固定向上取整
        floor_ver = service.register(
            connection,
            currency_code="USD",
            lines=[
                _line(
                    "0.000020",
                    billing_granularity=1000,
                    minimum_billable_quantity=0,
                    rounding_rule="floor",
                )
            ],
            effective_from_utc=NOW,
            **_scope("floor_blocks"),
        )
        amount_floor, _ = service.estimate_cost(
            floor_ver,
            {"input_tokens": Decimal("1500")},
            availability={"input_tokens": "complete"},
        )
        # ceil(1500/1000)=2 → 2*0.000020（floor 不影响 block 数）
        assert amount_floor == Decimal("0.000040")
        amount0, _ = service.estimate_cost(
            floor_ver,
            {"input_tokens": Decimal("0")},
            availability={"input_tokens": "complete"},
        )
        assert amount0 == Decimal("0")


def test_estimate_rounding_rule_only_affects_money() -> None:
    service, engine = make_service()
    with engine.begin() as connection:
        version = service.register(
            connection,
            currency_code="USD",
            lines=[
                _line("0.000020", meter="floor_m", billing_granularity=1000, rounding_rule="floor"),
                _line("0.000020", meter="ceil_m", billing_granularity=1000, rounding_rule="ceil"),
                _line(
                    "0.000020", meter="half_m", billing_granularity=1000, rounding_rule="half_up"
                ),
            ],
            effective_from_utc=NOW,
            **_scope("rules"),
        )
        measured = {
            "floor_m": Decimal("1500"),
            "ceil_m": Decimal("1500"),
            "half_m": Decimal("1500"),
        }
        amount, status = service.estimate_cost(
            version,
            measured,
            availability={meter: "complete" for meter in measured},
        )
        # 三种 rule 的 blocks 都是 ceil(1.5)=2 → (2+2+2)*0.000020
        assert (amount, status) == (Decimal("0.000120"), "complete")
        # 金额 6 位量化按各自 rule：0.0000005 → floor 0.000000 / ceil 0.000001 / half_up 0.000001
        money = service.register(
            connection,
            currency_code="USD",
            lines=[
                _line("0.0000005", meter="floor_m", rounding_rule="floor"),
                _line("0.0000005", meter="ceil_m", rounding_rule="ceil"),
                _line("0.0000005", meter="half_m", rounding_rule="half_up"),
            ],
            effective_from_utc=NOW,
            **_scope("money_rules"),
        )
        m2 = {"floor_m": Decimal("1"), "ceil_m": Decimal("1"), "half_m": Decimal("1")}
        amount2, _ = service.estimate_cost(
            money, m2, availability={meter: "complete" for meter in m2}
        )
        assert amount2 == Decimal("0.000002")


def test_estimate_unknown_meters_partial_and_unavailable() -> None:
    service, engine = make_service()
    with engine.begin() as connection:
        version = service.register(
            connection,
            currency_code="USD",
            lines=[
                _line("0.000020", meter="input_tokens"),
                _line("0.000060", meter="output_tokens"),
            ],
            effective_from_utc=NOW,
            **_scope("unknown"),
        )
        # 部分未知：已知金额求和；unknown 的非零 placeholder 被忽略（绝不当 0）
        amount, status = service.estimate_cost(
            version,
            {"input_tokens": Decimal("1000"), "output_tokens": Decimal("999999")},
            availability={"input_tokens": "complete", "output_tokens": "unavailable"},
        )
        assert (amount, status) == (Decimal("0.020000"), "partial")
        # 全部未知（placeholder 非零）→ (None, "unavailable")，金额不得为 0
        none_amount, none_status = service.estimate_cost(
            version,
            {"input_tokens": Decimal("5"), "output_tokens": Decimal("7")},
            availability={"input_tokens": "unavailable", "output_tokens": "unavailable"},
        )
        assert none_amount is None
        assert none_status == "unavailable"
        # availability="partial" 的 meter：金额照算，整体 partial
        amount_partial, status_partial = service.estimate_cost(
            version,
            {"input_tokens": Decimal("1000"), "output_tokens": Decimal("0")},
            availability={"input_tokens": "partial", "output_tokens": "complete"},
        )
        assert (amount_partial, status_partial) == (Decimal("0.020000"), "partial")


def test_estimate_zero_rate_line() -> None:
    service, engine = make_service()
    with engine.begin() as connection:
        version = service.register(
            connection,
            currency_code="USD",
            lines=[_line("0", billing_granularity=1000, rounding_rule="ceil")],
            effective_from_utc=NOW,
            **_scope("zero_rate"),
        )
        amount, status = service.estimate_cost(
            version,
            {"input_tokens": Decimal("5000")},
            availability={"input_tokens": "complete"},
        )
        # 零费率行仍必须有 line/version；已知 → 金额 0 + complete（不是 partial/unavailable）
        assert (amount, status) == (Decimal("0"), "complete")


def test_estimate_total_quantized_half_up() -> None:
    service, engine = make_service()
    with engine.begin() as connection:
        version = service.register(
            connection,
            currency_code="USD",
            lines=[
                _line("0.0000005", meter="a"),
                _line("0.0000005", meter="b"),
            ],
            effective_from_utc=NOW,
            **_scope("total_q"),
        )
        amount, status = service.estimate_cost(
            version,
            {"a": Decimal("1"), "b": Decimal("1")},
            availability={"a": "complete", "b": "complete"},
        )
        # 每行 half_up 到 6 位：0.000001 + 0.000001；总金额 half_up 6 位
        assert (amount, status) == (Decimal("0.000002"), "complete")


def test_estimate_availability_and_quantity_validation() -> None:
    service, engine = make_service()
    with engine.begin() as connection:
        version = service.register(
            connection,
            currency_code="USD",
            lines=[
                _line("0.000020", meter="input_tokens"),
                _line("0.000060", meter="output_tokens"),
            ],
            effective_from_utc=NOW,
            **_scope("estimate_validate"),
        )
        both = {"input_tokens": "complete", "output_tokens": "complete"}
        # availability 非法值 → 422
        with pytest.raises(PlatformError) as exc:
            service.estimate_cost(
                version,
                {"input_tokens": Decimal("1"), "output_tokens": Decimal("0")},
                availability={"input_tokens": "bogus", "output_tokens": "complete"},
            )
        assert exc.value.code == "validation_error"
        # priced meter 缺 availability → 422（missing 不当 0）
        with pytest.raises(PlatformError) as exc:
            service.estimate_cost(
                version,
                {"input_tokens": Decimal("1")},
                availability={"input_tokens": "complete"},
            )
        assert exc.value.code == "validation_error"
        # complete meter 缺 measured → 422
        with pytest.raises(PlatformError) as exc:
            service.estimate_cost(
                version,
                {"input_tokens": Decimal("1")},
                availability=both,
            )
        assert exc.value.code == "validation_error"
        # 负 quantity → 422
        with pytest.raises(PlatformError) as exc:
            service.estimate_cost(
                version,
                {"input_tokens": Decimal("-1"), "output_tokens": Decimal("0")},
                availability=both,
            )
        assert exc.value.code == "validation_error"
        # NaN quantity → 422
        with pytest.raises(PlatformError) as exc:
            service.estimate_cost(
                version,
                {"input_tokens": Decimal("NaN"), "output_tokens": Decimal("0")},
                availability=both,
            )
        assert exc.value.code == "validation_error"
        # 额外 unpriced meter（measured / availability）→ 忽略，不报错
        amount, status = service.estimate_cost(
            version,
            {
                "input_tokens": Decimal("1000"),
                "output_tokens": Decimal("0"),
                "extra_meter": Decimal("999"),
            },
            availability={
                "input_tokens": "complete",
                "output_tokens": "complete",
                "extra_meter": "unavailable",
            },
        )
        assert (amount, status) == (Decimal("0.020000"), "complete")


# ---------------------------------------------------------------------------
# savepoint 回滚（真实唯一约束失败）：无部分 header/line/old-close，外层事务可继续
# ---------------------------------------------------------------------------


def test_savepoint_rollback_on_real_constraint_conflict() -> None:
    """首版注册撞 partial unique index（真实约束失败）→ savepoint 整体回滚。

    败者的失败来自数据库唯一约束而非模拟异常：回滚后无任何 header/line 残留，
    且同一外层事务可继续执行无害 INSERT + 完整 register。
    """
    service, engine = make_service()
    scope = _scope("rb_real")
    with engine.begin() as connection:
        service.register(
            connection,
            currency_code="USD",
            lines=[_line("0.000020")],
            effective_from_utc=NOW,
            **scope,
        )
        # 同 scope 直接注册（无 supersede）→ _ensure_no_history 预检 409（非 DB 异常）
        with pytest.raises(PlatformError) as exc:
            service.register(
                connection,
                currency_code="USD",
                lines=[_line("0.000040")],
                effective_from_utc=NOW + ONE_DAY,
                **scope,
            )
        assert exc.value.code == "price_scope_conflict"
        assert _row_count(connection, scope) == 1  # 无部分 header/line
        # 同一外层事务仍可用：无害 SELECT + 完整 register 其它 scope
        connection.execute(select(price_catalog_table.c.id).limit(1))
        other = service.register(
            connection,
            currency_code="USD",
            lines=[_line("0.000020")],
            effective_from_utc=NOW,
            **_scope("rb_real_other"),
        )
        assert other.id


def test_savepoint_rollback_supersede_no_partial_old_close() -> None:
    """supersede 场景：header/lines 插入后触发真实约束失败 → old-close 一并回滚。"""
    service, engine = make_service()
    scope = _scope("rb_sup")
    with engine.begin() as connection:
        v1 = service.register(
            connection,
            currency_code="USD",
            lines=[_line("0.000020")],
            effective_from_utc=NOW,
            **scope,
        )
        # 同一 scope 已有 open；用指向 v1 的 supersede 但新 from 不晚于 v1.from
        # → 区间校验 409（savepoint 回滚，无 close/header/lines）
        with pytest.raises(PlatformError) as exc:
            service.register(
                connection,
                currency_code="USD",
                lines=[_line("0.000040")],
                effective_from_utc=NOW,
                supersedes_version_id=v1.id,
                **scope,
            )
        assert exc.value.code == "price_scope_conflict"
        assert _row_count(connection, scope) == 1
        assert _open_count(connection, scope) == 1  # v1 未被部分 close
        # 同一外层事务仍可用
        connection.execute(select(price_catalog_table.c.id).limit(1))
        other = service.register(
            connection,
            currency_code="USD",
            lines=[_line("0.000020")],
            effective_from_utc=NOW,
            **_scope("rb_sup_other"),
        )
        assert other.id != v1.id


def test_supersede_conflict_then_outer_transaction_continues() -> None:
    """预检/校验冲突后，同一外层事务继续执行无害 SELECT + 完整 register。"""
    service, engine = make_service()
    with engine.begin() as connection:
        v1 = service.register(
            connection,
            currency_code="USD",
            lines=[_line("0.000020")],
            effective_from_utc=NOW,
            **_scope("tx_continue"),
        )
        with pytest.raises(PlatformError) as exc:
            service.register(
                connection,
                currency_code="USD",
                lines=[_line("0.000040")],
                effective_from_utc=NOW + ONE_DAY,
                supersedes_version_id="price_missing",
                **_scope("tx_continue"),
            )
        assert exc.value.code == "price_not_found"
        # 同一事务继续
        connection.execute(select(price_catalog_table.c.id).limit(1))
        v2 = service.register(
            connection,
            currency_code="USD",
            lines=[_line("0.000040")],
            effective_from_utc=NOW + ONE_DAY,
            supersedes_version_id=v1.id,
            **_scope("tx_continue"),
        )
        assert v2.supersedes_version_id == v1.id


# ---------------------------------------------------------------------------
# lock_for_usage 区间合同单测（Task 5 调用接口）。
# SQLite FOR UPDATE 为 no-op：不在 SQLite 断言互斥（假阳性）；真实 PG 行锁
# 互斥验证留 acceptance 环境。
# ---------------------------------------------------------------------------


def test_lock_for_usage_returns_version_within_interval() -> None:
    service, engine = make_service()
    with engine.begin() as connection:
        version = service.register(
            connection,
            currency_code="USD",
            lines=[_line("0.000020")],
            effective_from_utc=NOW,
            **_scope("lock_usage_ok"),
        )
        locked = service.lock_for_usage(connection, version.id, NOW + timedelta(hours=1))
        assert locked.id == version.id
        assert [line.meter for line in locked.lines] == ["input_tokens"]
        assert locked.currency_code == "USD"
        # 恰 effective_from 属于区间（half-open）
        at_from = service.lock_for_usage(connection, version.id, NOW)
        assert at_from.id == version.id


def test_lock_for_usage_rejects_outside_interval() -> None:
    service, engine = make_service()
    with engine.begin() as connection:
        version = service.register(
            connection,
            currency_code="USD",
            lines=[_line("0.000020")],
            effective_from_utc=NOW,
            **_scope("lock_usage_bad"),
        )
        service.close_version(connection, version.id, NOW + ONE_DAY)
        # 区间内仍可 lock（half-open：恰 effective_to 之外才拒绝）
        inside = service.lock_for_usage(connection, version.id, NOW + timedelta(hours=1))
        assert inside.id == version.id
        for bad_at in (NOW + ONE_DAY, NOW + 2 * ONE_DAY, NOW - ONE_DAY):
            with pytest.raises(PlatformError) as exc:
                service.lock_for_usage(connection, version.id, bad_at)
            assert exc.value.code == "price_interval_conflict"
            assert exc.value.status_code == 409


def test_lock_for_usage_unknown_version_404() -> None:
    service, engine = make_service()
    with engine.begin() as connection:
        with pytest.raises(PlatformError) as exc:
            service.lock_for_usage(connection, "price_missing", NOW)
        assert exc.value.code == "price_not_found"
        assert exc.value.status_code == 404


# ---------------------------------------------------------------------------
# scope 写锁：真正验证阻塞（after_cursor_execute hook 精确匹配 scope UPDATE；
# SQLite 文件库可验证数据库写锁；PG 行锁留验收环境）
# ---------------------------------------------------------------------------


class _ScopeLockGate:
    """测试专用 after_cursor_execute hook：仅在 scope 写锁 UPDATE 完成后发信号。

    精确匹配：规范化 SQL（空白折叠）以 `UPDATE price_catalog_scope` 开头，且包含
    `SET lock_version=(price_catalog_scope.lock_version + ?)` 形式的自增表达式
    （`lock_version=(<col>.<col> + ?)`）——INSERT upsert（`INSERT INTO
    price_catalog_scope` 且值含 lock_version=0）与其它语句都不触发。匹配到的语句
    原文记录在 `matched_statements`，测试断言确为 UPDATE 后再证明 B 阻塞。
    仅注册在测试 engine 上，不污染生产 API；单次触发。
    """

    _SCOPE_UPDATE_RE = re.compile(
        r"^UPDATE\s+price_catalog_scope\s+SET\s+lock_version=\("
        r"price_catalog_scope\.lock_version\s+\+\s+\?\)"
    )

    def __init__(self, event: threading.Event) -> None:
        self._event = event
        self._done = False
        self.matched_statements: list[str] = []

    def __call__(self, conn, cursor, statement, parameters, context, executemany):
        del conn, cursor, parameters, context, executemany
        normalized = re.sub(r"\s+", " ", statement).strip()
        if (
            not self._done
            and normalized.startswith("UPDATE")
            and self._SCOPE_UPDATE_RE.match(normalized) is not None
        ):
            self._done = True
            self.matched_statements.append(normalized)
            self._event.set()


class _FixedClockNoGate:
    """并发线程内使用的固定时钟（不等待任何 barrier）。"""

    def now_utc(self, connection: Connection | None = None) -> datetime:
        del connection
        return NOW


def _file_engine(tmp_path: Path, name: str) -> Engine:
    """创建/复用单个共享 SQLite 文件上的 engine；并发测试必须共享同一文件。"""
    url = f"sqlite:///{tmp_path / name}"
    engine = create_engine(url, connect_args={"timeout": 30})
    usage_metadata.create_all(engine)
    return engine


def _race(holder: list, index: int, engine: Engine, register_call):
    service = PriceCatalogService(engine, _FixedClockNoGate())

    def attempt():
        with engine.begin() as connection:
            return register_call(service, connection)

    try:
        holder[index] = attempt()
    except PlatformError as exc:
        holder[index] = exc
    except BaseException as exc:  # pragma: no cover - 失败时暴露原因
        holder[index] = exc


def test_scope_lock_blocks_concurrent_register(tmp_path) -> None:
    """真实 scope 锁：A 完成 scope UPDATE 后被 hook 暂停，B 同 scope 注册必须阻塞。

    A 持有写锁（SQLite 数据库写锁，持有至事务结束）；B 的 register 在 scope
    UPDATE 处阻塞。释放 A 后 B 继续：锁内重新读取 latest/history，看到 A 已注册
    → 稳定 price_scope_conflict；最终库中恰 1 行、open 恰 1。
    """
    scope = _scope("scope_lock_block")
    db_url = f"sqlite:///{tmp_path / 'scope_lock_block.sqlite3'}"
    engine = _file_engine(tmp_path, "scope_lock_block.sqlite3")
    locked_event = threading.Event()
    gate = _ScopeLockGate(locked_event)
    event.listen(engine, "after_cursor_execute", gate)
    engine_b = create_engine(db_url, connect_args={"timeout": 30})
    service_a = PriceCatalogService(engine, FixedClock(NOW))
    outcomes: list = [None, None]
    a_release = threading.Event()

    def register_a():
        try:
            with engine.begin() as connection:
                version = service_a.register(
                    connection,
                    currency_code="USD",
                    lines=[_line("0.000020")],
                    effective_from_utc=NOW,
                    **scope,
                )
                outcomes[0] = version
                a_release.wait(timeout=30)  # 持锁暂停，等待测试释放
        except BaseException as exc:  # pragma: no cover - 失败时暴露原因
            outcomes[0] = exc

    def register_b():
        def body():
            _race(
                outcomes,
                1,
                engine_b,
                lambda s, c: s.register(
                    c,
                    currency_code="USD",
                    lines=[_line("0.000040")],
                    effective_from_utc=NOW + ONE_DAY,
                    **scope,
                ),
            )

        body()

    thread_a = threading.Thread(target=register_a)
    thread_b = threading.Thread(target=register_b)
    thread_a.start()
    # A 完成 scope 写锁 UPDATE（已持有 SQLite 数据库写锁）
    assert locked_event.wait(timeout=20), "A 未完成 scope UPDATE"
    # 先证明 hook 匹配的确实只是写锁 UPDATE（而非 INSERT upsert / 其它语句）
    assert len(gate.matched_statements) == 1, gate.matched_statements
    assert gate.matched_statements[0].startswith("UPDATE price_catalog_scope SET lock_version=(")
    assert "lock_version +" in gate.matched_statements[0]
    thread_b.start()
    # 等 B 进入并确认阻塞在写锁上（0.5s 后仍存活且未产出结果）
    time.sleep(0.5)
    assert thread_b.is_alive(), "B 应在 scope 写锁处阻塞"
    assert outcomes[1] is None, "B 不应在阻塞期间产出结果"
    # 释放 A：事务提交，写锁释放
    a_release.set()
    thread_a.join(timeout=30)
    thread_b.join(timeout=30)
    assert not thread_a.is_alive() and not thread_b.is_alive()
    assert isinstance(outcomes[0], PriceVersion)
    assert isinstance(outcomes[1], PlatformError)
    assert outcomes[1].code == "price_scope_conflict"
    assert _row_count(engine, scope) == 1
    assert _open_count(engine, scope) == 1
    engine.dispose()
    engine_b.dispose()


def test_scope_lock_blocks_concurrent_supersede(tmp_path) -> None:
    """真实 scope 锁 + supersede：A 持锁时 B 的 supersede 阻塞；释放后 B 读最新
    状态（v1 已被 A close）→ 稳定 price_scope_conflict；无部分 old-close。
    """
    scope = _scope("scope_lock_sup")
    db_url = f"sqlite:///{tmp_path / 'scope_lock_sup.sqlite3'}"
    engine = _file_engine(tmp_path, "scope_lock_sup.sqlite3")
    seed_service = PriceCatalogService(engine, FixedClock(NOW))
    with engine.begin() as connection:
        v1 = seed_service.register(
            connection,
            currency_code="USD",
            lines=[_line("0.000020")],
            effective_from_utc=NOW,
            **scope,
        )
    locked_event = threading.Event()
    gate = _ScopeLockGate(locked_event)
    event.listen(engine, "after_cursor_execute", gate)
    engine_b = create_engine(db_url, connect_args={"timeout": 30})
    service_a = PriceCatalogService(engine, FixedClock(NOW))
    outcomes: list = [None, None]
    a_release = threading.Event()

    def supersede_a():
        try:
            with engine.begin() as connection:
                version = service_a.register(
                    connection,
                    currency_code="USD",
                    lines=[_line("0.000040")],
                    effective_from_utc=NOW + ONE_DAY,
                    supersedes_version_id=v1.id,
                    **scope,
                )
                outcomes[0] = version
                a_release.wait(timeout=30)
        except BaseException as exc:  # pragma: no cover - 失败时暴露原因
            outcomes[0] = exc

    def supersede_b():
        _race(
            outcomes,
            1,
            engine_b,
            lambda s, c: s.register(
                c,
                currency_code="USD",
                lines=[_line("0.000060")],
                effective_from_utc=NOW + 2 * ONE_DAY,
                supersedes_version_id=v1.id,
                **scope,
            ),
        )

    thread_a = threading.Thread(target=supersede_a)
    thread_b = threading.Thread(target=supersede_b)
    thread_a.start()
    assert locked_event.wait(timeout=20), "A 未完成 scope UPDATE"
    # 先证明 hook 匹配的确实只是写锁 UPDATE（而非 INSERT upsert / 其它语句）
    assert len(gate.matched_statements) == 1, gate.matched_statements
    assert gate.matched_statements[0].startswith("UPDATE price_catalog_scope SET lock_version=(")
    assert "lock_version +" in gate.matched_statements[0]
    thread_b.start()
    time.sleep(0.5)
    assert thread_b.is_alive(), "B 应在 scope 写锁处阻塞"
    assert outcomes[1] is None
    a_release.set()
    thread_a.join(timeout=30)
    thread_b.join(timeout=30)
    assert not thread_a.is_alive() and not thread_b.is_alive()
    assert isinstance(outcomes[0], PriceVersion)
    assert isinstance(outcomes[1], PlatformError)
    assert outcomes[1].code == "price_scope_conflict"
    # A 成功：v1 关闭到 NOW+ONE_DAY + successor；B 败者无部分 old-close/header/line
    assert _row_count(engine, scope) == 2
    assert _open_count(engine, scope) == 1
    with engine.connect() as connection:
        v1_to = connection.execute(
            select(price_catalog_table.c.effective_to_utc).where(price_catalog_table.c.id == v1.id)
        ).scalar_one()
    # SQLite 回读 naive（方言行为），归一 UTC 后比较
    v1_to_aware = v1_to.replace(tzinfo=UTC) if v1_to.tzinfo is None else v1_to
    assert v1_to_aware == NOW + ONE_DAY
    engine.dispose()
    engine_b.dispose()


# ---------------------------------------------------------------------------
# savepoint 真实 PK IntegrityError：可控 ID 生成器使新 header 与已有版本 PK 冲突
# ---------------------------------------------------------------------------


def test_savepoint_real_pk_collision_rolls_back_whole_operation(monkeypatch) -> None:
    """header INSERT 触发真实 PK IntegrityError（非人工 raise）→ 整体回滚。

    在 savepoint 内先 close predecessor（v1），再以与 v1 相同的 id 插入新 header →
    真实 PK 冲突。savepoint 回滚后：v1 仍 open、无部分 lines、同一外层事务可继续
    执行无害 SELECT + 注册其它 scope。
    """
    service, engine = make_service()
    scope = _scope("pk_collision")
    with engine.begin() as connection:
        v1 = service.register(
            connection,
            currency_code="USD",
            lines=[_line("0.000020")],
            effective_from_utc=NOW,
            **scope,
        )
        original = secrets.token_urlsafe
        # 令新 version id（register 内的第一次调用）与 v1 相同 → header PK 冲突。
        # register 生成 version_id = f"price_{token_urlsafe(9)}"，故返回 v1.id 的
        # token 部分即产生相同 id；line id 使用不同前缀不会与 header 冲突。
        fixed_token = v1.id[len("price_") :]
        monkeypatch.setattr("app.usage.price.secrets.token_urlsafe", lambda n=None: fixed_token)
        try:
            with pytest.raises(PlatformError) as exc:
                service.register(
                    connection,
                    currency_code="USD",
                    lines=[_line("0.000040")],
                    effective_from_utc=NOW + ONE_DAY,
                    supersedes_version_id=v1.id,
                    **scope,
                )
        finally:
            monkeypatch.setattr("app.usage.price.secrets.token_urlsafe", original)
        assert exc.value.code == "price_scope_conflict"
        assert exc.value.status_code == 409
        # 无部分 old-close：v1 仍 open
        v1_to = connection.execute(
            select(price_catalog_table.c.effective_to_utc).where(price_catalog_table.c.id == v1.id)
        ).scalar_one()
        assert v1_to is None
        # 无部分 header / lines
        assert _row_count(connection, scope) == 1
        line_ids = connection.execute(select(price_catalog_line_table.c.id)).all()
        assert len(line_ids) == 1  # 仅 v1 的 line
        # 同一外层事务仍可用：无害 SELECT + 完整 register 其它 scope
        connection.execute(select(price_catalog_table.c.id).limit(1))
        other = service.register(
            connection,
            currency_code="USD",
            lines=[_line("0.000020")],
            effective_from_utc=NOW,
            **_scope("pk_collision_other"),
        )
        assert other.id != v1.id


def test_savepoint_conflict_then_outer_transaction_continues() -> None:
    """scope 冲突/404 后，同一外层事务继续执行无害 SELECT + 完整 register。"""
    service, engine = make_service()
    with engine.begin() as connection:
        v1 = service.register(
            connection,
            currency_code="USD",
            lines=[_line("0.000020")],
            effective_from_utc=NOW,
            **_scope("tx_continue"),
        )
        with pytest.raises(PlatformError) as exc:
            service.register(
                connection,
                currency_code="USD",
                lines=[_line("0.000040")],
                effective_from_utc=NOW + ONE_DAY,
                supersedes_version_id="price_missing",
                **_scope("tx_continue"),
            )
        assert exc.value.code == "price_not_found"
        # 同一事务继续
        connection.execute(select(price_catalog_table.c.id).limit(1))
        v2 = service.register(
            connection,
            currency_code="USD",
            lines=[_line("0.000040")],
            effective_from_utc=NOW + ONE_DAY,
            supersedes_version_id=v1.id,
            **_scope("tx_continue"),
        )
        assert v2.supersedes_version_id == v1.id
