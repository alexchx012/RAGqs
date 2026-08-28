"""provider-egress-transport 契约测试。

A1: 能力层（indexing/chat/evaluation/agents）不再出现直接构造模型 HTTP 请求的
    ``httpx.`` 调用（唯一白名单：platform 层 model_http transport 构造与基础设施
    检索后端 meilisearch/milvus/opensearch）。
A2: 假 transport 驱动的 §2.9 韧性序列断言（同步 ≤3 次、退避序列、熔断 open 60s
    half-open 单探测）。
A3: 每次物理发送产生新 provider_call_id；熔断拒绝不产生 usage 事件。
A13: LedgerBackedProviderReconciliationPort 生产对账契约。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

from app.platform.errors import PlatformError
from app.platform.model_http import (
    ModelHttpError,
    ModelHttpTransport,
    model_http_post,
)
from app.platform.provider import (
    CircuitBreakerRegistry,
    CircuitOpen,
    ProviderCallContext,
    ProviderFailure,
    call_with_policy,
)

# ---------------------------------------------------------------------------
# A1 静态守卫：能力层无模型 HTTP 直连
# ---------------------------------------------------------------------------

_CAPABILITY_PACKAGES = ("indexing", "chat", "evaluation", "agents")
# 基础设施检索后端（非模型出网）与 platform 层统一出网点是白名单。
_HTTPX_WHITELIST = (
    "app/platform/model_http.py",
    "app/indexing/meilisearch.py",
    "app/indexing/milvus.py",
    "app/indexing/opensearch.py",
)


def _capability_files(root: str) -> list[str]:
    import os

    files: list[str] = []
    for package in _CAPABILITY_PACKAGES:
        base = os.path.join(root, "app", package)
        for dirpath, _dirnames, filenames in os.walk(base):
            for name in filenames:
                if name.endswith(".py"):
                    files.append(os.path.join(dirpath, name))
    return files


def test_capability_layer_has_no_direct_model_http_calls() -> None:
    """A1: 能力层源码不出现 ``httpx.`` 直连（白名单：platform 统一出网点与
    基础设施检索后端）。"""

    import os

    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    offenders: list[str] = []
    for path in _capability_files(root):
        normalized = path.replace(os.sep, "/")
        repo_relative = normalized.split("/app/")[-1]
        if not repo_relative.startswith("app/"):
            repo_relative = "app/" + repo_relative
        if repo_relative in _HTTPX_WHITELIST:
            continue
        with open(path, encoding="utf-8") as handle:
            for lineno, line in enumerate(handle, start=1):
                stripped = line.strip()
                if not stripped or stripped.startswith("#"):
                    continue
                if "httpx." in stripped:
                    offenders.append(f"{repo_relative}:{lineno}: {stripped}")
                elif "httpx.Client(" in stripped or "httpx.post(" in stripped:
                    offenders.append(f"{repo_relative}:{lineno}: {stripped}")
    assert offenders == [], "capability layer must not construct model HTTP requests: " + "\n".join(
        offenders
    )


def test_model_http_module_is_the_platform_egress_point() -> None:
    """A1: 统一出网点存在且 prompt_enhance/judge 仍保留 dispose 语义所需 client。"""

    import inspect

    source = inspect.getsource(ModelHttpTransport)
    assert "httpx.Client" in source  # 物理构造只发生在 platform 层
    assert "httpx" in source


# ---------------------------------------------------------------------------
# A2/A3 假 transport 韧性序列
# ---------------------------------------------------------------------------


class _FakeTransport(httpx.BaseTransport):
    """假 transport：脚本化每次物理发送的响应/异常，记录调用序列。"""

    def __init__(self, script: list[Any]) -> None:
        self._script = list(script)
        self.calls: list[str] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request.url.path)
        item = self._script.pop(0) if self._script else httpx.Response(200, json={})
        if isinstance(item, Exception):
            raise item
        return item


class _FakeClock:
    def __init__(self, start: datetime) -> None:
        self.now = start
        self.sleeps: list[float] = []

    def __call__(self) -> datetime:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now = self.now + timedelta(seconds=seconds)


def _context(deadline_seconds: float = 60.0) -> ProviderCallContext:
    started = datetime(2026, 8, 28, 12, 0, tzinfo=UTC)
    return ProviderCallContext(
        provider="dashscope",
        operation="contract.check",
        provider_call_id="root_contract_check",
        attempt_id="attempt_1",
        deadline_utc=started + timedelta(seconds=deadline_seconds),
    )


def test_synchronous_retry_budget_is_three_with_backoff_sequence() -> None:
    """A2: 同步路径 ≤3 次物理发送；退避序列 250ms/1s/4s（capped 30s）。"""

    clock = _FakeClock(datetime(2026, 8, 28, 12, 0, tzinfo=UTC))
    transport = _FakeTransport([httpx.Response(503, text="unavailable") for _ in range(3)])
    with pytest.raises(ModelHttpError) as raised:
        model_http_post(
            provider="dashscope",
            operation="contract.check",
            url="https://provider.invalid/v1/check",
            payload={"k": "v"},
            timeout_seconds=60.0,
            transport=transport,
            now=clock,
            sleep=clock.sleep,
            jitter=lambda delay: 0.0,
        )
    assert len(transport.calls) == 3  # 同步预算 3 次
    assert raised.value.attempts == 3
    # 两次重试间退避：250ms、1s（第 3 次后不再退避）
    assert clock.sleeps == [0.25, 1.0]


def test_retryable_503_reuses_200_recovery() -> None:
    """A2: 429/503/504/网络错误可重试，随后成功即整体成功。"""

    clock = _FakeClock(datetime(2026, 8, 28, 12, 0, tzinfo=UTC))
    transport = _FakeTransport(
        [
            httpx.Response(429, text="rate"),
            httpx.Response(503, text="down"),
            httpx.Response(200, json={"ok": True}),
        ]
    )
    egress = model_http_post(
        provider="dashscope",
        operation="contract.check",
        url="https://provider.invalid/v1/check",
        payload={"k": "v"},
        timeout_seconds=60.0,
        transport=transport,
        now=clock,
        sleep=clock.sleep,
        jitter=lambda delay: 0.0,
    )
    assert egress.status_code == 200
    assert egress.attempts == 3
    assert len(transport.calls) == 3


def test_deterministic_4xx_is_not_retried() -> None:
    """A2: 确定性 4xx（401）不重试——单次物理发送后立即失败。"""

    clock = _FakeClock(datetime(2026, 8, 28, 12, 0, tzinfo=UTC))
    transport = _FakeTransport([httpx.Response(401, text="denied")])
    with pytest.raises(ModelHttpError) as raised:
        model_http_post(
            provider="dashscope",
            operation="contract.check",
            url="https://provider.invalid/v1/check",
            payload={},
            timeout_seconds=60.0,
            transport=transport,
            now=clock,
            sleep=clock.sleep,
        )
    assert len(transport.calls) == 1
    assert raised.value.error_class == "http_401"


def test_circuit_opens_after_five_retryable_failures_and_rejects() -> None:
    """A2: 连续 5 次可重试失败 → open 60s；open 期间拒绝且不再物理发送。"""

    clock = _FakeClock(datetime(2026, 8, 28, 12, 0, tzinfo=UTC))
    circuits = CircuitBreakerRegistry()
    # 5 轮 × 3 次物理发送 = 15 次失败后熔断 open。
    transport = _FakeTransport([httpx.Response(503, text="down") for _ in range(20)])
    for _round in range(5):
        with pytest.raises(ModelHttpError):
            model_http_post(
                provider="dashscope",
                operation="contract.check",
                url="https://provider.invalid/v1/check",
                payload={},
                timeout_seconds=60.0,
                transport=transport,
                circuits=circuits,
                now=clock,
                sleep=clock.sleep,
            )
    assert len(transport.calls) == 15
    # 第 6 轮：熔断 open，直接 CircuitOpen 拒绝——不再产生物理发送。
    with pytest.raises(CircuitOpen):
        model_http_post(
            provider="dashscope",
            operation="contract.check",
            url="https://provider.invalid/v1/check",
            payload={},
            timeout_seconds=60.0,
            transport=transport,
            circuits=circuits,
            now=clock,
            sleep=clock.sleep,
        )
    assert len(transport.calls) == 15


def test_half_open_allows_single_probe_after_cooldown() -> None:
    """A2: open 60s 后 half-open 单探测；探测成功关闭熔断。"""

    clock = _FakeClock(datetime(2026, 8, 28, 12, 0, tzinfo=UTC))
    circuits = CircuitBreakerRegistry()
    transport = _FakeTransport(
        [httpx.Response(503, text="down") for _ in range(15)]
        + [httpx.Response(200, json={"ok": True})]
    )
    for _round in range(5):
        with pytest.raises(ModelHttpError):
            model_http_post(
                provider="dashscope",
                operation="contract.check",
                url="https://provider.invalid/v1/check",
                payload={},
                timeout_seconds=60.0,
                transport=transport,
                circuits=circuits,
                now=clock,
                sleep=clock.sleep,
            )
    assert len(transport.calls) == 15
    # 冷却 60s 未到 → CircuitOpen。
    with pytest.raises(CircuitOpen):
        model_http_post(
            provider="dashscope",
            operation="contract.check",
            url="https://provider.invalid/v1/check",
            payload={},
            timeout_seconds=60.0,
            transport=transport,
            circuits=circuits,
            now=clock,
            sleep=clock.sleep,
        )
    assert len(transport.calls) == 15
    # 冷却 61s 后 → half-open 放行单探测；成功关闭。
    clock.now = clock.now + timedelta(seconds=61)
    egress = model_http_post(
        provider="dashscope",
        operation="contract.check",
        url="https://provider.invalid/v1/check",
        payload={},
        timeout_seconds=60.0,
        transport=transport,
        circuits=circuits,
        now=clock,
        sleep=clock.sleep,
    )
    assert egress.status_code == 200
    assert len(transport.calls) == 16


def test_each_physical_send_gets_distinct_provider_call_id() -> None:
    """A3: 每次物理发送使用新的派生 provider_call_id。"""

    seen_ids: list[str] = []
    clock = _FakeClock(datetime(2026, 8, 28, 12, 0, tzinfo=UTC))

    def operation(ctx: ProviderCallContext, request: Any) -> str:
        seen_ids.append(ctx.provider_call_id)
        if len(seen_ids) < 3:
            raise ProviderFailure("network_error", retryable=True, sent=True)
        return "done"

    result = call_with_policy(
        operation,
        _context(),
        None,
        now=clock,
        sleep=lambda _seconds: None,
        jitter=lambda delay: 0.0,
    )
    assert result.state == "succeeded"
    assert len(seen_ids) == 3
    assert len(set(seen_ids)) == 3  # 全部唯一
    assert all(pid.startswith("pc_") for pid in seen_ids)


def test_circuit_rejection_produces_no_usage_event() -> None:
    """A3: 熔断拒绝（未进入 operation）不产生 usage 事件。

    以 usage 包装 + 内存 lifecycle 断言：open 状态下 prepare 也未被调用。
    """

    from app.usage.provider_integration import (
        UsageSubmissionLifecycle,
        run_provider_call_with_usage,
    )

    class _RecordingSubmission:
        def __init__(self) -> None:
            self.prepared: list[str] = []
            self.completed: list[str] = []
            self.terminal: list[str] = []

        def prepare_provider_call(self, **kwargs: Any) -> str:
            call_id = kwargs["provider_call_id"]
            self.prepared.append(call_id)
            return call_id

        def mark_dispatching(self, provider_call_id: str, *, started_at_provider: Any) -> bool:
            return True

        def complete_provider_call(self, **kwargs: Any) -> str:
            self.completed.append(kwargs["provider_call_id"])
            return kwargs["provider_call_id"]

        def mark_not_sent(self, provider_call_id: str) -> None:
            self.terminal.append(("not_sent", provider_call_id))

        def mark_unknown(self, provider_call_id: str) -> None:
            self.terminal.append(("unknown", provider_call_id))

    from app.usage.ledger import OwnershipSnapshot, ProviderMeasurement

    submission = _RecordingSubmission()
    circuits = CircuitBreakerRegistry()
    # 预热熔断到 open。
    clock = _FakeClock(datetime(2026, 8, 28, 12, 0, tzinfo=UTC))
    for _round in range(5):
        with pytest.raises(ModelHttpError):
            model_http_post(
                provider="dashscope",
                operation="circuit.usage.check",
                url="https://provider.invalid/v1/check",
                payload={},
                timeout_seconds=60.0,
                transport=_FakeTransport([httpx.Response(503) for _ in range(3)]),
                circuits=circuits,
                now=clock,
                sleep=clock.sleep,
            )

    def operation(ctx: ProviderCallContext, request: Any) -> str:
        return "should-not-run"

    with pytest.raises(CircuitOpen):
        run_provider_call_with_usage(
            operation=operation,
            context=ProviderCallContext(
                provider="dashscope",
                operation="circuit.usage.check",
                provider_call_id="root_circuit_usage",
                attempt_id="attempt_1",
                deadline_utc=datetime(2026, 8, 28, 12, 5, tzinfo=UTC),
            ),
            model="test-model",
            lifecycle=UsageSubmissionLifecycle(submission),
            measurement_extractor=lambda value, ctx, failure: ProviderMeasurement(
                input_tokens=None,
                prompt_cache_hit_tokens=None,
                prompt_cache_miss_tokens=None,
                output_tokens=None,
                reasoning_tokens=None,
                image_count=None,
                visual_input_tokens=None,
                embedding_input_tokens=None,
                vector_count=None,
                measurement_sources={},
            ),
            ownership_provider=lambda ctx: OwnershipSnapshot(
                actor_user_id="user_1",
                actor_role_snapshot="user",
                actor_department_id_snapshot=None,
                quota_subject_user_id="user_1",
                cost_center_key="public",
            ),
            execution_kind="contract_test",
            execution_id="exec_1",
            request_fingerprint="fp_contract_circuit",
            circuits=circuits,
            now=clock,
            sleep=clock.sleep,
        )
    # 熔断拒绝在 prepare 之前发生：零 usage 生命周期事件。
    assert submission.prepared == []
    assert submission.completed == []
    assert submission.terminal == []


# ---------------------------------------------------------------------------
# A13 生产对账契约
# ---------------------------------------------------------------------------


def test_ledger_backed_reconciliation_confirms_terminal_states() -> None:
    """A13: completed→ConfirmedUsage（用量恢复）；not_sent/prepared→ConfirmedNotSent；
    dispatching/unknown/缺失行→StillUnknown。"""

    from decimal import Decimal

    from sqlalchemy import create_engine
    from sqlalchemy.pool import StaticPool

    from app.platform.database import SqlAlchemyDatabaseClock
    from app.usage.calendar import BusinessCalendarService
    from app.usage.ledger import OwnershipSnapshot, ProviderMeasurement, UsageLedger
    from app.usage.price import PriceCatalogService
    from app.usage.reconcile import (
        ConfirmedNotSent,
        ConfirmedUsage,
        LedgerBackedProviderReconciliationPort,
        StillUnknown,
    )
    from app.usage.schema import usage_metadata

    now = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)

    class FixedClock:
        def now_utc(self, connection=None) -> datetime:
            del connection
            return now

    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    usage_metadata.create_all(engine)
    calendar = BusinessCalendarService(engine, SqlAlchemyDatabaseClock(engine), "Asia/Shanghai")
    prices = PriceCatalogService(engine, FixedClock())
    ledger = UsageLedger(engine, FixedClock(), calendar, prices)
    with engine.begin() as connection:
        calendar.lock_or_verify(connection)
        prices.register(
            connection,
            provider="dashscope",
            model="qwen3.7-plus",
            operation="chat_generation",
            currency_code="USD",
            lines=[{"meter": "input_tokens", "unit": "token", "rate": Decimal("0.000020")}],
            effective_from_utc=now - timedelta(hours=1),
        )

    ownership = OwnershipSnapshot(
        actor_user_id="u1",
        actor_role_snapshot="user",
        actor_department_id_snapshot=None,
        quota_subject_user_id="u1",
        cost_center_key="user:u1",
    )
    measurement = ProviderMeasurement(
        input_tokens=10,
        output_tokens=None,
        prompt_cache_hit_tokens=None,
        prompt_cache_miss_tokens=None,
        reasoning_tokens=None,
        image_count=None,
        visual_input_tokens=None,
        embedding_input_tokens=None,
        vector_count=None,
        measurement_sources={"input_tokens": "provider_reported"},
    )

    def make_call(call_id: str) -> str:
        return ledger.prepare_provider_call(
            provider="dashscope",
            model="qwen3.7-plus",
            operation="chat_generation",
            execution_kind="generation",
            execution_id="gen_1",
            request_fingerprint=f"fp-{call_id}",
            provider_call_id=call_id,
            attempt_id=f"attempt-{call_id}",
            deadline_utc=now + timedelta(hours=1),
        )

    port = LedgerBackedProviderReconciliationPort(engine)

    completed_id = make_call("pc_completed_1")
    ledger.mark_dispatching(completed_id, started_at_provider=now)
    ledger.complete_provider_call(
        provider_call_id=completed_id,
        measurement=measurement,
        ownership=ownership,
        result="succeeded",
    )
    decision = port.confirm(provider_call_id=completed_id, fingerprint="fp", connection=None)
    assert isinstance(decision, ConfirmedUsage)
    assert decision.result == "succeeded"
    assert decision.measurement.input_tokens == 10
    assert decision.ownership.cost_center_key == "user:u1"

    not_sent_id = make_call("pc_not_sent_1")
    ledger.mark_dispatching(not_sent_id, started_at_provider=now)
    ledger.mark_not_sent(not_sent_id)
    decision = port.confirm(provider_call_id=not_sent_id, fingerprint="fp", connection=None)
    assert isinstance(decision, ConfirmedNotSent)

    prepared_id = make_call("pc_prepared_1")
    decision = port.confirm(provider_call_id=prepared_id, fingerprint="fp", connection=None)
    assert isinstance(decision, ConfirmedNotSent)

    unknown_id = make_call("pc_unknown_1")
    ledger.mark_dispatching(unknown_id, started_at_provider=now)
    ledger.mark_unknown(unknown_id)
    decision = port.confirm(provider_call_id=unknown_id, fingerprint="fp", connection=None)
    assert isinstance(decision, StillUnknown)

    decision = port.confirm(provider_call_id="pc_missing", fingerprint="fp", connection=None)
    assert isinstance(decision, StillUnknown)


# ---------------------------------------------------------------------------
# A4 runtime 装配契约
# ---------------------------------------------------------------------------


def test_runtime_assembles_dashscope_chat_provider_when_configured() -> None:
    """A4: DASHSCOPE_API_KEY（全局 provider api_key）就绪 → 生产 chat adapter；
    未配置 → Unavailable（503 fail-closed）。"""

    from app.chat.ports import UnavailableChatProviderPort
    from app.platform.chat_provider import DashScopeChatProvider
    from app.platform.config import load_platform_settings
    from app.platform.runtime import _default_chat_provider

    configured = _default_chat_provider(
        load_platform_settings(
            {
                "RAG_PLATFORM_PROFILE": "development",
                "RAG_DATABASE_URL": "sqlite+pysqlite:///:memory:",
                "RAG_OBJECT_STORAGE_ENDPOINT": "http://localhost:9000",
                "RAG_OBJECT_STORAGE_BUCKET": "rag-dev",
                "RAG_PROVIDER_NAME": "dashscope",
                "RAG_PROVIDER_API_KEY": "sk-test-key",
            }
        )
    )
    assert isinstance(configured, DashScopeChatProvider)

    unavailable = _default_chat_provider(
        load_platform_settings(
            {
                "RAG_PLATFORM_PROFILE": "development",
                "RAG_DATABASE_URL": "sqlite+pysqlite:///:memory:",
                "RAG_OBJECT_STORAGE_ENDPOINT": "http://localhost:9000",
                "RAG_OBJECT_STORAGE_BUCKET": "rag-dev",
                "RAG_PROVIDER_NAME": "fake",
            }
        )
    )
    assert isinstance(unavailable, UnavailableChatProviderPort)
    with pytest.raises(PlatformError) as raised:
        unavailable.generate(None)  # type: ignore[arg-type]
    assert raised.value.status_code == 503
