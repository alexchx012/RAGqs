"""Task 8：直接入库余额门禁审计钉死 + publication debit 端口（check_direct_ingest_balance /
record_publication_debit）。

语义（正式 spec §2/§3 + Task 8 约束；旧 brief 示例的 SELECT `.rowcount` 陷阱已审计修正：
SQLAlchemy 的 CursorResult.rowcount 对 SELECT 不可靠（多数驱动返回 -1），所有行数断言
一律 `.all()` 后 `len()` 比较内容）：
- check_direct_ingest_balance 只做当前余额检查：user/minister 用 used+pages 与
  effective_limit 比较，超限固定 409 quota_exceeded；不预留、不创建 quota ledger/
  projection 行、不改余额；ops/admin unlimited 直接放行。
- 门禁边界：used+pages == effective_limit 通过、> 拒绝；pages 0/负数/bool、subject 空
  稳定 422 validation_error；未知/空 role 按有限角色判定（fail-safe，不崩溃）。
- record_publication_debit 接收显式 publication 业务终态、调用方 connection 与冻结的
  calendar_lock/published_at；只有 succeeded 委托 append_debit
  （effective_at_utc=published_at），failed/cancelled/dead_letter fail-closed 返回 None 且
  保留既有 usage。同 quota_operation_id 幂等复用且不二次投影；shared_library_submission、
  ops/admin、replay_generation>0 豁免返回 None 且不写账本；
  同 operation 异事实 409 ledger_invariant_conflict。quota_exempt_reason 仅可为 None
  或精确 shared_library_submission：未知值/空串/非字符串在豁免早退前稳定 422（ops/admin/
  replay 不绕过输入错误）。
- 强入口类型校验：ownership/calendar_lock/published_at 非法类型稳定 422
  validation_error（不泄漏 AttributeError/TypeError）。
- published_at 决定 effective period（跨业务月整体归期不拆分），recorded 用 DB now
  （clock）。
- caller transaction rollback：debit 与投影属于调用方事务，回滚后与业务结果一起消失
  （服务不自行开事务）。
- 端口结构化兼容：QuotaService 的 check/record 与 DirectIngestGatePort/
  PublicationDebitPort 使用同一组精确领域类型（mypy assert_type 静态验证 + 运行时调用）；
  mypy 负例 fixture 真实检查错误类型（# type: ignore[arg-type] 仅在预期报错处）。
"""

from __future__ import annotations

import asyncio
import typing
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import NotRequired, TypedDict, assert_type

import pytest
from sqlalchemy import Engine, create_engine, select
from sqlalchemy.engine import Connection
from sqlalchemy.pool import StaticPool

from app.platform.database import SqlAlchemyDatabaseClock
from app.platform.errors import PlatformError
from app.platform.provider import (
    CircuitBreakerRegistry,
    ProviderCallContext,
    ProviderFailure,
    ProviderResult,
    RetryPolicy,
)
from app.usage.calendar import BusinessCalendarService, CalendarLock
from app.usage.ledger import (
    LocalMeasurement,
    OwnershipSnapshot,
    ProviderMeasurement,
    UsageLedger,
)
from app.usage.ports import (
    DirectIngestGatePort,
    PublicationDebitPort,
    PublicationTerminalStatus,
)
from app.usage.price import PriceCatalogService
from app.usage.provider_integration import UsageLedgerLifecycle, run_provider_call_with_usage
from app.usage.quota import QuotaService
from app.usage.schema import (
    provider_call_table,
    quota_debit_table,
    quota_projection_table,
    usage_event_table,
    usage_metadata,
)

NOW = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)
SEPT = datetime(2026, 9, 3, 8, 0, tzinfo=UTC)


class DebitCase(TypedDict):
    role: str
    quota_exempt_reason: NotRequired[str]
    replay_generation: NotRequired[int]


@dataclass(frozen=True, slots=True)
class PublicationProviderCase:
    name: str
    behavior: str
    provider_status: str
    usage_result: str | None
    policy_state: str | None
    policy_error_class: str | None


@dataclass(frozen=True, slots=True)
class FixedClock:
    now: datetime

    def now_utc(self, connection: Connection | None = None) -> datetime:
        del connection
        return self.now


def make_quota() -> tuple[Engine, QuotaService]:
    """返回类型注解必须显式：否则 mypy 推断 quota: Any，负例 fixture 的
    `# type: ignore[arg-type]` 会变成 unused-ignore，类型负例失效。"""
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    usage_metadata.create_all(engine)
    clock = FixedClock(NOW)
    calendar = BusinessCalendarService(engine, SqlAlchemyDatabaseClock(engine), "Asia/Shanghai")
    return engine, QuotaService(engine, clock, calendar)


def make_policy_services() -> tuple[Engine, UsageLedger, QuotaService]:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    usage_metadata.create_all(engine)
    clock = FixedClock(NOW)
    calendar = BusinessCalendarService(engine, SqlAlchemyDatabaseClock(engine), "Asia/Shanghai")
    prices = PriceCatalogService(engine, clock)
    return (
        engine,
        UsageLedger(engine, clock, calendar, prices),
        QuotaService(
            engine,
            clock,
            calendar,
        ),
    )


def ownership() -> OwnershipSnapshot:
    return OwnershipSnapshot(
        actor_user_id="u1",
        actor_role_snapshot="user",
        actor_department_id_snapshot=None,
        quota_subject_user_id="u1",
        cost_center_key="user:u1",
    )


def debit_rows(connection) -> list[dict]:
    return [
        dict(row)
        for row in connection.execute(
            select(quota_debit_table).order_by(
                quota_debit_table.c.created_at_utc, quota_debit_table.c.quota_debit_id
            )
        ).mappings()
    ]


def projection_rows(connection) -> list[dict]:
    return [
        dict(row)
        for row in connection.execute(
            select(quota_projection_table).order_by(quota_projection_table.c.quota_period)
        ).mappings()
    ]


def usage_rows(connection) -> list[dict]:
    return [
        dict(row)
        for row in connection.execute(
            select(usage_event_table).order_by(usage_event_table.c.created_at_utc)
        ).mappings()
    ]


def provider_call_rows(connection) -> list[dict]:
    return [dict(row) for row in connection.execute(select(provider_call_table)).mappings()]


def record_actual_usage(
    ledger: UsageLedger,
    *,
    execution_kind: str,
    execution_id: str,
    result: str,
) -> str:
    return ledger.submit_local_usage(
        execution_kind=execution_kind,
        execution_id=execution_id,
        stage="actual_work",
        resource_kind="cpu",
        measurement=LocalMeasurement(
            page_count=3,
            input_bytes=1024,
            item_count=1,
            gpu_milliseconds=None,
            cpu_milliseconds=250,
            peak_vram_bytes=None,
        ),
        ownership=ownership(),
        result=result,
        started_at_utc=NOW,
    )


def seed_provider_price(engine: Engine, ledger: UsageLedger) -> None:
    with engine.begin() as connection:
        ledger.calendar.lock_or_verify(connection)
        ledger.prices.register(
            connection,
            provider="test-provider",
            model="m",
            operation="generate",
            currency_code="USD",
            lines=[
                {"meter": "input_tokens", "unit": "token", "rate": Decimal("0.000020")},
                {"meter": "output_tokens", "unit": "token", "rate": Decimal("0.000060")},
            ],
            effective_from_utc=NOW,
        )


def provider_measurement() -> ProviderMeasurement:
    return ProviderMeasurement(
        input_tokens=100,
        output_tokens=50,
        prompt_cache_hit_tokens=None,
        prompt_cache_miss_tokens=None,
        reasoning_tokens=None,
        image_count=None,
        visual_input_tokens=None,
        embedding_input_tokens=None,
        vector_count=None,
        measurement_sources={
            "input_tokens": "provider_reported",
            "output_tokens": "provider_reported",
        },
    )


