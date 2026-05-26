"""FastAPI 应用入口"""

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from loguru import logger

from app.api import chat, file, health
from app.config import config
from app.core.milvus_client import milvus_manager
from app.ingestion.worker import get_background_indexing_worker
from app.observability import install_request_context_middleware
from app.security import build_cors_options


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info(f"🚀 {config.app_name} v{config.app_version} 启动中...")
    milvus_manager.connect()
    logger.info("✅ Milvus 连接成功")
    indexing_worker = None
    if _config_id(getattr(config, "indexing_execution_mode", "sync")) == "background":
        indexing_worker = get_background_indexing_worker(settings=config)
        indexing_worker.start()
        logger.info("✅ 后台索引 worker 已启动")
    try:
        yield
    finally:
        if indexing_worker is not None:
            stopped = indexing_worker.stop(
                timeout_seconds=float(
                    getattr(config, "indexing_worker_shutdown_timeout_seconds", 5.0)
                )
            )
            if stopped:
                logger.info("✅ 后台索引 worker 已停止")
            else:
                logger.warning("后台索引 worker 未在超时时间内停止")
        milvus_manager.close()
        logger.info(f"👋 {config.app_name} 关闭")


app = FastAPI(
    title=config.app_name,
    version=config.app_version,
    description="纯 RAG 知识库问答 Agent",
    lifespan=lifespan,
)

install_request_context_middleware(app)

app.add_middleware(
    CORSMiddleware,
    **build_cors_options(config),
)

app.include_router(health.router, tags=["健康检查"])
app.include_router(chat.router, prefix="/api", tags=["对话"])
app.include_router(file.router, prefix="/api", tags=["文件管理"])

static_dir = "static"
if os.path.isdir(static_dir):
    app.mount("/static", StaticFiles(directory=static_dir), name="static")


@app.get("/")
async def root():
    index_path = os.path.join(static_dir, "index.html")
    if os.path.exists(index_path):
        return FileResponse(index_path)
    return {"message": f"{config.app_name} API", "docs": "/docs"}


def _config_id(value: str) -> str:
    return str(value).strip().lower().replace("-", "_")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host=config.host, port=config.port, reload=config.debug)
