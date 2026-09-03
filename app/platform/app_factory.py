from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from importlib.metadata import version as package_version
from pathlib import Path
from time import perf_counter

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.routing import APIRoute
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from starlette.routing import Match
from starlette.types import Scope

from app.api.v1 import router as v1_router
from app.identity.service import IdentityAccessService
from app.indexing.observability import INDEX_INTERNAL_OBSERVABILITY_ROUTES
from app.indexing.retrieval import SPARSE_EXACT_MATCH_ROUTE

from . import runtime as platform_runtime_module
from .config import (
    PlatformConfigurationError,
    PlatformSettings,
    load_platform_settings,
    validate_startup_settings,
)
from .context import new_request_context
from .errors import PlatformError, map_exception
from .http_contract import (
    compatibility_error_payload,
    register_exception_handlers,
    request_error_payload,
)
from .observability import ObservabilityMetricsError, ObservabilitySample, sample_success
from .runtime import PlatformRuntime, build_runtime

logger = logging.getLogger(__name__)

_STATIC_DIRECTORY = Path(__file__).resolve().parents[2] / "static"
_SPA_RESERVED_PATHS = ("/v1", "/static")


class _SpaFallbackRoute(APIRoute):
    def matches(self, scope: Scope) -> tuple[Match, Scope]:
        path = scope.get("path", "")
        if _is_spa_reserved_path(path):
            return Match.NONE, {}
        return super().matches(scope)


def _is_spa_reserved_path(path: str) -> bool:
    return any(path == prefix or path.startswith(f"{prefix}/") for prefix in _SPA_RESERVED_PATHS)


def _static_spa_route_template(path: str, method: str, *, installed: bool) -> str | None:
    if not installed:
        return None
    if path == "/static" or path.startswith("/static/"):
        return "/static"
    if method == "GET" and not _is_spa_reserved_path(path):
        return "/{full_path:path}"
    return None


def _install_static_spa(app: FastAPI) -> bool:
    index_file = _STATIC_DIRECTORY / "index.html"
    if not _STATIC_DIRECTORY.is_dir() or not index_file.is_file():
        return False

    app.mount("/static", StaticFiles(directory=str(_STATIC_DIRECTORY)), name="static")

    async def serve_spa(full_path: str) -> FileResponse:
        del full_path
        return FileResponse(index_file)

    app.router.add_api_route(
        "/{full_path:path}",
        serve_spa,
        methods=["GET"],
        include_in_schema=False,
        name="spa-fallback",
        route_class_override=_SpaFallbackRoute,
    )
    return True