def run_publication_provider_call(
    ledger: UsageLedger,
    *,
    execution_kind: str,
    execution_id: str,
    provider_call_root_id: str,
    behavior: str,
    operation_calls: list[str] | None = None,
    request_fingerprint: str | None = None,
) -> ProviderResult:
    """Run one real publication provider lifecycle for the quota boundary tests.

    There is no publication orchestrator in ``app/``. This helper therefore stops at
    the real provider-integration/lifecycle boundary and deliberately leaves the quota
    debit decision to the separate port-contract tests below.
    """

    def operation(context: ProviderCallContext, request: object) -> str:
        del request
        if operation_calls is not None:
            operation_calls.append(context.provider_call_id)
        if behavior == "succeeded":
            return "published"
        if behavior == "known_failure":
            raise ProviderFailure(
                "upstream_503",
                status_code=503,
                retryable=False,
                sent=True,
            )
        if behavior == "unknown":
            raise ProviderFailure("timeout", retryable=False, sent=True)
        if behavior == "cancelled":
            raise asyncio.CancelledError(f"publication {execution_id} cancelled")
        raise AssertionError(f"unsupported provider behavior: {behavior}")

    return run_provider_call_with_usage(
        operation=operation,
        context=ProviderCallContext(
            provider="test-provider",
            operation="generate",
            provider_call_id=provider_call_root_id,
            attempt_id=f"attempt-{execution_id}",
            deadline_utc=NOW + timedelta(minutes=5),
            resource_id=f"resource-{execution_id}",
        ),
        model="m",
        lifecycle=UsageLedgerLifecycle(ledger),
        measurement_extractor=lambda _value, _context, _failure: provider_measurement(),
        ownership_provider=lambda _context: ownership(),
        execution_kind=execution_kind,
        execution_id=execution_id,
        request_fingerprint=(
            request_fingerprint if request_fingerprint is not None else f"fp-{execution_id}"
        ),
        policy=RetryPolicy(synchronous_attempts=1),
        circuits=CircuitBreakerRegistry(),
        now=lambda: NOW,
        sleep=lambda _delay: None,
        jitter=lambda _delay: 0,
    )


def fill_quota(engine, quota, pages: int = 500) -> None:
    with engine.begin() as connection:
        lock = quota.calendar.lock_or_verify(connection)
        quota.append_debit(
            connection,
            quota_operation_id="job_full",
            publication_id="pub_full",
            quota_subject_user_id="u1",
            pages=pages,
            ownership=ownership(),
            calendar_lock=lock,
            role="user",
            effective_at_utc=NOW,
        )


def test_gate_rejects_when_insufficient_without_writing() -> None:
    """余额不足 409 quota_exceeded；不预留、不写 ledger/projection、不改余额。"""
    engine, quota = make_quota()
    fill_quota(engine, quota, 500)
    with engine.begin() as connection:
        before_debit = len(debit_rows(connection))
        before_projection = len(projection_rows(connection))
        with pytest.raises(PlatformError) as exc:
            quota.check_direct_ingest_balance(
                connection, quota_subject_user_id="u1", pages=1, role="user"
            )
        after_debit = len(debit_rows(connection))
        after_projection = len(projection_rows(connection))
        snapshot = quota.read_snapshot(connection, quota_subject_user_id="u1", role="user")
    assert exc.value.code == "quota_exceeded"
    assert exc.value.status_code == 409
    assert before_debit == after_debit  # 不新增账本行（不用 rowcount：SELECT rowcount 不可靠）
    assert before_projection == after_projection  # 不新增投影行
    assert snapshot.used == 500  # 余额未被改写
    assert snapshot.effective_limit == 500


def test_gate_passes_for_unlimited_and_at_or_below_limit() -> None:
    """ops/admin unlimited 放行；user 恰等于/低于 effective_limit 通过。"""
    engine, quota = make_quota()
    fill_quota(engine, quota, 300)
    with engine.begin() as connection:
        quota.check_direct_ingest_balance(
            connection, quota_subject_user_id="u1", pages=10**6, role="ops"
        )
        quota.check_direct_ingest_balance(
            connection, quota_subject_user_id="u1", pages=10**6, role="admin"
        )
        quota.check_direct_ingest_balance(
            connection, quota_subject_user_id="u1", pages=200, role="user"
        )
        # 门禁不改写任何行
        rows = debit_rows(connection)
        assert len(rows) == 1  # 仅 fill 的 debit
        proj = projection_rows(connection)
        assert len(proj) == 1
        assert proj[0]["used"] == 300


def test_gate_rejects_exactly_over_limit_and_minister_is_gated() -> None:
    """边界：used+pages == effective_limit 通过、> 拒绝；minister 与 user 同门禁。"""
    engine, quota = make_quota()
    fill_quota(engine, quota, 300)
    with engine.begin() as connection:
        quota.check_direct_ingest_balance(
            connection, quota_subject_user_id="u1", pages=200, role="minister"
        )
        with pytest.raises(PlatformError) as exc:
            quota.check_direct_ingest_balance(
                connection, quota_subject_user_id="u1", pages=201, role="user"
            )
        assert exc.value.code == "quota_exceeded"
        with pytest.raises(PlatformError) as exc:
            quota.check_direct_ingest_balance(
                connection, quota_subject_user_id="u1", pages=201, role="minister"
            )
        assert exc.value.code == "quota_exceeded"
        # 未知非空 role：未超限放行（fail-safe 不崩溃），超限必须 409（review Task8 #3）
        quota.check_direct_ingest_balance(
            connection, quota_subject_user_id="u1", pages=200, role="unknown_role"
        )
        with pytest.raises(PlatformError) as exc:
            quota.check_direct_ingest_balance(
                connection, quota_subject_user_id="u1", pages=201, role="unknown_role"
            )
        assert exc.value.code == "quota_exceeded"
        assert exc.value.status_code == 409
        with pytest.raises(PlatformError) as exc:
            quota.check_direct_ingest_balance(
                connection, quota_subject_user_id="u1", pages=201, role=""
            )
        assert exc.value.code == "quota_exceeded"


def test_gate_input_boundaries_stable() -> None:
    """pages 0/负数/bool、subject 空 → 稳定 422；校验失败零写入。"""
    engine, quota = make_quota()
    with engine.begin() as connection:
        quota.calendar.lock_or_verify(connection)
        for bad_pages in (0, -1, True, False, 2_147_483_648):
            with pytest.raises(PlatformError) as exc:
                quota.check_direct_ingest_balance(
                    connection, quota_subject_user_id="u1", pages=bad_pages, role="user"
                )
            assert exc.value.code == "validation_error"
            assert exc.value.status_code == 422
        with pytest.raises(PlatformError) as exc:
            quota.check_direct_ingest_balance(
                connection, quota_subject_user_id="", pages=1, role="user"
            )
        assert exc.value.code == "validation_error"
        assert debit_rows(connection) == []
        assert projection_rows(connection) == []


def test_publication_debit_charged_once_after_success() -> None:
    """成功 publication 扣一次；同 operation 幂等复用且不二次投影。"""
    engine, quota = make_quota()
    with engine.begin() as connection:
        lock = quota.calendar.lock_or_verify(connection)
        debit_id = quota.record_publication_debit(
            connection,
            publication_status="succeeded",
            quota_operation_id="job_1",
            publication_id="pub_1",
            quota_subject_user_id="u1",
            pages=120,
            ownership=ownership(),
            calendar_lock=lock,
            role="user",
            published_at=NOW,
        )
        replay = quota.record_publication_debit(
            connection,
            publication_status="succeeded",
            quota_operation_id="job_1",
            publication_id="pub_1",
            quota_subject_user_id="u1",
            pages=120,
            ownership=ownership(),
            calendar_lock=lock,
            role="user",
            published_at=NOW,
        )
        assert debit_id is not None
        assert replay == debit_id
        rows = debit_rows(connection)
        assert len(rows) == 1  # 幂等重放不新增行
        proj = projection_rows(connection)
        assert len(proj) == 1
        assert proj[0]["used"] == 120  # 不二次投影
        assert proj[0]["last_debit_id"] == debit_id


