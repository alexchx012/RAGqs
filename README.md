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
