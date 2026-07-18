# RAG 知识库问答 Agent

基于 **LangChain + LangGraph + Milvus + DeepSeek chat + DashScope embedding** 的纯 RAG 知识库问答系统。

> **状态**: 内部实验项目，尚未完成生产级验证。多实例数据层、真实业务答案质量等均未经过完整测试。

## Features

- **Provider 解耦** — Chat / Embedding / VectorStore / Session / Audit / Ingestion 全部通过 `app/providers/` 的 contract + factory 装配，支持 DeepSeek、DashScope、OpenAI-compatible、fake provider 和 Postgres-backed store 切换。
- **显式 LangGraph 编排** — `app/agents/rag_graph.py` 将检索决策、检索、拒答、工具调用、答案生成、错误处理拆成稳定节点，流程可追踪、可调试。
- **结构化检索 Pipeline** — `app/retrieval/pipeline.py` 支持 query rewrite、多 retriever、去重、rerank、context compression 和阶段耗时 debug。
- **知识空间与权限隔离** — chat / upload / session / audit / document lifecycle 均在服务端校验角色与 space 权限。
- **可观测性内建** — X-Trace-Id 追踪、结构化 access log、请求 / 延迟 / token 指标（JSON + Prometheus）。
- **评测基座** — `app/evaluation/` 支持 golden JSONL / fake / service / http 评测模式与 faithfulness 指标。

## Quick Start

```bash
# 1. 安装
pip install uv
uv venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
uv pip install -e .

# 2. 配置
cp .env.example .env
# 编辑 .env：填入 DEEPSEEK_API_KEY（默认 chat）与 DASHSCOPE_API_KEY（默认 embedding）

# 3. 一键启动（自动检查 / 启动 Milvus）
.\start.ps1

# 4. 上传文档
curl -X POST http://localhost:9900/api/upload -F "file=@your-doc.md"
```

- Web 界面: http://localhost:9900
- Swagger: http://localhost:9900/docs

更多启动选项：

```bash
.\start.ps1 -StopMilvusOnExit     # 退出时停止 Milvus
.\start.ps1 -PreflightOnly        # 仅做启动前健康检查

# 手动启动
docker compose -f vector-database.yml up -d
python -m uvicorn app.main:app --host 0.0.0.0 --port 9900 --reload
```

## Architecture

```
用户问题 → FastAPI chat / chat_stream API（权限检查）
         → LangGraph 显式 RAG 状态图
         → RetrievalPipeline（Milvus 向量检索 + sources / debug）
         → LLM 生成答案（基于检索上下文）
         → 会话存储 + 检索审计 + 指标 + trace
```

核心组件：

| 模块 | 职责 |
|------|------|
| `app/agents/rag_graph.py` | LangGraph 显式状态图 |
| `app/retrieval/pipeline.py` | 检索增强、去重、rerank、引用 |
| `app/providers/` | 模型 / Embedding / 向量库 / 会话 / 审计 / 入库装配 |
| `app/security/` | 认证授权、CORS、上传安全、运行时并发控制 |
| `app/observability/` / `app/operations/` | trace、metrics、health、smoke、配置校验 |
| `app/evaluation/` | 评测基座（golden dataset、模型裁判、faithfulness） |

## API

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/chat` | 知识库问答（非流式） |
| POST | `/api/chat_stream` | 知识库问答（SSE 流式） |
| POST | `/api/upload` | 上传文档到知识库 |
| POST | `/api/chat/clear` | 清空会话 |
| GET | `/api/chat/session/{id}` | 查看会话历史 |
| GET | `/health` | 健康检查 |

## Configuration & Ops

默认是 **DeepSeek chat + DashScope embedding** 混合装配：

```dotenv
# 默认聊天：CHAT_PROVIDER 留空时，若 DEEPSEEK_API_KEY 有效则自动选择 DeepSeek。
# 只有同时保存两个有效 chat Key 才触发 DeepSeek-first 自动顺序；只有一个有效 chat Key 时不比较优先级。
# DashScope chat、OpenAI-compatible chat 与 fake 均必须显式设置 CHAT_PROVIDER。
CHAT_MODEL=deepseek-v4-pro
DEEPSEEK_API_KEY=...