def test_publication_debit_replay_succeeds_after_quota_is_full() -> None:
    """已持久化 debit 的同事实重放优先于新的容量校验。"""
    engine, quota = make_quota()
    with engine.begin() as connection:
        lock = quota.calendar.lock_or_verify(connection)
        first_debit_id = quota.record_publication_debit(
            connection,
            publication_status="succeeded",
            quota_operation_id="job_1",
            publication_id="pub_1",
            quota_subject_user_id="u1",
            pages=300,
            ownership=ownership(),
            calendar_lock=lock,
            role="user",
            published_at=NOW,
        )
        second_debit_id = quota.record_publication_debit(
            connection,
            publication_status="succeeded",
            quota_operation_id="job_2",
            publication_id="pub_2",
            quota_subject_user_id="u1",
            pages=200,
            ownership=ownership(),
            calendar_lock=lock,
            role="user",
            published_at=NOW,
        )
        replayed_debit_id = quota.record_publication_debit(
            connection,
            publication_status="succeeded",
            quota_operation_id="job_1",
            publication_id="pub_1",
            quota_subject_user_id="u1",
            pages=300,
            ownership=ownership(),
            calendar_lock=lock,
            role="user",
            published_at=NOW,
        )
        snapshot = quota.read_snapshot(connection, quota_subject_user_id="u1", role="user")

    assert first_debit_id is not None
    assert second_debit_id is not None
    assert replayed_debit_id == first_debit_id
    assert snapshot.used == 500
    with engine.connect() as connection:
        assert len(debit_rows(connection)) == 2


def test_successful_publication_can_exceed_latest_balance_without_losing_usage() -> None:
    """最终 publication 允许越限；已提交的实际 usage 不随 debit 回滚。"""
    engine, ledger, quota = make_policy_services()
    seed_provider_price(engine, ledger)
    provider_result = run_publication_provider_call(
        ledger,
        execution_kind="initial",
        execution_id="job-over-limit",
        provider_call_root_id="pc-over-limit",
        behavior="succeeded",
    )
    assert provider_result.state == "succeeded"
    fill_quota(engine, quota, 500)

    with engine.begin() as connection:
        lock = quota.calendar.lock_or_verify(connection)
        debit_id = quota.record_publication_debit(
            connection,
            publication_status="succeeded",
            quota_operation_id="job-over-limit",
            publication_id="pub-over-limit",
            quota_subject_user_id="u1",
            pages=1,
            ownership=ownership(),
            calendar_lock=lock,
            role="user",
            published_at=NOW,
        )

    assert debit_id is not None
    with engine.connect() as connection:
        provider_usage = [
            row for row in usage_rows(connection) if row["event_kind"] == "provider_usage"
        ]
        snapshot = quota.read_snapshot(connection, quota_subject_user_id="u1", role="user")
    assert len(provider_usage) == 1
    assert snapshot.used == 501

    with engine.begin() as connection:
        with pytest.raises(PlatformError) as rejected:
            quota.check_direct_ingest_balance(
                connection, quota_subject_user_id="u1", pages=1, role="user"
            )
    assert rejected.value.code == "quota_exceeded"


def test_publication_debit_exempt_unlimited_replay() -> None:
    """shared_library_submission / ops / admin / replay_generation>0 → None 且不写账本。"""
    engine, quota = make_quota()
    with engine.begin() as connection:
        lock = quota.calendar.lock_or_verify(connection)
        cases: list[DebitCase] = [
            {"quota_exempt_reason": "shared_library_submission", "role": "user"},
            {"role": "ops"},
            {"role": "admin"},
            {"replay_generation": 1, "role": "user"},
        ]
        for i, case in enumerate(cases):
            result = quota.record_publication_debit(
                connection,
                publication_status="succeeded",
                quota_operation_id=f"job_e{i}",
                publication_id="pub_e",
                quota_subject_user_id="u1",
                pages=100,
                ownership=ownership(),
                calendar_lock=lock,
                role=case["role"],
                quota_exempt_reason=case.get("quota_exempt_reason"),
                replay_generation=case.get("replay_generation", 0),
                published_at=NOW,
            )
            assert result is None
        assert debit_rows(connection) == []
        assert projection_rows(connection) == []  # 豁免不建投影


@pytest.mark.parametrize(
    "bad_status",
    ["published", "unknown", " succeeded", "SUCCEEDED", "", None, 1],
)
def test_publication_debit_rejects_invalid_status_before_exemptions(
    bad_status: object,
) -> None:
    engine, quota = make_quota()
    debit_port: PublicationDebitPort = quota
    with engine.begin() as connection:
        lock = quota.calendar.lock_or_verify(connection)
        with pytest.raises(PlatformError) as exc:
            debit_port.record(
                connection,
                publication_status=bad_status,  # type: ignore[arg-type]
                quota_operation_id="job-invalid-status",
                publication_id="pub-invalid-status",
                quota_subject_user_id="u1",
                pages=100,
                ownership=ownership(),
                calendar_lock=lock,
                role="ops",
                quota_exempt_reason="shared_library_submission",
                replay_generation=1,
                published_at=NOW,
            )
        assert exc.value.code == "validation_error"
        assert exc.value.status_code == 422
        assert debit_rows(connection) == []
        assert projection_rows(connection) == []


def test_publication_debit_exempt_reason_must_be_exact() -> None:
    """quota_exempt_reason 仅 None 或精确 shared_library_submission（review Task8 #1）。

    未知值/空串/非字符串在豁免早退前稳定 422 validation_error，且零写入（不因
    ops/admin/replay_generation 绕过输入错误）。
    """
    engine, quota = make_quota()
    with engine.begin() as connection:
        lock = quota.calendar.lock_or_verify(connection)
        cases = [
            # (reason, role, replay)
            ("unknown_reason", "user", 0),
            ("", "user", 0),
            ("SHARED_LIBRARY_SUBMISSION", "user", 0),  # 大小写不精确也拒绝
            ("shared_library_submission ", "user", 0),  # 带尾随空格不精确
            (None, "user", 0),  # None 合法 → 走正常 debit（下方单独断言）
            ("unknown_reason", "ops", 1),  # ops/replay 不得绕过输入错误
            ("", "ops", 0),
            ("unknown_reason", "admin", 0),
        ]
        for reason, role, replay in cases:
            if reason is None:
                continue
            with pytest.raises(PlatformError) as exc:
                quota.record_publication_debit(
                    connection,
                    publication_status="succeeded",
                    quota_operation_id=f"job_bad_{role}_{replay}",
                    publication_id="pub_bad",
                    quota_subject_user_id="u1",
                    pages=100,
                    ownership=ownership(),
                    calendar_lock=lock,
                    role=role,
                    quota_exempt_reason=reason,
                    replay_generation=replay,
                    published_at=NOW,
                )
            assert exc.value.code == "validation_error"
            assert exc.value.status_code == 422
        # 非字符串（int/bool/list）同样 422（mypy 层面非法，运行时仍稳定拒绝）
        for bad_reason in (123, True, ["shared_library_submission"]):
            with pytest.raises(PlatformError) as exc:
                quota.record_publication_debit(
                    connection,
                    publication_status="succeeded",
                    quota_operation_id="job_bad_type",
                    publication_id="pub_bad",
                    quota_subject_user_id="u1",
                    pages=100,
                    ownership=ownership(),
                    calendar_lock=lock,
                    role="user",
                    quota_exempt_reason=bad_reason,  # type: ignore[arg-type]
                    published_at=NOW,
                )
            assert exc.value.code == "validation_error"
        # 零写入：全部失败路径无账本/投影行
        assert debit_rows(connection) == []
        assert projection_rows(connection) == []
        # None 合法 → 正常扣（对照）
        debit_id = quota.record_publication_debit(
            connection,
            publication_status="succeeded",
            quota_operation_id="job_ok",
            publication_id="pub_ok",
            quota_subject_user_id="u1",
            pages=100,
            ownership=ownership(),
            calendar_lock=lock,
            role="user",
            quota_exempt_reason=None,
            published_at=NOW,
        )
        assert debit_id is not None
        assert len(debit_rows(connection)) == 1


