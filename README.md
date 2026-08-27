# RAGqs Core Platform

这是 RAGqs 新后端的绿地平台基座。同一 FastAPI 端口发布 `/v1` API、`/static/*` 前端构建产物和 SPA 路由 fallback；PostgreSQL 与 S3 兼容对象存储是事实源，索引与缓存由后续领域服务作为可重建派生数据接入。

## 配置

所有 API、worker、维护任务使用 `RAG_` 前缀的严格配置。最低配置见 `.env.example`。生产 profile 只接受 PostgreSQL、HTTPS 对象存储、非 fake provider、关闭 debug 的组合；旧环境变量和 tenant 参数会在启动前被拒绝。Outbox 通知与已投递事件的 retention 配置必须是正整数，默认分别为 90 天和 30 天；配置值在后续通知物化或投递完成时冻结，修改配置不会重算既有 retire/compact 时间。

## 本地运行

使用项目根目录的 `.venv`：

```powershell
$env:RAG_PLATFORM_PROFILE = "development"
$env:RAG_DATABASE_URL = "postgresql+psycopg://ragqs:ragqs@localhost:5432/ragqs"
$env:RAG_OBJECT_STORAGE_ENDPOINT = "http://127.0.0.1:9000"
$env:RAG_OBJECT_STORAGE_BUCKET = "ragqs"
$env:RAG_OBJECT_STORAGE_ACCESS_KEY = "minioadmin"
$env:RAG_OBJECT_STORAGE_SECRET_KEY = "change-me"
$env:RAG_PROVIDER_NAME = "fake"
python -m alembic upgrade head
python -m uvicorn app.main:app --host 127.0.0.1 --port 9900
```

上述 development 配置要求 S3 兼容对象存储在该 endpoint 可用；完整环境变量和生产约束见 `.env.example`。

浏览器入口位于 `http://127.0.0.1:9900/`，健康检查位于 `/v1/health`，OpenAPI 文档位于 `/v1/docs`。发布包未包含 `static/index.html` 时，应用保持纯 API 模式并正常启动。

## 验证

```powershell
python -m pytest tests/platform -q
python -m ruff check app alembic tests
python -m black --check app alembic tests
python -m isort --check-only app alembic tests
python -m mypy app
```

配置 `RAGQS_TEST_POSTGRES_URL` 和 `RAGQS_TEST_S3_*` 后，集成测试会验证空 PostgreSQL 迁移、API、worker 与 S3 兼容对象存储。GitHub Actions 将这些服务作为合并门禁启动。

usage-quota 的 PostgreSQL 破坏性验收（`tests/usage/test_concurrency_postgres.py`）要求**全部**门槛齐备，缺任一条件整组 skip 且报告记为 PostgreSQL mandatory acceptance `NOT RUN/BLOCKED`：`RAGQS_TEST_POSTGRES_URL`（backend 必须为 `postgresql`）、`RAGQS_ALLOW_DESTRUCTIVE_POSTGRES_TESTS=1`（显式 destructive opt-in）、数据库名须包含 `test`（无旁路开关）。
