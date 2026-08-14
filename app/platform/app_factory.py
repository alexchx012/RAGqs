from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from time import perf_counter

from fastapi import FastAPI, Request
from sqlalchemy import text

from app.api.v1 import router as v1_router
from app.identity.service import IdentityAccessService

from . import runtime as platform_runtime_module
from .config import PlatformSettings, load_platform_settings
from .context import new_request_context
from .http_contract import register_exception_handlers
from .observability import ObservabilityMetricsError, ObservabilitySample, sample_success
from .runtime import PlatformRuntime, build_runtime


def create_platform_app(
    settings: PlatformSettings | None = None,
    *,
    runtime: PlatformRuntime | None = None,
) -> FastAPI:
    settings = settings or load_platform_settings()
    owns_runtime = runtime is None
    runtime = runtime or build_runtime(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        del app
        try:
            engine = runtime.resolve("database_engine")
            if engine is not None:
                with engine.connect() as connection:
                    connection.execute(text("SELECT 1"))
            # H3：数据库探活之后锁定/校验业务日历（时区冲突 → 503 拒启，不悄悄重写）。
            # 与 usage maintenance 通过模块 lookup 调用同一 helper，便于行为级验证。
            platform_runtime_module.ensure_business_calendar_locked(runtime)
            if settings.profile == "production":
                missing_variable_names: list[str] = []
                if not settings.evaluation.judge_base_url:
                    missing_variable_names.append("RAG_EVALUATION_JUDGE_BASE_URL")
                judge_api_key = settings.evaluation.judge_api_key
                if judge_api_key is None or not judge_api_key.get_secret_value():
                    missing_variable_names.append("RAG_EVALUATION_JUDGE_API_KEY")
                if missing_variable_names:
                    startup_alert_port = runtime.resolve("startup_configuration_alert_port")
                    if startup_alert_port is not None:
                        with engine.begin() as connection:
                            startup_alert_port.publish_missing_evaluation_judge_configuration(
                                missing_variable_names=tuple(missing_variable_names),
                                occurred_at=datetime.now(UTC),
                                connection=connection,
                            )
            if settings.auth.admin_roster:
                identity_access = runtime.resolve("identity_access")
                if isinstance(identity_access, IdentityAccessService):
                    identity_access.reconcile_admin_roster()
            yield
        finally:
            if owns_runtime:
                runtime.close()

    app = FastAPI(
        title="RAGqs Core Platform",
        version="1.0.0",
        lifespan=lifespan,
        openapi_url="/v1/openapi.json",
        docs_url="/v1/docs",
        redoc_url=None,
        swagger_ui_oauth2_redirect_url="/v1/docs/oauth2-redirect",
    )
    app.state.platform_runtime = runtime
    register_exception_handlers(app)
    app.include_router(v1_router, prefix="/v1")

    @app.middleware("http")
    async def install_request_context(request: Request, call_next):
        context = new_request_context()
        started = perf_counter()
        response = None
        with context:
            try:
                response = await call_next(request)
            finally:
                metrics = runtime.resolve("observability_metrics")
                if metrics is not None:
                    status_code = response.status_code if response is not None else 500
                    route = getattr(request.scope.get("route"), "path", "other")
                    outcome_class = _outcome_class(status_code)
                    selected, sample_weight = (
                        sample_success(context.request_id, metrics.success_sample_rate)
                        if outcome_class == "success"
                        else (True, 1.0)
                    )
                    if selected:
                        try:
                            await asyncio.to_thread(
                                metrics.record,
                                ObservabilitySample(
                                    observed_at_utc=context.started_at_utc,
                                    route_template=route,
                                    method=request.method,
                                    outcome_class=outcome_class,
                                    status_family=f"{status_code // 100}xx",
                                    latency_ms=max(0, int((perf_counter() - started) * 1000)),
                                    sample_weight=sample_weight,
                                ),
                            )
                        except ObservabilityMetricsError:
                            # A telemetry write must not turn an otherwise valid API response into a failure.
                            pass
        assert response is not None
        response.headers["X-Request-Id"] = context.request_id
        return response

    return app


def _outcome_class(status_code: int) -> str:
    if 200 <= status_code < 400:
        return "success"
    if status_code == 401:
        return "authentication_error"
    if status_code == 403:
        return "authorization_error"
    if 400 <= status_code < 500:
        return "validation_error"
    if status_code == 504:
        return "gateway_timeout"
    if status_code >= 500:
        return "server_error"
    return "other"