def test_exemption_still_validates_all_inputs_before_early_return() -> None:
    """豁免早退前完成全部纯输入校验（review Task8 第二轮 #1）。

    shared-library / ops / admin / replay 豁免搭配非法输入 → 稳定 422，不因豁免放行；
    合法豁免参数仍 None 且零 SQL/零写入（账本与投影均为空，未取 clock 未写行）。
    """
    engine, quota = make_quota()
    with engine.begin() as connection:
        lock = quota.calendar.lock_or_verify(connection)

        def valid_call(exempt: str | None, role: str, replay: int) -> None:
            result = quota.record_publication_debit(
                connection,
                publication_status="succeeded",
                quota_operation_id=f"job_ex_{exempt or role}_{replay}",
                publication_id="pub_ex",
                quota_subject_user_id="u1",
                pages=100,
                ownership=ownership(),
                calendar_lock=lock,
                role=role,
                quota_exempt_reason=exempt,
                replay_generation=replay,
                published_at=NOW,
            )
            assert result is None  # 合法豁免 → None

        def record_bad(
            *,
            quota_subject_user_id: str | None = None,
            pages: int | None = None,
            ownership_override: OwnershipSnapshot | None = None,
            role: str | None = None,
            quota_exempt_reason: str | None = None,
            replay_generation: int | None = None,
        ) -> None:
            """直接构造调用（无闭包捕获循环变量、无 **kwargs），非法输入必须稳定 422。

            None 为哨兵表示用合法默认值（quota_subject_user_id="" / pages=0 等非法值
            显式传入仍生效）。
            """
            with pytest.raises(PlatformError) as exc:
                quota.record_publication_debit(
                    connection,
                    publication_status="succeeded",
                    quota_operation_id="job_x",
                    publication_id="pub_x",
                    quota_subject_user_id=(
                        "u1" if quota_subject_user_id is None else quota_subject_user_id
                    ),
                    pages=100 if pages is None else pages,
                    ownership=ownership() if ownership_override is None else ownership_override,
                    calendar_lock=lock,
                    role="user" if role is None else role,
                    quota_exempt_reason=quota_exempt_reason,
                    replay_generation=0 if replay_generation is None else replay_generation,
                    published_at=NOW,
                )
            assert exc.value.code == "validation_error"
            assert exc.value.status_code == 422

        cases = [
            # (豁免组合: exempt, role, replay)
            ("shared_library_submission", "user", 0),
            (None, "ops", 0),
            (None, "admin", 0),
            (None, "user", 1),
        ]
        for exempt, role, replay in cases:
            bad_subject_ownership = OwnershipSnapshot(
                actor_user_id="u1",
                actor_role_snapshot="user",
                actor_department_id_snapshot=None,
                quota_subject_user_id="other",
                cost_center_key="user:u1",
            )
            bad_fields_ownership = OwnershipSnapshot(
                actor_user_id="u1",
                actor_role_snapshot="user",
                actor_department_id_snapshot=None,
                quota_subject_user_id="u1",
                cost_center_key="",
            )
            # pages=0
            record_bad(pages=0, role=role, quota_exempt_reason=exempt, replay_generation=replay)
            # subject 空
            record_bad(
                quota_subject_user_id="",
                role=role,
                quota_exempt_reason=exempt,
                replay_generation=replay,
            )
            # ownership 字段非法（cost_center_key 空）
            record_bad(
                ownership_override=bad_fields_ownership,
                role=role,
                quota_exempt_reason=exempt,
                replay_generation=replay,
            )
            # ownership subject 与显式 subject 不一致
            record_bad(
                ownership_override=bad_subject_ownership,
                role=role,
                quota_exempt_reason=exempt,
                replay_generation=replay,
            )
        # 合法豁免参数仍 None 且零写入
        valid_call("shared_library_submission", "user", 0)
        valid_call(None, "ops", 0)
        valid_call(None, "admin", 0)
        valid_call(None, "user", 1)
        assert debit_rows(connection) == []
        assert projection_rows(connection) == []


@pytest.mark.parametrize(
    "bad_role",
    [
        " ops",
        "ops ",
        "admin\t",
        "\nadmin",
        "\tadmin\n",
        " admin ",
        "ops\u00a0",  # NBSP
        "\u2003admin",  # EM SPACE
    ],
)
def test_publication_debit_rejects_whitespace_padded_roles(bad_role: str) -> None:
    """带前后空白的 role 不能免扣（review Task8 第三轮）：任何豁免组合下稳定 422 且零写入。

    role 专用校验 `_require_role`：必须 str、非空、<=32 且原值等于 value.strip()
    （不做 strip 规范化）——' ops '/'ops ' 等原值含前后空白一律 422，不因
    shared_library_submission / ops / admin / replay_generation 豁免放行，也不因
    unlimited 判定被规范化命中。只允许原始精确 ops/admin 被 unlimited（对照见
    test_publication_debit_exempt_unlimited_replay 与
    test_exemption_still_validates_all_inputs_before_early_return 的精确 role 合法
    豁免）；unknown_role 超限 409 见 test_gate_rejects_exactly_over_limit_and_minister_is_gated。
    """
    engine, quota = make_quota()
    with engine.begin() as connection:
        lock = quota.calendar.lock_or_verify(connection)
        for exempt, replay in (
            (None, 0),
            ("shared_library_submission", 0),
            (None, 1),
            ("shared_library_submission", 1),
        ):
            with pytest.raises(PlatformError) as exc:
                quota.record_publication_debit(
                    connection,
                    publication_status="succeeded",
                    quota_operation_id="job_ws",
                    publication_id="pub_ws",
                    quota_subject_user_id="u1",
                    pages=100,
                    ownership=ownership(),
                    calendar_lock=lock,
                    role=bad_role,
                    quota_exempt_reason=exempt,
                    replay_generation=replay,
                    published_at=NOW,
                )
            assert exc.value.code == "validation_error"
            assert exc.value.status_code == 422
        # 零写入：全部失败路径无账本/投影行
        assert debit_rows(connection) == []
        assert projection_rows(connection) == []


def test_publication_debit_port_hints_resolve_at_runtime() -> None:
    """PublicationDebitPort.record 类型注解运行时解析为精确类（review Task8 第二轮 #2）。

    ports 将 OwnershipSnapshot/CalendarLock 放在模块 global namespace 的运行时 import
    （无循环；reconcile 的反向 import 仍留在 TYPE_CHECKING），datetime 本就运行时引入。
    get_type_hints 断言业务终态 Literal 与三个字段解析为精确类型——不是字符串/ForwardRef。
    """
    hints = typing.get_type_hints(PublicationDebitPort.record)
    assert hints["publication_status"] == PublicationTerminalStatus
    assert hints["ownership"] is OwnershipSnapshot
    assert hints["calendar_lock"] is CalendarLock
    assert hints["published_at"] is datetime


