# RAG 知识库问答 Agent

纯 RAG 知识库问答系统，基于 LangChain + LangGraph + Milvus 向量检索 + 通义千问 LLM。

## 快速开始

```bash
# 1. 安装依赖
pip install uv
uv venv && source .venv/bin/activate  # Windows: .venv\Scripts\activate
uv pip install -e .

# 2. 配置环境变量
cp .env.example .env
# 编辑 .env，填入 DASHSCOPE_API_KEY

# 开发环境运行态状态默认写入本地 SQLite：
# SESSION_STORE_PROVIDER=sqlite
# INDEXING_QUEUE_PROVIDER=sqlite
# CHECKPOINT_PROVIDER=sqlite
# SQLite 文件默认位于 data/*.sqlite3

# 3. 一键启动（默认启动/检查 Milvus，退出时不停止数据库）
.\start.ps1

# 如需退出时同时停止 Milvus：
.\start.ps1 -StopMilvusOnExit

# 只做启动前检查并确认 Milvus 健康，不拉起 FastAPI：
.\start.ps1 -PreflightOnly

# 手动启动方式：
docker compose -f vector-database.yml up -d
python -m uvicorn app.main:app --host 0.0.0.0 --port 9900 --reload

# 5. 上传知识库文档
# 将 .md 或 .txt 文件通过 Web 界面上传，或使用 API：
curl -X POST http://localhost:9900/api/upload -F "file=@your-doc.md"
```

## 访问

- Web 界面: http://localhost:9900
- API 文档: http://localhost:9900/docs

## 工程化优化与技术特点

本项目最终目标为可复用的 RAG 基座系统，重点优化在依赖解耦、检索质量、入库生命周期、权限隔离、可观测性和试运行门禁。

- **Provider 解耦**：LLM、Embedding、VectorStore、Retriever、SessionStore、RetrievalAuditStore、Ingestion 和 LangGraph Checkpoint 都通过 `app/providers/contracts.py` 定义边界，并由 `app/providers/factory.py` 统一装配；默认使用 DashScope + Milvus + SQLite，也支持 OpenAI-compatible、fake provider 和 Postgres-backed store。
- **显式 LangGraph 编排**：默认 `AGENT_RUNTIME=explicit_graph`，通过 `app/agents/rag_graph.py` 将输入规范化、检索决策、检索、无依据拒答、工具调用、答案生成、错误策略和最终响应拆成稳定节点，避免把核心问答流程完全藏在黑盒 Agent 内。
- **结构化检索 Pipeline**：`app/retrieval/pipeline.py` 支持 query rewrite、向量检索、多 retriever 分支、去重、rerank、context compression、引用来源生成和阶段耗时 debug；`high_recall` profile 可扩大 top-k 并提供 relaxed-filter fallback，同时保留知识空间和租户隔离键。
- **安全且可追踪的数据入库**：上传阶段会校验扩展名、大小、UTF-8、路径逃逸和高风险 prompt 注入片段；索引阶段会生成稳定 `document_id`、`chunk_id`、`content_hash`、heading path，并通过 indexing job 和 document catalog 记录状态，支持删除、重建和失败重试。
- **知识空间与权限隔离**：chat、upload、session、retrieval audit、knowledge-space、document lifecycle 和 index-job API 都在后端检查角色权限和知识空间访问，不能只依赖前端传入的 `spaceId` 做隔离。
- **可重启的开发运行态**：默认用 SQLite 保存会话、检索审计、索引队列、索引任务、文档目录和 checkpoint；需要多实例共享数据层时，可将这些 store 切换到 Postgres，并通过 smoke 命令验证配置和写入路径。
- **可观测性内建**：请求中间件会分配并回传 `X-Trace-Id`，输出结构化 access log；运行时指标覆盖 HTTP 请求、RAG 查询、知识空间分布、延迟桶和 token usage，并提供 JSON 与 Prometheus 格式。
- **运维与试运行门禁**：提供启动配置校验、provider-aware `/health`、集成 smoke、Postgres smoke、fake load 和 evaluation preflight。fake provider 与 preflight 只证明软件路径、配置边界和接口形态可运行，不声明真实业务答案质量已经验证。
- **评测基座**：`app/evaluation/` 支持 golden JSONL、fake/service/http 三种评测模式、answer trait/source/refusal/faithfulness 指标、模型裁判和 JSON 报告，适合作为后续业务 RAG 质量验收的基础。

## 开发环境状态存储

默认配置使用本地 SQLite 保存会话、检索审计、索引队列、索引任务、文档目录和 LangGraph checkpoint，数据库文件位于 `data/*.sqlite3`。保持 `.env.example` 中的 `SESSION_STORE_PROVIDER=sqlite`、`RETRIEVAL_AUDIT_STORE_PROVIDER=sqlite`、`INDEXING_QUEUE_PROVIDER=sqlite`、`INDEXING_JOB_STORE_PROVIDER=sqlite`、`DOCUMENT_CATALOG_PROVIDER=sqlite` 和 `CHECKPOINT_PROVIDER=sqlite` 即可获得可重启的本地开发状态。`memory` provider 仅用于显式配置的临时测试，不作为开发默认数据库。

