"""FastAPI 应用入口"""

import os
from collections.abc import Callable
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from loguru import logger

from app.api import admin_users, auth, chat, file, health, metrics
from app.config import config
from app.core.milvus_client import milvus_manager as default_milvus_manager
from app.ingestion.worker import get_background_indexing_worker
from app.observability import install_request_context_middleware
from app.operations.health import create_default_health_checker
from app.security import build_cors_options
from app.security.runtime_controls import install_runtime_controls_middleware


# 构造 FastAPI lifespan 回调；参数注入让测试可以替换真实配置、Milvus 和索引 worker。
def create_lifespan(
    *,
    settings: Any = config,
    milvus_manager: Any = default_milvus_manager,
    indexing_worker_factory: Callable[..., Any] = get_background_indexing_worker,
) -> Callable[[FastAPI], Any]:
    """创建 FastAPI 启动/关闭生命周期函数。

    这是应用入口装配层代码，不直接启动服务，也不写具体业务逻辑。
    它把 settings、milvus_manager 和 indexing_worker_factory 封进闭包，
    返回给 FastAPI 一个 lifespan(app) 函数。FastAPI 启动时执行 yield
    前的连接 Milvus / 启动后台索引 worker 逻辑，关闭时执行 finally 里的
    worker 停止、Milvus 关闭和日志清理逻辑。
    """

    # FastAPI 实际执行的生命周期函数：yield 前启动资源，finally 中按相反方向关闭资源。
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app_name = _settings_value(settings, "app", "name", "app_name", "RAG Knowledge Agent")
        app_version = _settings_value(settings, "app", "version", "app_version", "1.0.0")
        logger.info(f"🚀 {app_name} v{app_version} 启动中...")
        vector_store_provider = _config_id(
            _settings_value(
                settings,
                "providers",
                "vector_store",
                "vector_store_provider",
                "milvus",
            )
        )
        milvus_connected = False
        if vector_store_provider == "milvus":
            milvus_manager.connect()
            milvus_connected = True
            logger.info("✅ Milvus 连接成功")
        else:
            logger.info(f"跳过 Milvus 连接, vector_store_provider={vector_store_provider}")
        indexing_worker = None
        if (
            _config_id(
                _settings_value(
                    settings,
                    "storage",
                    "indexing_execution_mode",
                    "indexing_execution_mode",
                    "sync",
                )
            )
            == "background"
        ):
            indexing_worker = indexing_worker_factory(settings=settings)
            indexing_worker.start()
            logger.info("✅ 后台索引 worker 已启动")
        auth_provider = _config_id(
            _settings_value(
                settings,
                "auth",
                "provider",
                "auth_provider",
                "dev_header",
            )
        )
        if auth_provider == "local_credentials":
            from app.security.local_auth_service import get_local_auth_service

            get_local_auth_service(settings).seed_initial_admin()
            logger.info("✅ 本地账号种子检查完成")
        try:
            yield
        finally:
            if indexing_worker is not None:
                stopped = indexing_worker.stop(
                    timeout_seconds=float(
                        _settings_value(
                            settings,
                            "storage",
                            "indexing_worker_shutdown_timeout_seconds",
                            "indexing_worker_shutdown_timeout_seconds",
                            5.0,
                        )
                    )
                )
                if stopped:
                    logger.info("✅ 后台索引 worker 已停止")
                else:
                    logger.warning("后台索引 worker 未在超时时间内停止")
            if milvus_connected:
                milvus_manager.close()
            logger.info(f"👋 {app_name} 关闭")

    return lifespan