def test_record_strong_entry_type_validation() -> None:
    """强入口类型校验（review Task8 #2）：非法 ownership/calendar_lock/published_at 稳定 422。

    mypy 层面这些值类型非法（见 test_quota_type_negative_fixture 的负例 fixture）；
    若类型系统被绕过（动态错误值），record → record_publication_debit → append_debit
    的运行时 isinstance 校验给出稳定 PlatformError，不泄漏 AttributeError/TypeError。
    """
    engine, quota = make_quota()
    with engine.begin() as connection:
        lock = quota.calendar.lock_or_verify(connection)
        with pytest.raises(PlatformError) as exc:
            quota.record_publication_debit(
                connection,
                publication_status="succeeded",
                quota_operation_id="job_t1",
                publication_id="pub_t1",
                quota_subject_user_id="u1",
                pages=100,
                ownership=object(),  # type: ignore[arg-type]
                calendar_lock=lock,
                role="user",
                published_at=NOW,
            )
        assert exc.value.code == "validation_error"
        assert exc.value.status_code == 422
        with pytest.raises(PlatformError) as exc:
            quota.record_publication_debit(
                connection,
                publication_status="succeeded",
                quota_operation_id="job_t2",
                publication_id="pub_t2",
                quota_subject_user_id="u1",
                pages=100,
                ownership=ownership(),
                calendar_lock=object(),  # type: ignore[arg-type]
                role="user",
                published_at=NOW,
            )
        assert exc.value.code == "validation_error"
        with pytest.raises(PlatformError) as exc:
            quota.record_publication_debit(
                connection,
                publication_status="succeeded",
                quota_operation_id="job_t3",
                publication_id="pub_t3",
                quota_subject_user_id="u1",
                pages=100,
                ownership=ownership(),
                calendar_lock=lock,
                role="user",
                published_at="2026-08-05T12:00:00Z",  # type: ignore[arg-type]
            )
        assert exc.value.code == "validation_error"
        assert debit_rows(connection) == []
        assert projection_rows(connection) == []


def test_record_alias_does_not_bypass_task7_validation() -> None:
    """record 别名路径不绕过 Task7 的 subject/ownership/input/fingerprint 校验（review Task8 #5）。

    经端口别名 record 调用的语义校验与直接 record_publication_debit 完全一致
    （同一 append_debit 入口）：ownership subject 不一致 422、ownership 字段非法 422、
    同 operation 异事实 409 ledger_invariant_conflict，且失败路径零写入。
    """
    engine, quota = make_quota()
    debit_port: PublicationDebitPort = quota
    with engine.begin() as connection:
        lock = quota.calendar.lock_or_verify(connection)
        # ownership.quota_subject_user_id 与显式 subject 不一致 → 422
        mismatched = OwnershipSnapshot(
            actor_user_id="u1",
            actor_role_snapshot="user",
            actor_department_id_snapshot=None,
            quota_subject_user_id="other",
            cost_center_key="user:u1",
        )
        with pytest.raises(PlatformError) as exc:
            debit_port.record(
                connection,
                publication_status="succeeded",
                quota_operation_id="job_s1",
                publication_id="pub_s1",
                quota_subject_user_id="u1",
                pages=100,
                ownership=mismatched,
                calendar_lock=lock,
                role="user",
                published_at=NOW,
            )
        assert exc.value.code == "validation_error"
        assert exc.value.status_code == 422
        # ownership 字段非法（cost_center_key 空）→ 422
        bad_fields = OwnershipSnapshot(
            actor_user_id="u1",
            actor_role_snapshot="user",
            actor_department_id_snapshot=None,
            quota_subject_user_id="u1",
            cost_center_key="",
        )
        with pytest.raises(PlatformError) as exc:
            debit_port.record(
                connection,
                publication_status="succeeded",
                quota_operation_id="job_s2",
                publication_id="pub_s2",
                quota_subject_user_id="u1",
                pages=100,
                ownership=bad_fields,
                calendar_lock=lock,
                role="user",
                published_at=NOW,
            )
        assert exc.value.code == "validation_error"
        # pages 边界（0）→ 422
        with pytest.raises(PlatformError) as exc:
            debit_port.record(
                connection,
                publication_status="succeeded",
                quota_operation_id="job_s3",
                publication_id="pub_s3",
                quota_subject_user_id="u1",
                pages=0,
                ownership=ownership(),
                calendar_lock=lock,
                role="user",
                published_at=NOW,
            )
        assert exc.value.code == "validation_error"
        # 正常插入后同 operation 异事实（pages 不同）经别名 → 409（fingerprint 校验）
        first = debit_port.record(
            connection,
            publication_status="succeeded",
            quota_operation_id="job_s4",
            publication_id="pub_s4",
            quota_subject_user_id="u1",
            pages=100,
            ownership=ownership(),
            calendar_lock=lock,
            role="user",
            published_at=NOW,
        )
        assert first is not None
        with pytest.raises(PlatformError) as exc:
            debit_port.record(
                connection,
                publication_status="succeeded",
                quota_operation_id="job_s4",
                publication_id="pub_s4",
                quota_subject_user_id="u1",
                pages=200,
                ownership=ownership(),
                calendar_lock=lock,
                role="user",
                published_at=NOW,
            )
        assert exc.value.code == "ledger_invariant_conflict"
        # 失败路径零写入；仅 job_s4 一行
        rows = debit_rows(connection)
        assert len(rows) == 1
        assert rows[0]["page_delta"] == 100
        proj = projection_rows(connection)
        assert len(proj) == 1
        assert proj[0]["used"] == 100


def test_quota_type_negative_fixture() -> None:
    """mypy 类型负例 fixture：错误类型必须被 mypy 真实报错（arg-type），运行时不崩溃。

    mypy 作用：在 `# type: ignore[arg-type]` 注释处验证错误类型确实被拒绝——若
    ports/record/check 退化为 Any/object（类型系统被绕过），mypy 会报 "unused
    'type: ignore' comment" → 检查失败。
    运行时作用：正例由 test_service_satisfies_gate_and_debit_ports 覆盖；本 fixture
    的坏调用（类型系统被动态绕过时）必须落到强入口运行时校验的稳定 422
    validation_error，而非 AttributeError/TypeError。
    """
    engine, quota = make_quota()
    with engine.begin() as connection:
        lock = quota.calendar.lock_or_verify(connection)
        debit_port: PublicationDebitPort = quota
        gate: DirectIngestGatePort = quota
        wrong_ownership: object = object()
        wrong_lock: object = object()
        wrong_when: object = "2026-08-05T12:00:00Z"
        wrong_pages: object = "one-hundred"
        wrong_status: str = "published"

        def expect_422(call) -> None:
            with pytest.raises(PlatformError) as exc:
                call()
            assert exc.value.code == "validation_error"
            assert exc.value.status_code == 422

        expect_422(
            lambda: debit_port.record(
                connection,
                publication_status=wrong_status,  # type: ignore[arg-type]
                quota_operation_id="job_bad_status",
                publication_id="pub_bad_status",
                quota_subject_user_id="u1",
                pages=100,
                ownership=ownership(),
                calendar_lock=lock,
                role="user",
                published_at=NOW,
            )
        )
        expect_422(
            lambda: debit_port.record(
                connection,
                publication_status="succeeded",
                quota_operation_id="job_t1",
                publication_id="pub_t1",
                quota_subject_user_id="u1",
                pages=100,
                ownership=wrong_ownership,  # type: ignore[arg-type]
                calendar_lock=lock,
                role="user",
                published_at=NOW,
            )
        )
        expect_422(
            lambda: debit_port.record(
                connection,
                publication_status="succeeded",
                quota_operation_id="job_t2",
                publication_id="pub_t2",
                quota_subject_user_id="u1",
                pages=100,
                ownership=ownership(),
                calendar_lock=wrong_lock,  # type: ignore[arg-type]
                role="user",
                published_at=NOW,
            )
        )
        expect_422(
            lambda: debit_port.record(
                connection,
                publication_status="succeeded",
                quota_operation_id="job_t3",
                publication_id="pub_t3",
                quota_subject_user_id="u1",
                pages=100,
                ownership=ownership(),
                calendar_lock=lock,
                role="user",
                published_at=wrong_when,  # type: ignore[arg-type]
            )
        )
        expect_422(
            lambda: debit_port.record(
                connection,
                publication_status="succeeded",
                quota_operation_id="job_t4",
                publication_id="pub_t4",
                quota_subject_user_id="u1",
                pages=wrong_pages,  # type: ignore[arg-type]
                ownership=ownership(),
                calendar_lock=lock,
                role="user",
                published_at=NOW,
            )
        )
        expect_422(
            lambda: gate.check(
                connection,
                quota_subject_user_id="u1",
                pages=wrong_pages,  # type: ignore[arg-type]
                role="user",
            )
        )
        # 全部失败路径零写入
        assert debit_rows(connection) == []
        assert projection_rows(connection) == []