## 内部认证与知识空间权限

本地默认 `AUTH_ENABLED=false`，会以 `local-admin` 兼容既有开发流程。内部试运行应设置 `AUTH_ENABLED=true`，先用 `AUTH_PROVIDER=dev_header` 和 `AUTH_DEV_USERS=alice:viewer|uploader:hr|finance;bob:admin:*` 验证权限路径；接入企业 SSO/OIDC 或反向代理时使用 `AUTH_PROVIDER=reverse_proxy`，由网关把身份、角色和知识空间映射到 `X-RAG-User`、`X-RAG-Roles`、`X-RAG-Spaces`。

后端会在 chat、upload、knowledge-space、document lifecycle、index-job 和 retrieval audit API 统一检查权限与知识空间访问，不能只依赖客户端传入的 `spaceId` 做隔离。

## 内部试运行并发边界

默认 `RUNTIME_CONTROLS_ENABLED=false`，避免影响本地开发。内部试运行前可启用进程内请求控制：

```env
RUNTIME_CONTROLS_ENABLED=true
RUNTIME_MAX_CONCURRENT_REQUESTS=40
RUNTIME_QUEUE_TIMEOUT_SECONDS=2.0
RUNTIME_REQUEST_TIMEOUT_SECONDS=60.0
```

使用 fake provider 验证 API 并发、超时和错误响应路径：

```powershell
.\scripts\run-fake-load.ps1 -ApiUrl http://127.0.0.1:9900 -Concurrency 20 -Requests 40 -Json
```

该命令只证明软件路径可运行；不证明真实业务答案质量或多实例生产数据层已经验证。

## 试运行前门禁

评测基座用于验证 golden dataset schema、provider 边界、报告 JSON 和 CI artifact 输出。默认 fake evaluation 只证明评测软件接口可运行，不证明真实业务答案质量：

```powershell
.\scripts\run-evaluation.ps1
.\scripts\run-evaluation.ps1 -Dataset data\evaluation\business.example.jsonl -Mode service -FaithfulnessJudge model -PreflightOnly -MinExamples 6
```

多实例数据层在本仓库中只提供配置、初始化边界和 smoke/preflight 命令；尚未完成真实多实例生产数据行为验证。试运行前将 session、retrieval audit、indexing queue、indexing job、document catalog 和 checkpoint 全部切到 Postgres 后运行：

```powershell
.\scripts\run-postgres-smoke.ps1 -RequireConfigured -ValidateWritePath -Json
```

## API

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/chat` | 知识库问答（非流式） |
| POST | `/api/chat_stream` | 知识库问答（SSE 流式） |
| POST | `/api/upload` | 上传文档到知识库 |
| POST | `/api/chat/clear` | 清空会话 |
| GET | `/api/chat/session/{id}` | 查看会话历史 |
| GET | `/health` | 健康检查 |

## 架构

```
用户问题
  → FastAPI chat / chat_stream API
  → 权限与知识空间检查
  → LangGraph 显式 RAG 状态图
  → RetrievalPipeline / retrieve_knowledge
  → Milvus 向量检索 + sources/debug
  → LLM 基于检索上下文生成答案
  → 会话存储、检索审计、指标与 trace 输出
```

核心组件：
- `app/agents/rag_graph.py` — 显式 LangGraph RAG 状态图
- `app/services/rag_agent_service.py` — RAG 会话、流式输出、trace、审计和指标记录
- `app/retrieval/pipeline.py`、`app/retrieval/profiles.py` — 检索增强、profile、去重、引用和 debug
- `app/providers/` — 模型、Embedding、向量库、会话、审计、入库和 checkpoint provider 装配
- `app/tools/knowledge_tool.py` — 知识库检索工具和请求级知识空间约束
- `app/services/vector_store_manager.py`、`app/core/milvus_client.py` — Milvus collection、索引和向量存储管理
- `app/services/document_splitter_service.py`、`app/ingestion/` — 文档加载、切分、元数据标准化、索引任务和后台队列
- `app/knowledge/catalog.py` — 知识空间和文档生命周期目录
- `app/security/` — 上传安全、认证授权、CORS 和运行时并发控制
- `app/observability/`、`app/operations/`、`app/evaluation/` — trace、metrics、health、smoke、配置校验和评测基座

## 技术栈

- FastAPI + LangChain + LangGraph
- 通义千问 (DashScope) — LLM + Embedding
- Milvus — 向量数据库
- SSE — 流式输出