def create_platform_app(
    settings: PlatformSettings | None = None,
    *,
    runtime: PlatformRuntime | None = None,
) -> FastAPI:
    settings = settings or load_platform_settings()
    if settings.profile == "production":
        try:
            validate_startup_settings(settings)
        except PlatformConfigurationError:
            # Production preflight rejected the configuration. When the judge
            # settings are the reason, ring the ops bell before dying (best
            # effort; the alert must never mask the startup failure).
            platform_runtime_module.publish_missing_evaluation_judge_configuration_alert(
                settings, runtime
            )
            raise
    # RAG_LOG_LEVEL：无 handler 时安装默认 handler，并始终把根 logger 调到配置级别
    # （uvicorn 已配置 handler 时 basicConfig 不生效，setLevel 仍然生效）。
    logging.basicConfig(level=settings.logging.level)
    logging.getLogger().setLevel(settings.logging.level)
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
            if settings.auth.admin_roster:
                identity_access = runtime.resolve("identity_access")
                if isinstance(identity_access, IdentityAccessService):
                    identity_access.reconcile_admin_roster()
            yield
        finally:
            if owns_runtime:
                runtime.close()

    # 应用版本与 pyproject 单一来源（安装元数据）；production 不暴露 API schema
    # 与文档 UI（404），development 保持可达。
    expose_api_docs = settings.profile != "production"
    app = FastAPI(
        title="RAGqs Core Platform",
        version=package_version("ragqs-core-platform"),
        lifespan=lifespan,
        openapi_url="/v1/openapi.json" if expose_api_docs else None,
        docs_url="/v1/docs" if expose_api_docs else None,
        redoc_url=None,
        swagger_ui_oauth2_redirect_url="/v1/docs/oauth2-redirect" if expose_api_docs else None,
    )
    app.state.platform_runtime = runtime
    register_exception_handlers(app)
    app.include_router(v1_router, prefix="/v1")
    static_spa_installed = _install_static_spa(app)
    metrics = runtime.resolve("observability_metrics")
    configure_route_templates = getattr(metrics, "configure_route_templates", None)
    route_template_by_route_id: dict[int, str] = {}
    if callable(configure_route_templates):
        route_templates: list[str] = []
        for route in app.routes:
            route_path = getattr(route, "path", None)
            if isinstance(route_path, str):
                route_templates.append(route_path)
                route_template_by_route_id[id(route)] = route_path
            effective_route_contexts = getattr(route, "effective_route_contexts", None)
            if callable(effective_route_contexts):
                for context in effective_route_contexts():
                    context_path = getattr(context, "path", None)
                    if isinstance(context_path, str):
                        route_templates.append(context_path)
                        original_route = getattr(context, "original_route", None)
                        if original_route is not None:
                            route_template_by_route_id[id(original_route)] = context_path
        # Internal sparse exact-match sampling rides the same read path as API
        # routes; keep its template registered alongside them.
        route_templates.append(SPARSE_EXACT_MATCH_ROUTE)
        route_templates.extend(INDEX_INTERNAL_OBSERVABILITY_ROUTES)
        configure_route_templates(route_templates)

    @app.middleware("http")
    async def install_request_context(request: Request, call_next):
        context = new_request_context()
        started = perf_counter()
        response = None
        # Count admitted business writes so the backup worker can drain them
        # before moving the write gate from closing to closed (Q7).
        write_tracker = runtime.resolve("backup_write_tracker")
        tracked_write = write_tracker is not None and _write_gate_applies(request)
        if tracked_write:
            write_tracker.inc()
        with context:
            try:
                _reject_when_reads_closed(request, runtime)
                _reject_writes_during_backup(request, runtime)
                response = await call_next(request)
            except Exception as exc:
                error = map_exception(exc)
                logger.exception(
                    "Unhandled request exception", extra={"request_id": context.request_id}
                )
                response = JSONResponse(
                    (
                        compatibility_error_payload(error, context.request_id)
                        if request.url.path == "/v1/chat"
                        else request_error_payload(error, context.request_id)
                    ),
                    status_code=error.status_code,
                )
            finally:
                if tracked_write:
                    write_tracker.dec()
                metrics = runtime.resolve("observability_metrics")
                if metrics is not None:
                    status_code = response.status_code if response is not None else 500
                    matched_route = request.scope.get("route")
                    route = _static_spa_route_template(
                        request.url.path,
                        request.method,
                        installed=static_spa_installed,
                    )
                    if route is None:
                        route = route_template_by_route_id.get(
                            id(matched_route), getattr(matched_route, "path", "other")
                        )
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
        # 全局安全响应头：成功、错误与静态资源响应统一携带。
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
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


# Routes that stay available while the maintenance gate closes reads during a
# restore (design §2.8): health, metrics and ops maintenance endpoints.
_READ_GATE_EXEMPT_PREFIXES: tuple[str, ...] = ("/v1/health", "/v1/metrics", "/v1/ops")

# Routes that stay writable while the backup write gate is closing/closed
# (Q7): health, metrics, the ops backup/restore commands themselves and auth
# (operators must still be able to log in to monitor or unblock a backup).
_WRITE_GATE_EXEMPT_PREFIXES: tuple[str, ...] = (
    "/v1/health",
    "/v1/metrics",
    "/v1/ops",
    "/v1/auth",
)

_WRITE_GATE_SAFE_METHODS: frozenset[str] = frozenset({"GET", "HEAD", "OPTIONS"})


def _reject_when_reads_closed(request: Request, runtime: PlatformRuntime) -> None:
    path = request.url.path
    if any(path.startswith(prefix) for prefix in _READ_GATE_EXEMPT_PREFIXES):
        return
    gate_reader = runtime.resolve("maintenance_gate_reader")
    if gate_reader is None or not gate_reader.reads_closed():
        return
    error = map_exception(
        PlatformError(
            "maintenance_mode",
            "Reads are closed during a restore",
            {},
            503,
        )
    )
    raise error


def _write_gate_applies(request: Request) -> bool:
    if request.method in _WRITE_GATE_SAFE_METHODS:
        return False
    path = request.url.path
    return not any(path.startswith(prefix) for prefix in _WRITE_GATE_EXEMPT_PREFIXES)


def _reject_writes_during_backup(request: Request, runtime: PlatformRuntime) -> None:
    if not _write_gate_applies(request):
        return
    gate_reader = runtime.resolve("backup_write_gate_reader")
    if gate_reader is None or gate_reader.writes_open():
        return
    error = map_exception(
        PlatformError(
            "backup_in_progress",
            "Writes are paused while a backup is in progress",
            {},
            503,
        )
    )
    raise error