def test_publication_debit_same_operation_different_facts_is_conflict() -> None:
    """同 quota_operation_id 异事实 → 409 ledger_invariant_conflict，不新增行。"""
    engine, quota = make_quota()
    with engine.begin() as connection:
        lock = quota.calendar.lock_or_verify(connection)
        quota.record_publication_debit(
            connection,
            publication_status="succeeded",
            quota_operation_id="job_7",
            publication_id="pub_7",
            quota_subject_user_id="u1",
            pages=100,
            ownership=ownership(),
            calendar_lock=lock,
            role="user",
            published_at=NOW,
        )
        with pytest.raises(PlatformError) as exc:
            quota.record_publication_debit(
                connection,
                publication_status="succeeded",
                quota_operation_id="job_7",
                publication_id="pub_7",
                quota_subject_user_id="u1",
                pages=200,
                ownership=ownership(),
                calendar_lock=lock,
                role="user",
                published_at=NOW,
            )
        assert exc.value.code == "ledger_invariant_conflict"
        rows = debit_rows(connection)
        assert len(rows) == 1
        assert rows[0]["page_delta"] == 100


def test_publication_debit_published_at_decides_effective_period() -> None:
    """published_at 决定 effective period（跨业务月整体归期）；recorded 用 DB now。"""
    engine, quota = make_quota()
    with engine.begin() as connection:
        lock = quota.calendar.lock_or_verify(connection)
        debit_id = quota.record_publication_debit(
            connection,
            publication_status="succeeded",
            quota_operation_id="job_9",
            publication_id="pub_9",
            quota_subject_user_id="u1",
            pages=120,
            ownership=ownership(),
            calendar_lock=lock,
            role="user",
            published_at=SEPT,
        )
        assert debit_id is not None
        rows = debit_rows(connection)
        assert len(rows) == 1
        row = rows[0]
        # SQLite 回读 naive UTC（方言行为）：与写入的 aware UTC 等价比较
        assert row["effective_at_utc"] == SEPT.replace(tzinfo=None)
        assert row["effective_period"] == "2026-09"  # published_at 归期（Asia/Shanghai）
        assert row["quota_period"] == "2026-09"
        assert row["recorded_at_utc"] == NOW.replace(tzinfo=None)  # recorded 用 DB now
        assert row["recorded_period"] == "2026-08"
        proj = projection_rows(connection)
        assert len(proj) == 1
        assert proj[0]["quota_period"] == "2026-09"
        assert proj[0]["used"] == 120


def test_publication_debit_caller_transaction_rollback() -> None:
    """debit 与投影属于调用方事务：回滚后与业务结果一起消失（服务不自行开事务）。"""
    engine, quota = make_quota()
    connection = engine.connect()
    tx = connection.begin()
    try:
        lock = quota.calendar.lock_or_verify(connection)
        quota.record_publication_debit(
            connection,
            publication_status="succeeded",
            quota_operation_id="job_4",
            publication_id="pub_4",
            quota_subject_user_id="u1",
            pages=120,
            ownership=ownership(),
            calendar_lock=lock,
            role="user",
            published_at=NOW,
        )
        assert len(debit_rows(connection)) == 1
        assert len(projection_rows(connection)) == 1
        tx.rollback()
    finally:
        connection.close()
    with engine.connect() as connection:
        assert debit_rows(connection) == []
        assert projection_rows(connection) == []


def test_service_satisfies_gate_and_debit_ports() -> None:
    """QuotaService 与 DirectIngestGatePort/PublicationDebitPort 结构化兼容。

    `assert_type(gate, DirectIngestGatePort)` / `assert_type(debit_port,
    PublicationDebitPort)` 由 mypy 静态验证（method 名/参数签名一致、使用同一组精确
    领域类型）；运行时经端口引用调用别名方法。
    """
    engine, quota = make_quota()
    gate: DirectIngestGatePort = quota
    debit_port: PublicationDebitPort = quota
    assert_type(gate, DirectIngestGatePort)
    assert_type(debit_port, PublicationDebitPort)
    with engine.begin() as connection:
        gate.check(connection, quota_subject_user_id="u1", pages=1, role="user")
        lock = quota.calendar.lock_or_verify(connection)
        result = debit_port.record(
            connection,
            publication_status="succeeded",
            quota_operation_id="job_1",
            publication_id="pub_1",
            quota_subject_user_id="u1",
            pages=120,
            ownership=ownership(),
            calendar_lock=lock,
            role="user",
            published_at=NOW,
        )
        assert result is not None
        assert len(debit_rows(connection)) == 1
        assert len(projection_rows(connection)) == 1


@pytest.mark.parametrize("operation", ["initial", "replace", "restore", "reindex"])
def test_direct_publication_operations_share_gate_and_success_debit_contract(
    operation: str,
) -> None:
    """Every direct operation gates first and debits once by its stable job id."""
    engine, quota = make_quota()
    gate: DirectIngestGatePort = quota
    debit_port: PublicationDebitPort = quota
    job_id = f"job-{operation}"

    with engine.begin() as connection:
        gate.check(connection, quota_subject_user_id="u1", pages=120, role="user")
        lock = quota.calendar.lock_or_verify(connection)
        first_debit = debit_port.record(
            connection,
            publication_status="succeeded",
            quota_operation_id=job_id,
            publication_id=f"publication-{operation}",
            quota_subject_user_id="u1",
            pages=120,
            ownership=ownership(),
            calendar_lock=lock,
            role="user",
            published_at=NOW,
        )
        replayed_debit = debit_port.record(
            connection,
            publication_status="succeeded",
            quota_operation_id=job_id,
            publication_id=f"publication-{operation}",
            quota_subject_user_id="u1",
            pages=120,
            ownership=ownership(),
            calendar_lock=lock,
            role="user",
            published_at=NOW,
        )
        persisted_debits = debit_rows(connection)
        persisted_projection = projection_rows(connection)

    assert first_debit is not None
    assert replayed_debit == first_debit
    assert [(row["quota_operation_id"], row["page_delta"]) for row in persisted_debits] == [
        (job_id, 120)
    ]
    assert len(persisted_projection) == 1
    assert persisted_projection[0]["used"] == 120


@pytest.mark.parametrize("operation", ["initial", "replace", "restore", "reindex"])
def test_direct_publication_operations_all_fail_gate_when_quota_is_exceeded(
    operation: str,
) -> None:
    """No direct operation may bypass the shared fail-closed balance gate."""
    engine, quota = make_quota()
    gate: DirectIngestGatePort = quota
    fill_quota(engine, quota)

    with engine.begin() as connection:
        before_debits = debit_rows(connection)
        before_projection = projection_rows(connection)
        with pytest.raises(PlatformError) as exc:
            gate.check(
                connection,
                quota_subject_user_id="u1",
                pages=1,
                role="user",
            )
        after_debits = debit_rows(connection)
        after_projection = projection_rows(connection)

    assert exc.value.code == "quota_exceeded", operation
    assert exc.value.status_code == 409
    assert after_debits == before_debits
    assert after_projection == before_projection


