"""统一模型出网 transport：能力层唯一的模型 provider HTTP 构造与发送点。

所有能力层（indexing/chat/evaluation/agents）的模型出网调用——embedding、
contextual retrieval、图片 VLM、prompt-enhance、评测判官、chat 生成——都经
``ModelHttpTransport`` 执行单次物理发送，由 ``app.platform.provider.call_with_policy``
内核提供 §2.9 全量韧性语义（绝对 deadline、每次物理发送唯一 provider_call_id、
短重试与退避、按 provider+operation 隔离的熔断）。

契约测试静态断言能力层不再出现 ``httpx.`` 直连：本模块与基础设施检索后端
（meilisearch/milvus/opensearch）是仅有的 HTTP 构造白名单。
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from .provider import (
    CircuitBreakerRegistry,
    ProviderCallContext,
    ProviderFailure,
    ProviderResult,
    ProviderTelemetryPort,
    RetryPolicy,
    call_with_policy,
)

_logger = logging.getLogger(__name__)

_RETRYABLE_HTTP_STATUSES = frozenset({429, 502, 503, 504})
_TIMEOUT_ERROR_CLASSES = frozenset({"timeout", "deadline_exceeded"})
_FAILURE_BODY_LOG_LIMIT = 500


class ModelHttpError(RuntimeError):
    """统一出网最终失败信封：携带内核结果语义供能力层映射既有错误码。"""

    def __init__(self, *, provider: str, operation: str, result: ProviderResult) -> None:
        self.provider = provider
        self.operation = operation
        self.state = result.state
        self.error_class = result.error_class
        self.attempts = result.attempts
        self.retryable = result.retryable
        super().__init__(
            f"model egress failed: {provider}/{operation} "
            f"state={result.state} error_class={result.error_class} "
            f"attempts={result.attempts}"
        )

    @property
    def status_code(self) -> int | None:
        """从 error_class 还原最后一次物理失败的 HTTP 状态（http_<code> 惯例）。"""

        if self.error_class and self.error_class.startswith("http_"):
            suffix = self.error_class[len("http_") :]
            if suffix.isdigit():
                return int(suffix)
        return None

    @property
    def timeout(self) -> bool:
        return self.error_class in _TIMEOUT_ERROR_CLASSES


@dataclass(frozen=True, slots=True)
class ModelHttpResponse:
    """统一出网成功信封：最后一次物理调用的响应事实。"""

    body: Mapping[str, Any]
    status_code: int
    headers: Mapping[str, str]
    provider_call_id: str
    attempts: int
    provider_request_id: str | None = None


class ModelHttpTransport:
    """单次物理发送的 ProviderPort 适配器（模型 HTTP 构造唯一白名单点）。

    可直接作为 ``call_with_policy`` 的 operation，或作为
    ``run_provider_call_with_usage`` 的底层 operation 使用；每次调用按
    ``context.deadline_utc`` 推导本attempt超时，内核负责重试编排。

    连接复用：传入共享 ``client`` 时不拥有其生命周期（调用方负责 close/dispose，
    prompt_enhance/judge 的既有 dispose 语义保留）；未传入时按 attempt 短暂建连。
    """

    def __init__(
        self,
        *,
        url: str,
        headers: Mapping[str, str] | None = None,
        transport: httpx.BaseTransport | None = None,
        client: httpx.Client | None = None,
        method: str = "POST",
        params: Mapping[str, str] | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._url = url
        self._headers = dict(headers or {})
        self._transport = transport
        self._client = client
        self._method = method
        self._params = dict(params) if params is not None else None
        self._now = now or (lambda: datetime.now(UTC))

    def __call__(self, context: ProviderCallContext, request: Any) -> httpx.Response:
        return self.call(context, request)

    def call(self, context: ProviderCallContext, request: Any) -> httpx.Response:
        """一次物理发送：按 attempt deadline 推导超时并分类 §2.9 失败语义。"""

        remaining = (context.deadline_utc - self._now()).total_seconds()
        if remaining <= 0:
            raise ProviderFailure("timeout", retryable=True, sent=False)
        if isinstance(request, str):
            request_kwargs: dict[str, Any] = {"content": request}
        elif isinstance(request, Mapping):
            request_kwargs = {"json": dict(request)}
        else:
            request_kwargs = {"content": request}
        try:
            if self._client is not None:
                response = self._client.request(
                    self._method,
                    self._url,
                    headers=self._headers,
                    params=self._params,
                    timeout=remaining,
                    **request_kwargs,
                )
            else:
                with httpx.Client(
                    timeout=remaining,
                    transport=self._transport,
                ) as client:
                    response = client.request(
                        self._method,
                        self._url,
                        headers=self._headers,
                        params=self._params,
                        **request_kwargs,
                    )
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            # 连接从未建立：确定未发送（not_sent，无 usage）；错误类用内核可重试集合成员。
            raise ProviderFailure("network_error", retryable=True, sent=False) from exc
        except httpx.TimeoutException as exc:
            # 读/写超时：请求可能已到达 provider，结果无法确认（unknown）。
            raise ProviderFailure("timeout", retryable=True, sent=True) from exc
        except httpx.HTTPError as exc:
            # 其他中途网络错误：保守按已发送无法确认处理（unknown）。
            raise ProviderFailure("network_error", retryable=True, sent=True) from exc
        status = response.status_code
        if status >= 400:
            if status not in _RETRYABLE_HTTP_STATUSES:
                # 不可重试失败必然终态：记录 provider 自身的错误原文（截断），
                # 否则 model_http_post 只上抛 http_<code>，404/422 的真实原因
                # （模型不存在、路径错误等）对运维不可见。best-effort 观测，
                # 读取失败不影响异常分类。
                _log_http_failure(context, status, response)
            raise ProviderFailure(
                f"http_{status}",
                status_code=status,
                retryable=status in _RETRYABLE_HTTP_STATUSES,
                sent=True,
            )
        return response


def _log_http_failure(context: ProviderCallContext, status: int, response: httpx.Response) -> None:
    try:
        body: str = response.text
    except Exception as exc:  # noqa: BLE001 - 观测失败不改变异常语义
        _logger.warning(
            "model http failure: provider=%s operation=%s status=%s " "body_read_error=%s",
            context.provider,
            context.operation,
            status,
            exc,
        )
        return
    _logger.warning(
        "model http failure: provider=%s operation=%s status=%s body=%s",
        context.provider,
        context.operation,
        status,
        body[:_FAILURE_BODY_LOG_LIMIT],
    )


def _default_request_id(provider: str, operation: str, now: datetime) -> str:
    # 根调用标识由调用方传入（usage 生命周期使用持久化 root id）；无 usage 的
    # 直连调用使用可读的临时根标识，物理 attempt id 由内核按惯例派生。
    stamp = now.strftime("%Y%m%d%H%M%S%f")
    return f"mhr_{provider}_{operation}_{stamp}".replace("/", "-").replace(":", "-")


def new_provider_call_root_id(prefix: str = "mhr") -> str:
    """生成 usage 集成路径的根 provider_call_id（内核按 root+attempt 派生物理 id）。"""

    return f"{prefix}_{uuid.uuid4().hex}"


def model_http_post(
    *,
    provider: str,
    operation: str,
    url: str,
    headers: Mapping[str, str] | None = None,
    payload: Mapping[str, Any] | str | bytes | None = None,
    timeout_seconds: float,
    request_id: str | None = None,
    attempt_id: str | None = None,
    resource_id: str | None = None,
    asynchronous: bool = False,
    circuits: CircuitBreakerRegistry | None = None,
    telemetry: ProviderTelemetryPort | None = None,
    policy: RetryPolicy | None = None,
    now: Callable[[], datetime] | None = None,
    sleep: Callable[[float], None] | None = None,
    jitter: Callable[[float], float] | None = None,
    transport: httpx.BaseTransport | None = None,
    client: httpx.Client | None = None,
) -> ModelHttpResponse:
    """经统一内核发送一次模型 HTTP 请求（无 usage 账本集成的便捷入口）。

    成功返回 :class:`ModelHttpResponse`；重试预算耗尽/熔断打开时抛出
    :class:`ModelHttpError` 或 :class:`~app.platform.provider.CircuitOpen`，
    由能力层映射为既有对外错误码。
    """

    clock = now or (lambda: datetime.now(UTC))
    started = clock()
    root_id = request_id or _default_request_id(provider, operation, started)
    context = ProviderCallContext(
        provider=provider,
        operation=operation,
        provider_call_id=root_id,
        attempt_id=attempt_id or root_id,
        deadline_utc=started + timedelta(seconds=timeout_seconds),
        resource_id=resource_id,
    )
    egress = ModelHttpTransport(
        url=url, headers=headers, transport=transport, client=client, now=clock
    )
    result = call_with_policy(
        egress,
        context,
        payload if payload is not None else {},
        asynchronous=asynchronous,
        policy=policy,
        circuits=circuits,
        now=now,
        sleep=sleep,
        jitter=jitter,
        telemetry=telemetry,
    )
    if result.state != "succeeded":
        raise ModelHttpError(provider=provider, operation=operation, result=result)
    response = result.value
    if not isinstance(response, httpx.Response):
        raise ModelHttpError(
            provider=provider,
            operation=operation,
            result=ProviderResult(
                state="failed",
                error_class="invalid_transport_value",
                elapsed_ms=result.elapsed_ms,
                attempts=result.attempts,
                retryable=False,
            ),
        )
    try:
        body = response.json()
    except ValueError as exc:
        raise ModelHttpError(
            provider=provider,
            operation=operation,
            result=ProviderResult(
                state="failed",
                error_class="invalid_response_body",
                elapsed_ms=result.elapsed_ms,
                attempts=result.attempts,
                retryable=False,
            ),
        ) from exc
    if not isinstance(body, Mapping):
        raise ModelHttpError(
            provider=provider,
            operation=operation,
            result=ProviderResult(
                state="failed",
                error_class="invalid_response_body",
                elapsed_ms=result.elapsed_ms,
                attempts=result.attempts,
                retryable=False,
            ),
        )
    provider_request_id = response.headers.get("x-request-id") or response.headers.get("request-id")
    return ModelHttpResponse(
        body=body,
        status_code=response.status_code,
        headers=dict(response.headers),
        provider_call_id=context.provider_call_id,
        attempts=result.attempts,
        provider_request_id=provider_request_id,
    )


@dataclass(frozen=True, slots=True)
class ModelHttpStreamResponse:
    """统一流式出网成功信封：SSE ``data:`` 行已逐条回调，响应体不缓冲。"""

    status_code: int
    headers: Mapping[str, str]
    provider_call_id: str
    attempts: int
    data_count: int
    provider_request_id: str | None = None


class ModelHttpStreamTransport:
    """单次物理流式发送（SSE 行协议）。

    与 :class:`ModelHttpTransport` 共享连接/超时/HTTP 状态失败分类，但 2xx
    响应体按 SSE ``data:`` 行逐条交给 ``on_data`` 回调、不缓冲。流不可重放：
    调用方必须以单 attempt 策略运行，不做内核重试。
    """

    def __init__(
        self,
        *,
        url: str,
        headers: Mapping[str, str] | None = None,
        transport: httpx.BaseTransport | None = None,
        client: httpx.Client | None = None,
        on_data: Callable[[str], None],
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._url = url
        self._headers = dict(headers or {})
        self._transport = transport
        self._client = client
        self._on_data = on_data
        self._now = now or (lambda: datetime.now(UTC))

    def __call__(self, context: ProviderCallContext, request: Any) -> ModelHttpStreamResponse:
        remaining = (context.deadline_utc - self._now()).total_seconds()
        if remaining <= 0:
            raise ProviderFailure("timeout", retryable=True, sent=False)
        if isinstance(request, Mapping):
            request_kwargs: dict[str, Any] = {"json": dict(request)}
        else:
            request_kwargs = {"content": request}
        try:
            if self._client is not None:
                outcome = self._consume(self._client, context, remaining, request_kwargs)
            else:
                with httpx.Client(timeout=remaining, transport=self._transport) as client:
                    outcome = self._consume(client, context, remaining, request_kwargs)
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            raise ProviderFailure("network_error", retryable=True, sent=False) from exc
        except httpx.TimeoutException as exc:
            raise ProviderFailure("timeout", retryable=True, sent=True) from exc
        except httpx.HTTPError as exc:
            # 其他中途网络错误（含流中段断开）：保守按已发送无法确认处理。
            raise ProviderFailure("network_error", retryable=True, sent=True) from exc
        status, headers, data_count = outcome
        provider_request_id = headers.get("x-request-id") or headers.get("request-id")
        return ModelHttpStreamResponse(
            status_code=status,
            headers=headers,
            provider_call_id=context.provider_call_id,
            attempts=1,
            data_count=data_count,
            provider_request_id=provider_request_id,
        )

    def _consume(
        self,
        client: httpx.Client,
        context: ProviderCallContext,
        remaining: float,
        request_kwargs: dict[str, Any],
    ) -> tuple[int, dict[str, str], int]:
        with client.stream(
            "POST",
            self._url,
            headers=self._headers,
            timeout=remaining,
            **request_kwargs,
        ) as response:
            status = response.status_code
            if status >= 400:
                response.read()
                if status not in _RETRYABLE_HTTP_STATUSES:
                    _log_http_failure(context, status, response)
                raise ProviderFailure(
                    f"http_{status}",
                    status_code=status,
                    retryable=status in _RETRYABLE_HTTP_STATUSES,
                    sent=True,
                )
            data_count = 0
            for line in response.iter_lines():
                if not line.startswith("data:"):
                    continue
                data = line[len("data:") :].lstrip(" ")
                if not data:
                    continue
                data_count += 1
                self._on_data(data)
            return status, dict(response.headers), data_count


def model_http_stream_post(
    *,
    provider: str,
    operation: str,
    url: str,
    headers: Mapping[str, str] | None = None,
    payload: Mapping[str, Any] | str | bytes | None = None,
    timeout_seconds: float,
    on_data: Callable[[str], None],
    client: httpx.Client | None = None,
    transport: httpx.BaseTransport | None = None,
    now: Callable[[], datetime] | None = None,
    circuits: CircuitBreakerRegistry | None = None,
    telemetry: ProviderTelemetryPort | None = None,
) -> ModelHttpStreamResponse:
    """经统一内核发送一次流式模型 HTTP 请求（SSE ``data:`` 行逐条回调）。

    流式响应体不可重放：固定单 attempt（无内核重试）；绝对 deadline、熔断与
    遥测语义与 :func:`model_http_post` 一致。``on_data`` 抛出的
    :class:`ProviderFailure` 原样上抛并按内核失败语义记账。
    """

    clock = now or (lambda: datetime.now(UTC))
    started = clock()
    root_id = _default_request_id(provider, operation, started)
    context = ProviderCallContext(
        provider=provider,
        operation=operation,
        provider_call_id=root_id,
        attempt_id=root_id,
        deadline_utc=started + timedelta(seconds=timeout_seconds),
    )
    egress = ModelHttpStreamTransport(
        url=url,
        headers=headers,
        transport=transport,
        client=client,
        on_data=on_data,
        now=clock,
    )
    result = call_with_policy(
        egress,
        context,
        payload if payload is not None else {},
        asynchronous=False,
        policy=RetryPolicy(synchronous_attempts=1, asynchronous_attempts=1),
        circuits=circuits,
        now=now,
        telemetry=telemetry,
    )
    if result.state != "succeeded":
        raise ModelHttpError(provider=provider, operation=operation, result=result)
    stream = result.value
    if not isinstance(stream, ModelHttpStreamResponse):
        raise ModelHttpError(
            provider=provider,
            operation=operation,
            result=ProviderResult(
                state="failed",
                error_class="invalid_transport_value",
                elapsed_ms=result.elapsed_ms,
                attempts=result.attempts,
                retryable=False,
            ),
        )
    # 信封契约与 model_http_post 一致：provider_call_id 是 root id（内核传给
    # 物理 attempt 的 context 是 attempt 级派生 id），attempts 由内核结果填充。
    return ModelHttpStreamResponse(
        status_code=stream.status_code,
        headers=stream.headers,
        provider_call_id=context.provider_call_id,
        attempts=result.attempts,
        data_count=stream.data_count,
        provider_request_id=stream.provider_request_id,
    )


def build_model_http_client(
    *,
    base_url: str = "",
    headers: Mapping[str, str] | None = None,
    timeout: float = 30.0,
    transport: httpx.BaseTransport | None = None,
) -> httpx.Client:
    """构造共享模型出网 client（httpx.Client 唯一白名单构造点）。

    连接池所有权归调用方：prompt_enhance/judge 持有返回值并保留既有
    close/dispose 语义；实际发送统一经 :class:`ModelHttpTransport`。
    """

    return httpx.Client(
        base_url=base_url.rstrip("/"),
        headers=dict(headers or {}),
        timeout=float(timeout),
        transport=transport,
    )


__all__ = [
    "ModelHttpError",
    "ModelHttpResponse",
    "ModelHttpStreamResponse",
    "ModelHttpStreamTransport",
    "build_model_http_client",
    "model_http_post",
    "model_http_stream_post",
    "new_provider_call_root_id",
]