# 组装完整 FastAPI 应用实例：安装中间件、挂载路由和静态目录，但不直接启动服务。
def create_app(
    *,
    settings: Any = config,
    milvus_manager: Any = default_milvus_manager,
    indexing_worker_factory: Callable[..., Any] = get_background_indexing_worker,
    static_dir: str = "static",
) -> FastAPI:
    """创建并装配 FastAPI 应用实例。

    这是应用入口装配层：创建 FastAPI application，安装生命周期函数、
    请求上下文 middleware、运行时控制 middleware、CORS、健康检查路由、
    对话/文件/指标 API 路由、静态资源挂载和首页 fallback。这里不直接实现
    RAG 检索、上传安全扫描或向量入库；这些行为由 API、service、graph、
    provider 等下游模块负责。返回值是 Uvicorn 可加载的 FastAPI app。
    """

    app_name = _settings_value(settings, "app", "name", "app_name", "RAG Knowledge Agent")
    app_version = _settings_value(settings, "app", "version", "app_version", "1.0.0")
    application = FastAPI(
        title=app_name,
        version=app_version,
        description="纯 RAG 知识库问答 Agent",
        lifespan=create_lifespan(
            settings=settings,
            milvus_manager=milvus_manager,
            indexing_worker_factory=indexing_worker_factory,
        ),
    )

    install_request_context_middleware(application)
    install_runtime_controls_middleware(application, settings=settings)

    application.add_middleware(
        CORSMiddleware,
        **build_cors_options(settings),
    )

    health_checker = create_default_health_checker(
        settings=settings,
        milvus_manager=milvus_manager,
    )
    application.include_router(
        health.create_health_router(health_checker),
        tags=["健康检查"],
    )
    application.include_router(auth.router, prefix="/api", tags=["认证"])
    application.include_router(admin_users.router, prefix="/api", tags=["管理员用户"])
    application.include_router(chat.router, prefix="/api", tags=["对话"])
    application.include_router(file.router, prefix="/api", tags=["文件管理"])
    application.include_router(metrics.router, prefix="/api", tags=["运行指标"])

    if os.path.isdir(static_dir):
        application.mount("/static", StaticFiles(directory=static_dir), name="static")

    index_path = os.path.join(static_dir, "index.html")
    index_exists = os.path.isfile(index_path)

    # 处理浏览器访问根路径：优先返回静态首页，缺失时退回轻量 API 信息。
    @application.get("/")
    async def root():
        if index_exists:
            return FileResponse(index_path)
        return {"message": f"{app_name} API", "docs": "/docs"}

    # SPA deep-link fallback: BrowserRouter paths must serve index.html on refresh.
    # Do not swallow API, docs, OpenAPI, metrics, health, or static asset requests.
    if index_exists:
        _SPA_RESERVED_ROOTS = frozenset(
            {"api", "docs", "redoc", "openapi.json", "metrics", "health", "static"}
        )

        @application.get("/{full_path:path}")
        async def spa_fallback(full_path: str):
            root_segment = full_path.split("/", 1)[0]
            if root_segment in _SPA_RESERVED_ROOTS:
                raise HTTPException(status_code=404, detail="Not Found")
            # Missing built assets (e.g. *.js) should 404, not fall back to HTML.
            leaf = full_path.rsplit("/", 1)[-1]
            if "." in leaf:
                raise HTTPException(status_code=404, detail="Not Found")
            return FileResponse(index_path)

    return application


# 为直接运行本文件时的 uvicorn 启动提供参数，集中复用 app 组配置和旧平铺字段。
def build_uvicorn_options(settings: Any = config) -> dict[str, Any]:
    """从配置中构造 Uvicorn 启动参数。

    返回的字典提供 host、port 和 reload，供直接运行本文件时的
    uvicorn.run(...) 使用。它只负责服务器监听参数，不改变 FastAPI
    路由或 RAG 业务行为。
    """

    return {
        "host": _settings_value(settings, "app", "host", "host", "0.0.0.0"),
        "port": int(_settings_value(settings, "app", "port", "port", 9900)),
        "reload": bool(_settings_value(settings, "app", "debug", "debug", False)),
    }


# 将配置中的 provider 或模式标识归一化，避免大小写和连字符差异影响分支判断。
def _config_id(value: str) -> str:
    return str(value).strip().lower().replace("-", "_")


# 兼容分组配置和旧平铺字段：优先读取 settings.<group>.<field>，缺失时回退到平铺字段。
def _settings_value(
    settings: Any,
    group_name: str,
    group_field_name: str,
    flat_field_name: str,
    default: Any,
) -> Any:
    group = getattr(settings, group_name, None)
    if group is not None and hasattr(group, group_field_name):
        return getattr(group, group_field_name)
    return getattr(settings, flat_field_name, default)


app = create_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", **build_uvicorn_options(config))