@pytest.mark.parametrize(
    "execution_kind",
    ["chat", "evaluation", "graph", "deletion", "archive", "index_maintenance"],
)
def test_non_publication_work_records_usage_without_quota_side_effects(
    execution_kind: str,
) -> None:
    """Non-publication work remains billable usage even when quota is untouched."""
    engine, ledger, quota = make_policy_services()

    event_id = record_actual_usage(
        ledger,
        execution_kind=execution_kind,
        execution_id=f"{execution_kind}-1",
        result="succeeded",
    )

    with engine.begin() as connection:
        persisted_usage = usage_rows(connection)
        persisted_debits = debit_rows(connection)
        persisted_projection = projection_rows(connection)
        persisted_calls = provider_call_rows(connection)
        snapshot = quota.read_snapshot(
            connection,
            quota_subject_user_id="u1",
            role="user",
        )
    assert [(row["usage_event_id"], row["execution_kind"]) for row in persisted_usage] == [
        (event_id, execution_kind)
    ]
    assert persisted_usage[0]["result"] == "succeeded"
    assert persisted_debits == []
    assert persisted_projection == []
    assert persisted_calls == []
    assert snapshot.used == 0


@pytest.mark.parametrize("operation", ["initial", "replace", "restore", "reindex"])
@pytest.mark.parametrize(
    "case",
    [
        PublicationProviderCase(
            name="succeeded",
            behavior="succeeded",
            provider_status="completed",
            usage_result="succeeded",
            policy_state="succeeded",
            policy_error_class=None,
        ),
        PublicationProviderCase(
            name="known_failure",
            behavior="known_failure",
            provider_status="completed",
            usage_result="failed",
            policy_state="failed",
            policy_error_class="upstream_503",
        ),
        PublicationProviderCase(
            name="cancelled",
            behavior="cancelled",
            provider_status="unknown",
            usage_result=None,
            policy_state=None,
            policy_error_class=None,
        ),
        PublicationProviderCase(
            name="unknown",
            behavior="unknown",
            provider_status="unknown",
            usage_result=None,
            policy_state="unknown",
            policy_error_class="timeout",
        ),
    ],
    ids=lambda case: case.name,
)
def test_publication_provider_matrix_persists_real_cost_facts(
    operation: str,
    case: PublicationProviderCase,
) -> None:
    """Publication cases first cross the real provider/ledger lifecycle.

    The repository has no publication caller. This matrix therefore proves only the
    physical provider send/usage facts and leaves quota untouched. Business publication
    terminal states are verified separately through the fail-closed debit port; no
    provider not-sent outcome is used to impersonate business dead-letter control flow.
    """
    engine, ledger, quota = make_policy_services()
    seed_provider_price(engine, ledger)
    gate: DirectIngestGatePort = quota
    execution_id = f"{operation}-{case.name}"
    provider_call_root_id = f"pc-publication-{execution_id}"

    if operation == "initial":
        with engine.begin() as connection:
            gate.check(connection, quota_subject_user_id="u1", pages=120, role="user")

    if case.behavior == "cancelled":
        with pytest.raises(asyncio.CancelledError):
            run_publication_provider_call(
                ledger,
                execution_kind=operation,
                execution_id=execution_id,
                provider_call_root_id=provider_call_root_id,
                behavior=case.behavior,
            )
    else:
        result = run_publication_provider_call(
            ledger,
            execution_kind=operation,
            execution_id=execution_id,
            provider_call_root_id=provider_call_root_id,
            behavior=case.behavior,
        )
        assert result.state == case.policy_state
        assert result.error_class == case.policy_error_class

    with engine.connect() as connection:
        persisted_calls = provider_call_rows(connection)
        persisted_usage = usage_rows(connection)
        persisted_debits = debit_rows(connection)
        persisted_projection = projection_rows(connection)

    assert len(persisted_calls) == 1
    assert persisted_calls[0]["status"] == case.provider_status
    provider_usage = [row for row in persisted_usage if row["event_kind"] == "provider_usage"]
    if case.usage_result is None:
        assert provider_usage == []
    else:
        assert len(provider_usage) == 1
        assert provider_usage[0]["result"] == case.usage_result
        assert provider_usage[0]["provider_call_id"] == persisted_calls[0]["provider_call_id"]
    assert persisted_debits == []
    assert persisted_projection == []


@pytest.mark.parametrize("publication_status", ["failed", "cancelled", "dead_letter"])
def test_business_terminal_failure_preserves_cost_facts_without_debit(
    publication_status: PublicationTerminalStatus,
) -> None:
    """Business failure is distinct from provider send state and is fail-closed."""
    engine, ledger, quota = make_policy_services()
    seed_provider_price(engine, ledger)
    execution_id = f"replace-business-{publication_status}"
    provider_result = run_publication_provider_call(
        ledger,
        execution_kind="replace",
        execution_id=execution_id,
        provider_call_root_id=f"pc-publication-{execution_id}",
        behavior="succeeded",
    )
    assert provider_result.state == "succeeded"
    local_event_id = record_actual_usage(
        ledger,
        execution_kind="replace",
        execution_id=execution_id,
        result="succeeded",
    )

    debit_port: PublicationDebitPort = quota
    with engine.begin() as connection:
        lock = quota.calendar.lock_or_verify(connection)
        debit = debit_port.record(
            connection,
            publication_status=publication_status,
            quota_operation_id=execution_id,
            publication_id=f"publication-{execution_id}",
            quota_subject_user_id="u1",
            pages=120,
            ownership=ownership(),
            calendar_lock=lock,
            role="user",
            published_at=NOW,
        )
    assert debit is None

    with engine.connect() as connection:
        persisted_calls = provider_call_rows(connection)
        persisted_usage = usage_rows(connection)
        persisted_debits = debit_rows(connection)
        persisted_projection = projection_rows(connection)
    provider_usage = [row for row in persisted_usage if row["event_kind"] == "provider_usage"]
    local_usage = [row for row in persisted_usage if row["event_kind"] == "local_usage"]
    assert len(persisted_calls) == 1
    assert persisted_calls[0]["status"] == "completed"
    assert len(provider_usage) == 1
    assert provider_usage[0]["result"] == "succeeded"
    assert [(row["usage_event_id"], row["result"]) for row in local_usage] == [
        (local_event_id, "succeeded")
    ]
    assert persisted_debits == []
    assert persisted_projection == []