# 默认向量化：DashScope embedding（与 chat provider 独立）。
DASHSCOPE_API_KEY=...
DASHSCOPE_EMBEDDING_MODEL=text-embedding-v4
```

- **Chat 配置** — 所有 chat provider 共用必填 `CHAT_MODEL`；可选 `CHAT_PROVIDER`（`deepseek` / `dashscope` / `openai_compatible` / `fake`）。不再使用 `RAG_MODEL`、`DASHSCOPE_MODEL`、`OPENAI_COMPATIBLE_MODEL`。
- **状态存储** — 默认使用本地 SQLite（`data/*.sqlite3`），支持切换到 Postgres；`.env.example` 中设置 `SESSION_STORE_PROVIDER=sqlite`、`INDEXING_QUEUE_PROVIDER=sqlite`、`CHECKPOINT_PROVIDER=sqlite` 即可获得可重启的本地开发状态。
- **认证与权限** — 本地开发默认 `AUTH_ENABLED=false`，以 `local-admin` 身份运行。试运行可启用 `AUTH_ENABLED=true` 并选择 `dev_header` 或 `reverse_proxy` provider，role + space 权限在后端统一校验。**【破坏性变更】**：管理员角色字符串已从 `admin` 改名为 `super_admin`（新增部门范围受限的 `department_admin`），不提供运行时兼容别名；升级前请将 `AUTH_DEV_USERS`/`AUTH_DEFAULT_ROLES` 环境变量及反向代理下发的角色 Header 值同步改为 `super_admin`，否则相关账号会在升级后立即失去全部权限。
- **并发控制** — 可选启用 `RUNTIME_CONTROLS_ENABLED=true` 限制进程内并发请求数、排队超时和请求超时；通过 `.\scripts\run-fake-load.ps1` 验证 API 并发路径。
- **健康与 smoke 边界** — `/health` 与启动 preflight 只表示配置 / 依赖边界检查；真实 DeepSeek 调用需运行后续 DeepSeek smoke（opt-in，见 `docs/operations.md`）。配置完整 ≠ 真实 smoke 通过 ≠ 答案质量已验证。
- **DeepSeek 真实 smoke（opt-in）** — `tests/integration/test_deepseek_smoke.py` 仅在 `DEEPSEEK_SMOKE=1` 且 `DEEPSEEK_API_KEY` 为非占位密钥时执行；否则 `pytest.skip`（记为未执行 / SKIPPED，不得记为 PASS）。覆盖非流式文本、普通流、以及零副作用 `get_current_time` 的 thinking+tool-call 续接（首轮强制 tool call；次轮仅要最终公开回答，并尝试 `extra_body` 开启 thinking；`reasoning_content` 仅在 live 返回时断言，完整保真由单元测试覆盖）。输出不得打印密钥。
- **试运行门禁** — 评测基座默认 fake 模式只验证软件接口可运行；将 session / audit / indexing / checkpoint 全切到 Postgres 后，运行 `.\scripts\run-postgres-smoke.ps1` 和 `.\scripts\run-evaluation.ps1` 做端到端验证。
- **回滚** — 若本 change 引入不可接受回归，还原本 change 产生的提交即可；不涉及向量、数据库 schema 或公开 API 数据迁移。
- **更多信息** — 项目协作规则、测试命令和代码结构详见 `AGENTS.md`。

## Tech Stack

FastAPI · LangChain · LangGraph · DeepSeek (chat) · DashScope (embedding) · Milvus · SQLite / Postgres · React · Vite · TypeScript · SSE

## 前端构建

前端使用 React + Vite + TypeScript 构建，源码位于 `frontend/` 目录。

### 开发

```bash
cd frontend
npm install
npm run dev
```

开发服务器默认运行在 `http://localhost:5173`，API 请求自动代理到 `http://localhost:9900`（FastAPI 后端默认端口）。

### 生产构建

```bash
cd frontend
npm install
npm run build
```

构建产物输出到 `static/` 目录，由 FastAPI 的 `StaticFiles` 托管，无需额外配置。

### 测试

```bash
# 后端（默认不调用真实 DeepSeek）
.\.venv\Scripts\python.exe -m pytest -q

# DeepSeek 真实 smoke：无 DEEPSEEK_SMOKE=1 时必须 SKIPPED
.\.venv\Scripts\python.exe -m pytest tests/integration/test_deepseek_smoke.py -v

# 受控 live 运行（需有效 DEEPSEEK_API_KEY；切勿在日志中打印密钥）
# PowerShell: $env:DEEPSEEK_SMOKE='1'
# .\.venv\Scripts\python.exe -m pytest tests/integration/test_deepseek_smoke.py -v

cd frontend
npm test              # Vitest 单元测试
npx playwright test   # Playwright E2E 测试（需先启动后端）
```
