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

## 开发环境状态存储

默认配置使用本地 SQLite 保存会话、检索审计、索引队列、索引任务、文档目录和 LangGraph checkpoint，数据库文件位于 `data/*.sqlite3`。保持 `.env.example` 中的 `SESSION_STORE_PROVIDER=sqlite`、`RETRIEVAL_AUDIT_STORE_PROVIDER=sqlite`、`INDEXING_QUEUE_PROVIDER=sqlite`、`INDEXING_JOB_STORE_PROVIDER=sqlite`、`DOCUMENT_CATALOG_PROVIDER=sqlite` 和 `CHECKPOINT_PROVIDER=sqlite` 即可获得可重启的本地开发状态。`memory` provider 仅用于显式配置的临时测试，不作为开发默认数据库。

## 内部认证与知识空间权限

本地默认 `AUTH_ENABLED=false`，会以 `local-admin` 兼容既有开发流程。内部试运行应设置 `AUTH_ENABLED=true`，先用 `AUTH_PROVIDER=dev_header` 和 `AUTH_DEV_USERS=alice:viewer|uploader:hr|finance;bob:admin:*` 验证权限路径；接入企业 SSO/OIDC 或反向代理时使用 `AUTH_PROVIDER=reverse_proxy`，由网关把身份、角色和知识空间映射到 `X-RAG-User`、`X-RAG-Roles`、`X-RAG-Spaces`。

后端会在 chat、upload、knowledge-space、document lifecycle、index-job 和 retrieval audit API 统一检查权限与知识空间访问，不能只依赖客户端传入的 `spaceId` 做隔离。

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
用户问题 → LangGraph Agent → retrieve_knowledge 工具 → Milvus 向量检索 → LLM 生成答案
```

核心组件：
- `app/services/rag_agent_service.py` — LangGraph Agent（会话管理 + 工具调用）
- `app/tools/knowledge_tool.py` — 知识库检索工具
- `app/services/vector_store_manager.py` — Milvus 向量存储管理
- `app/services/document_splitter_service.py` — Markdown/文本智能分割
- `app/services/vector_index_service.py` — 文档索引流程

## 技术栈

- FastAPI + LangChain + LangGraph
- 通义千问 (DashScope) — LLM + Embedding
- Milvus — 向量数据库
- SSE — 流式输出