def test_success_publication_provider_cost_and_debit_are_idempotent() -> None:
    """Provider/local completion and publication debit replays remain singletons."""
    engine, ledger, quota = make_policy_services()
    seed_provider_price(engine, ledger)
    operation_calls: list[str] = []
    execution_id = "initial-success-replay"
    root_id = "pc-publication-initial-success-replay"
    first_result = run_publication_provider_call(
        ledger,
        execution_kind="initial",
        execution_id=execution_id,
        provider_call_root_id=root_id,
        behavior="succeeded",
        operation_calls=operation_calls,
    )
    assert first_result.state == "succeeded"
    assert len(operation_calls) == 1
    physical_call_id = operation_calls[0]
    with engine.connect() as connection:
        original_call = provider_call_rows(connection)[0]
        original_provider_usage = [
            row for row in usage_rows(connection) if row["event_kind"] == "provider_usage"
        ]
    assert original_call["provider_call_id"] == physical_call_id
    assert original_call["request_fingerprint"] == f"fp-{execution_id}"
    assert len(original_provider_usage) == 1
    original_provider_usage_id = original_provider_usage[0]["usage_event_id"]

    with pytest.raises(PlatformError) as replay_error:
        run_publication_provider_call(
            ledger,
            execution_kind="initial",
            execution_id=execution_id,
            provider_call_root_id=root_id,
            behavior="succeeded",
            operation_calls=operation_calls,
        )
    assert replay_error.value.code == "provider_call_state_conflict"
    assert len(operation_calls) == 1

    with pytest.raises(PlatformError) as fingerprint_error:
        run_publication_provider_call(
            ledger,
            execution_kind="initial",
            execution_id=execution_id,
            provider_call_root_id=root_id,
            behavior="succeeded",
            operation_calls=operation_calls,
            request_fingerprint="fp-conflicting-replay",
        )
    assert fingerprint_error.value.code == "ledger_invariant_conflict"
    assert fingerprint_error.value.details == {"field": "request_fingerprint"}
    assert len(operation_calls) == 1

    first_completion_replay = ledger.complete_provider_call(
        provider_call_id=physical_call_id,
        measurement=provider_measurement(),
        ownership=ownership(),
        result="succeeded",
    )
    second_completion_replay = ledger.complete_provider_call(
        provider_call_id=physical_call_id,
        measurement=provider_measurement(),
        ownership=ownership(),
        result="succeeded",
    )
    assert first_completion_replay == original_provider_usage_id
    assert second_completion_replay == original_provider_usage_id

    first_local_usage = record_actual_usage(
        ledger,
        execution_kind="initial",
        execution_id=execution_id,
        result="succeeded",
    )
    replayed_local_usage = record_actual_usage(
        ledger,
        execution_kind="initial",
        execution_id=execution_id,
        result="succeeded",
    )
    assert replayed_local_usage == first_local_usage

    debit_port: PublicationDebitPort = quota
    with engine.begin() as connection:
        lock = quota.calendar.lock_or_verify(connection)
        first_debit = debit_port.record(
            connection,
            publication_status="succeeded",
            quota_operation_id="initial-success-replay",
            publication_id="publication-initial-success-replay",
            quota_subject_user_id="u1",
            pages=120,
            ownership=ownership(),
            calendar_lock=lock,
            role="user",
            published_at=NOW,
        )
        replayed_debit = debit_port.record(
            connection,
            publication_status="succeeded",
            quota_operation_id="initial-success-replay",
            publication_id="publication-initial-success-replay",
            quota_subject_user_id="u1",
            pages=120,
            ownership=ownership(),
            calendar_lock=lock,
            role="user",
            published_at=NOW,
        )

    assert first_debit is not None
    assert replayed_debit == first_debit
    with engine.connect() as connection:
        persisted_calls = provider_call_rows(connection)
        persisted_usage = usage_rows(connection)
        persisted_debits = debit_rows(connection)
        persisted_projection = projection_rows(connection)
    provider_usage = [row for row in persisted_usage if row["event_kind"] == "provider_usage"]
    local_usage = [row for row in persisted_usage if row["event_kind"] == "local_usage"]
    assert len(persisted_calls) == 1
    assert persisted_calls[0]["provider_call_id"] == physical_call_id
    assert persisted_calls[0]["request_fingerprint"] == f"fp-{execution_id}"
    assert persisted_calls[0]["status"] == "completed"
    assert [row["usage_event_id"] for row in provider_usage] == [original_provider_usage_id]
    assert Decimal(str(provider_usage[0]["estimated_cost_amount"])) == Decimal("0.005")
    assert [row["usage_event_id"] for row in local_usage] == [first_local_usage]
    assert len(persisted_debits) == 1
    assert len(persisted_projection) == 1
    assert persisted_projection[0]["used"] == 120


@pytest.mark.parametrize(
    ("role", "quota_exempt_reason", "replay_generation"),
    [
        ("user", "shared_library_submission", 0),
        ("ops", None, 0),
        ("admin", None, 0),
        ("user", None, 1),
    ],
    ids=["shared", "ops", "admin", "replay-generation"],
)
def test_success_provider_cost_exemptions_use_real_debit_port(
    role: str,
    quota_exempt_reason: str | None,
    replay_generation: int,
) -> None:
    """Exemptions are proven through PublicationDebitPort, not a test invoke flag."""
    engine, ledger, quota = make_policy_services()
    seed_provider_price(engine, ledger)
    execution_id = f"replace-exempt-{role}-{replay_generation}"
    result = run_publication_provider_call(
        ledger,
        execution_kind="replace",
        execution_id=execution_id,
        provider_call_root_id=f"pc-publication-{execution_id}",
        behavior="succeeded",
    )
    assert result.state == "succeeded"

    debit_port: PublicationDebitPort = quota
    with engine.begin() as connection:
        lock = quota.calendar.lock_or_verify(connection)
        first = debit_port.record(
            connection,
            publication_status="succeeded",
            quota_operation_id=execution_id,
            publication_id=f"publication-{execution_id}",
            quota_subject_user_id="u1",
            pages=120,
            ownership=ownership(),
            calendar_lock=lock,
            role=role,
            quota_exempt_reason=quota_exempt_reason,
            replay_generation=replay_generation,
            published_at=NOW,
        )
        replay = debit_port.record(
            connection,
            publication_status="succeeded",
            quota_operation_id=execution_id,
            publication_id=f"publication-{execution_id}",
            quota_subject_user_id="u1",
            pages=120,
            ownership=ownership(),
            calendar_lock=lock,
            role=role,
            quota_exempt_reason=quota_exempt_reason,
            replay_generation=replay_generation,
            published_at=NOW,
        )
        assert first is None
        assert replay is None
        assert debit_rows(connection) == []
        assert projection_rows(connection) == []

    with engine.connect() as connection:
        assert len(provider_call_rows(connection)) == 1
        assert (
            len([row for row in usage_rows(connection) if row["event_kind"] == "provider_usage"])
            == 1
        )


def test_rejected_initial_gate_creates_no_attempt_usage_or_additional_debit() -> None:
    engine, _ledger, quota = make_policy_services()
    gate: DirectIngestGatePort = quota
    fill_quota(engine, quota)

    with engine.begin() as connection:
        before_debits = debit_rows(connection)
        before_projection = projection_rows(connection)
        with pytest.raises(PlatformError) as exc:
            gate.check(connection, quota_subject_user_id="u1", pages=1, role="user")
        after_debits = debit_rows(connection)
        after_projection = projection_rows(connection)
        persisted_usage = usage_rows(connection)
        persisted_calls = provider_call_rows(connection)

    assert exc.value.code == "quota_exceeded"
    assert len(before_debits) == 1
    assert after_debits == before_debits
    assert after_projection == before_projection
    assert persisted_usage == []
    assert persisted_calls == []


@pytest.mark.parametrize("outcome", ["deduplicated", "job_not_created", "provider_not_sent"])
def test_pre_cost_short_circuits_have_no_usage_or_quota_debit(outcome: str) -> None:
    """No physical cost means no usage/debit, including an explicitly not-sent call."""
    engine, ledger, _quota = make_policy_services()
    provider_call_id: str | None = None

    if outcome != "job_not_created":
        provider_call_id, created = ledger.prepare_provider_call_with_status(
            provider="test-provider",
            model="m",
            operation="generate",
            execution_kind="ingestion",
            execution_id=f"{outcome}-1",
            provider_call_id=f"pc-{outcome}",
            attempt_id=f"attempt-{outcome}",
            deadline_utc=NOW + timedelta(minutes=1),
            request_fingerprint=f"fp-{outcome}",
        )
        assert created is True
        ledger.mark_not_sent(provider_call_id)
        if outcome == "deduplicated":
            replayed_id, replayed_created = ledger.prepare_provider_call_with_status(
                provider="test-provider",
                model="m",
                operation="generate",
                execution_kind="ingestion",
                execution_id=f"{outcome}-1",
                provider_call_id=f"pc-{outcome}",
                attempt_id=f"attempt-{outcome}",
                deadline_utc=NOW + timedelta(minutes=1),
                request_fingerprint=f"fp-{outcome}",
            )
            assert replayed_id == provider_call_id
            assert replayed_created is False

    with engine.connect() as connection:
        persisted_usage = usage_rows(connection)
        persisted_debits = debit_rows(connection)
        persisted_projection = projection_rows(connection)
        persisted_calls = provider_call_rows(connection)
    assert persisted_usage == []
    assert persisted_debits == []
    assert persisted_projection == []
    if outcome == "job_not_created":
        assert provider_call_id is None
        assert persisted_calls == []
    else:
        assert provider_call_id is not None
        assert [(row["provider_call_id"], row["status"]) for row in persisted_calls] == [
            (provider_call_id, "not_sent")
        ]
