## Context

本 change 的审计对象是 `C:\Users\SNight\Desktop\RAGqs` 当前文件树。审计过程只读取代码和配置，业务代码未修改。项目当前是一个 FastAPI RAG 知识库问答服务，核心运行链路由 HTTP API、LangGraph Agent、provider 容器、Milvus 向量库、SQLite/Postgres 状态存储、知识空间权限、评测和部署脚本组成。

当前仓库已恢复 `docs/` 目录，包含 architecture、evaluation、operations、deployment、extension guide、business samples、templates 和 superpowers plans。`tests/test_phase0_baseline.py` 仍是确认这些文档存在并覆盖关键主题的测试入口；剩余风险从“目录缺失”转为“恢复后的文档内容需要与代码和 OpenSpec baseline 持续同步”。

### 当前项目结构

| 路径 | 作用 | 关键函数/类 |
| --- | --- | --- |
| `app/main.py` | FastAPI 应用入口、生命周期、路由挂载、静态文件挂载 | `create_app()`、`create_lifespan()`、`build_uvicorn_options()` |
| `app/api/chat.py` | 对话、流式对话、会话、检索审计 API | `chat()`、`chat_stream()`、`list_sessions()`、`list_retrieval_audits()` |
| `app/api/file.py` | 上传、知识空间、文档生命周期、索引任务 API | `upload_file()`、`create_knowledge_space()`、`delete_document()`、`retry_indexing_job()` |
| `app/api/health.py` | 健康检查路由 | `create_health_router()` |
| `app/api/metrics.py` | JSON / Prometheus 指标路由 | `create_metrics_router()` |
| `app/config.py` | Pydantic settings 和分组配置视图 | `Settings`、`AppConfig`、`ProviderConfig`、`StorageConfig`、`AgentConfig`、`RagConfig` |
| `app/providers/factory.py` | 进程级 provider 组合 | `ProviderContainer`、`create_default_provider_container()`、`get_default_provider_container()` |
| `app/providers/contracts.py` | provider 协议和核心数据结构 | `RetrievalRequest`、`RetrievalResult`、`RetrievalSource`、`IngestionResult`、各 Provider Protocol |
| `app/agents/rag_graph.py` | 显式 LangGraph RAG 状态图 | `RagGraphState`、`RagGraphNodes`、`build_rag_state_graph()`、`ChatModelAnswerGenerator` |
| `app/services/rag_agent_service.py` | RAG Agent 服务门面，会话、审计、指标记录 | `RagAgentService`、`query_with_trace()`、`query_stream_with_trace()`、`retrieve_context()` |
| `app/tools/knowledge_tool.py` | LangChain 知识检索工具和 request-scoped space 强制 | `retrieve_knowledge()`、`enforce_knowledge_space()`、`resolve_knowledge_space_id()` |
| `app/retrieval/pipeline.py` | 可组合检索 pipeline | `RetrievalPipeline`、`LLMQueryRewriter`、`LLMReranker`、`LLMContextCompressor` |
| `app/retrieval/profiles.py` | retrieval profile 注册和 high-recall 分支 | `RetrievalProfileRegistry`、`RequestTransformingRetriever`、`build_retrievers_for_profile()` |
| `app/services/vector_index_service.py` | 文档索引编排、job、catalog、旧 chunk 删除 | `VectorIndexService`、`index_single_file()`、`run_indexing_job()` |
| `app/services/document_splitter_service.py` | Markdown / text 分块 | `DocumentSplitterService`、`split_document()` |
| `app/ingestion/loaders.py` | TXT/Markdown/CSV/HTML/JSON UTF-8 loader | `DocumentLoaderRegistry`、`TextDocumentLoader`、`CSVDocumentLoader`、`HTMLDocumentLoader`、`JSONDocumentLoader` |
| `app/ingestion/metadata.py` | 文档和 chunk 元数据归一化 | `DocumentMetadataNormalizer` |
| `app/ingestion/jobs.py` | 索引任务状态模型 | `IndexingJob`、`IndexingJobStatus` |
| `app/ingestion/queue.py` | memory / SQLite / Postgres 后台索引队列 | `IndexingQueue`、`SQLiteIndexingQueue`、`PostgresIndexingQueue` |
| `app/ingestion/worker.py` | 进程内后台索引 worker | `BackgroundIndexingWorker`、`get_background_indexing_worker()` |
| `app/knowledge/catalog.py` | 知识空间和文档目录 | `KnowledgeSpace`、`DocumentRecord`、`SQLiteKnowledgeCatalog`、`PostgresKnowledgeCatalog` |
| `app/core/milvus_client.py` | Milvus 连接、collection/schema/index 管理 | `MilvusClientManager`、`connect()`、`_create_collection()` |
| `app/providers/milvus.py` | LangChain Milvus vector store provider | `MilvusVectorStoreProvider`、`add_documents()`、`similarity_search()` |
| `app/security/auth.py` | 内部认证、角色权限、知识空间访问控制 | `AuthContext`、`SimpleAuthProvider`、`require_permission()`、`require_space_access()` |
| `app/security/uploads.py` | 上传文件名、扩展名、大小、UTF-8、prompt injection 扫描 | `UploadSecurityPolicy`、`secure_upload_payload()`、`scan_prompt_injection()` |
| `app/security/runtime_controls.py` | 进程内并发和超时控制 | `RuntimeControlSettings`、`install_runtime_controls_middleware()` |
| `app/observability/request_context.py` | trace id、访问日志、HTTP 指标 | `install_request_context_middleware()`、`get_current_trace_id()` |
| `app/observability/metrics.py` | 进程内运行指标和 Prometheus 文本 | `RuntimeMetrics`、`render_prometheus_metrics()` |
| `app/observability/retrieval_audit.py` | 检索审计记录存储 | `RetrievalAuditRecord`、`SQLiteRetrievalAuditStore`、`PostgresRetrievalAuditStore` |
| `app/evaluation/` | golden dataset、评测 runner、metrics、judge、HTTP/service/fake runner | `GoldenExample`、`EvaluationReport`、`run_fake_evaluation()`、`evaluate_results()` |
| `static/` | 浏览器 UI | `static/app.js` 的 `RAGApp` |
| `docs/architecture/` | 架构基线和基础风险登记 | `baseline-audit.md`、`risk-register.md`、`code-quality-audit.md` |
| `docs/evaluation.md` | RAG 评测说明、真实 provider readiness、LangSmith tracing | `docs/evaluation.md` |
| `docs/operations.md` | trace、metrics、runtime controls、health、smoke、security boundary 运维说明 | `docs/operations.md` |
| `docs/deployment.md` | 本地和 staged deployment runbook | `docs/deployment.md` |
| `docs/extension-guide.md` | 第二业务 RAG 扩展指南 | `docs/extension-guide.md` |
| `docs/templates/business-rag-template.md` | 新业务 RAG 配置、工具、prompt、评测模板 | `docs/templates/business-rag-template.md` |
| `docs/business-samples/` | 业务样例语料 | `hr-handbook.md`、`benefits-guide.md`、`expense-policy.md`、`support-sla.md` |
| `tests/` | pytest + Node UI 测试 | `tests/test_rag_state_graph.py`、`tests/test_rag_agent_graph_runtime.py`、`tests/test_vector_index_service_ingestion.py` 等 |
| `scripts/` | 本地验证、评测、smoke、health 脚本 | `validate-baseline.ps1`、`run-evaluation.ps1`、`run-integration-smoke.ps1`、`run-postgres-smoke.ps1` |
| `start.ps1` | Windows 本地启动编排 | `Ensure-PythonDependencies`、`Assert-AppConfiguration`、`Start-MilvusStack`、`Start-FastApiForeground` |
| `vector-database.yml` | Milvus standalone、etcd、MinIO、Attu compose | services `etcd`、`minio`、`standalone`、`attu` |
| `.github/workflows/ci.yml` | Windows CI baseline validation | job `baseline` |

## Goals / Non-Goals

**Goals:**

- 用 OpenSpec change 记录当前项目真实架构，而不是设计新的实现。
- 从实际文件路径说明入口、核心模块、RAG 数据流、Agent 调用链、配置系统、依赖、测试和部署方式。
- 标注每个关键结论对应的文件路径和关键函数/类名。
- 把不确定事项放进 open questions。
- 把风险、测试缺口、安全边界和风险代码集中到 `risk-register.md`。
- 给出新成员或未来的自己可以按步骤执行的学习路线。

**Non-Goals:**

- 不修改业务代码。
- 不修复审计发现的问题。
- 不修改恢复后的 `docs/` 正文、测试、依赖或运行脚本。
- 不声称真实答案质量、真实生产容量、多实例生产数据层已经验证。
- 不替代现有 baseline specs；这里只做学习和审计索引。

## Current Architecture

### 应用入口和生命周期

`app/main.py` 是运行入口。`create_app()` 创建 `FastAPI` 实例，安装 `install_request_context_middleware()` 和 `install_runtime_controls_middleware()`，再挂载 `health`、`chat`、`file`、`metrics` 路由。`create_lifespan()` 在启动时根据 `VECTOR_STORE_PROVIDER` 判断是否连接 Milvus，并在 `INDEXING_EXECUTION_MODE=background` 时启动 `BackgroundIndexingWorker`。`build_uvicorn_options()` 从 `Settings.app` 或 flat settings 读取 host、port、debug/reload。

静态 UI 由 `app/main.py` 挂载 `static/` 并在 `/` 返回 `static/index.html`。前端主要逻辑在 `static/app.js` 的 `RAGApp`，它调用 `/api/chat_stream`、`/api/upload`、`/api/knowledge-spaces`、`/api/index-jobs` 和 `/api/chat/audits`。

### 配置系统

`app/config.py` 的 `Settings` 从 `.env` 读取 flat 环境变量，并通过只读 grouped properties 暴露 `app`、`cors`、`upload`、`deployment`、`auth`、`runtime`、`providers`、`storage`、`agent`、`openai_compatible`、`dashscope`、`milvus`、`rag`、`chunking`。`app/operations/config_validation.py` 的 `validate_settings()` 对 provider id、生产环境安全值、CORS、上传限制、chunking、retrieval profile、Agent runtime、Postgres/SQLite DSN/path 等做启动前校验。

关键配置默认值来自 `app/config.py` 和 `.env.example`：默认 `CHAT_PROVIDER` 留空（按有效 Key 自动选择，双 Key 时 DeepSeek-first）、`CHAT_MODEL=deepseek-v4-pro`、`EMBEDDING_PROVIDER=dashscope`、`DASHSCOPE_EMBEDDING_MODEL=text-embedding-v4`、`VECTOR_STORE_PROVIDER=milvus`、`SESSION_STORE_PROVIDER=sqlite`、`CHECKPOINT_PROVIDER=sqlite`、`AGENT_RUNTIME=explicit_graph`、`ENABLED_TOOLS=retrieve_knowledge,get_current_time`、`RETRIEVAL_PROFILE=default`。RAG 没有专用模型变量；所有 chat provider 共用 `CHAT_MODEL`。

### Provider 组合

`app/providers/factory.py` 的 `create_default_provider_container()` 是运行时依赖装配中心。它根据 `ProviderSelection.from_settings()` 和 `validate_provider_selection()` 选择：

- chat provider：`DeepSeekChatModelProvider`（默认候选）、`DashScopeChatModelProvider`、`OpenAICompatibleChatModelProvider` 或 `FakeChatModelProvider`；每个 chat 分支只接收共享 `CHAT_MODEL`。
- embedding provider：`DashScopeEmbeddingProvider`（默认）、`OpenAICompatibleEmbeddingProvider` 或 `FakeEmbeddingProvider`；DashScope embedding 使用独立的 `DASHSCOPE_EMBEDDING_MODEL`。
- vector store provider：`MilvusVectorStoreProvider` 或 `FakeVectorStoreProvider`。
- retriever provider：`RetrievalPipeline`，内部包裹 `VectorStoreRetrieverProvider` 和可选 LLM rewrite/rerank/compress（LLM 增强器复用 `ProviderContainer.chat_model_provider`）。
- session store：`SQLiteSessionStoreProvider`、`PostgresSessionStoreProvider` 或 `InMemorySessionStoreProvider`。
- retrieval audit store：`SQLiteRetrievalAuditStore`、`PostgresRetrievalAuditStore` 或 `InMemoryRetrievalAuditStore`。
- ingestion provider：`VectorIndexIngestionProvider` 或 `FakeIngestionProvider`。
- checkpoint provider：`SQLiteCheckpointProvider`、`PostgresCheckpointProvider` 或 `InMemoryCheckpointProvider`。

`get_default_provider_container()` 是进程级 singleton。测试通过 `reset_default_provider_container()` 重置。

## RAG 数据流

### 文档上传和索引流

1. HTTP 请求进入 `app/api/file.py` 的 `upload_file()`，先由 `require_permission("document:upload")` 和 `require_space_access()` 做权限与知识空间检查。
2. 上传内容交给 `app/security/uploads.py` 的 `secure_upload_payload()`，检查文件名、扩展名、大小、UTF-8 和 `scan_prompt_injection()`。
3. `upload_file()` 把内容写到 `uploads/<safe_filename>`，如果同名文件已存在会先 `unlink()`。
4. `_call_index_single_file()` 通过 `get_default_provider_container().ingestion_provider.index_file()` 进入 ingestion provider。
5. `app/providers/ingestion.py` 的 `VectorIndexIngestionProvider.index_file()` 根据 `INDEXING_EXECUTION_MODE` 选择：
   - `sync`：调用 `VectorIndexService.index_single_file()`。
   - `background`：调用 `VectorIndexService.create_pending_indexing_job()`，然后 `BackgroundIndexingWorker.enqueue()`。
6. `app/services/vector_index_service.py` 的 `VectorIndexService.index_single_file()` 创建 pending job 后调用 `run_indexing_job()`。
7. `run_indexing_job()` 调用 `_load_document_metadata()`，由 `DocumentLoaderRegistry.default()` 选择 `TextDocumentLoader`、`MarkdownDocumentLoader`、`CSVDocumentLoader`、`HTMLDocumentLoader` 或 `JSONDocumentLoader`。
8. `DocumentMetadataNormalizer.document_metadata()` 生成稳定 `document_id`、`space_id`、`source_path`、`content_hash`。
9. `VectorIndexService._delete_existing_document_chunks()` 先按 `document_id` 和 legacy source metadata 删除旧 chunk。
10. `DocumentSplitterService.split_document()` 负责 Markdown/text 分块；Markdown 使用 `MarkdownHeaderTextSplitter`，普通文本使用 `RecursiveCharacterTextSplitter`。
11. 每个 chunk 通过 `DocumentMetadataNormalizer.chunk_metadata()` 写入 `chunk_id`、`chunk_index`、heading path 等 metadata。
12. `VectorStoreManager.add_documents()` 转到 `MilvusVectorStoreProvider.add_documents()`，通过 LangChain Milvus 写入 collection `biz`。
13. 索引结果保存到 `IndexingJobStore`，并通过 `KnowledgeCatalog.upsert_from_job()` 写入文档目录。

### 非流式问答流

1. `POST /api/chat` 进入 `app/api/chat.py` 的 `chat()`，使用 `ChatRequest` 解析 `Id`、`Question`、`spaceId`，并检查 `chat:write` 与知识空间权限。
2. `chat()` 调用 `_call_query_with_trace()`，实际进入 `app/services/rag_agent_service.py` 的 `RagAgentService.query_with_trace()`。
3. 默认 `AGENT_RUNTIME=explicit_graph`，`query_with_trace()` 调用 `_invoke_explicit_graph()`。
4. `_build_default_explicit_graph()` 调用 `build_rag_state_graph()`，注入 `RetrieverProvider`、`ChatModelAnswerGenerator`、`ToolExecutor`、可选 `ToolPlanner`、checkpointer 和默认 top_k。
5. `app/agents/rag_graph.py` 的图节点顺序是 `normalize_input` -> `decide_retrieval` -> `retrieve` 或 `tool` 或 `handoff` -> `answer` 或 `error_policy` -> `final_response`。
6. `RagGraphNodes.retrieve()` 用 `RetrievalRequest(query, top_k, filters)` 调用 `RetrievalPipeline.retrieve()`。当 `space_id != default` 时，`_filters_from_space_id()` 加入 `{"space_id": space_id}`。
7. `RetrievalPipeline.retrieve()` 可选 rewrite，然后调用 primary/additional retriever，去重，选用 reranker/compressor，再生成 `RetrievalSource`。
8. `VectorStoreRetrieverProvider.retrieve()` 调用 `MilvusVectorStoreProvider.similarity_search()`。
9. `RagGraphNodes.answer()` 调用 `ChatModelAnswerGenerator.generate()`，`_build_answer_prompt()` 把检索文档拼进 prompt，并要求只使用 retrieved context。
10. `RagAgentService.query_with_trace()` 把 graph state 序列化为 answer、sources、retrieval debug、token usage，并写入 session store、retrieval audit store 和 runtime metrics。
11. API 返回 `success_envelope()`，data 中包含 `success`、`answer`、`sources`、`retrievalDebug`、`retrieval`、`errorMessage`。

### 流式问答流

`POST /api/chat_stream` 进入 `chat_stream()`，调用 `RagAgentService.query_stream_with_trace()`。显式图使用 `explicit_graph.stream(..., stream_mode=["custom", "updates"])`，`app/services/rag_agent_service.py` 的 `_stream_chunk_from_custom_payload()`、`_stream_chunks_from_graph_update()` 和 `app/api/chat.py` 的 `format_stream_chunk()` 把 graph event 映射为 SSE message data。关键 chunk type 包括 `retrieval_decision`、`retrieval`、`handoff`、`error_policy`、`tool_call`、`tool_result`、`content`、`done`、`error`。

### Legacy Agent 和工具流

当 `AGENT_RUNTIME=legacy` 时，`RagAgentService._run_legacy_query()` 使用 LangChain `create_agent()`，工具来自 `app/extensions/tools.py` 的 `build_enabled_tools()`。默认工具是 `app/tools/knowledge_tool.py` 的 `retrieve_knowledge()` 和 `app/tools/time_tool.py` 的 `get_current_time()`。legacy 调用会包裹 `enforce_knowledge_space(space_id)`，强制 `retrieve_knowledge()` 使用请求选定 space。

显式图支持工具分支，但没有 pre-retrieval planner：`RagGraphNodes.decide_retrieval()` 仅在 state 已有显式 `tool_request.name` 时进入 `tool`；否则走 retrieve / handoff。答案阶段 `RagGraphNodes.answer()` 可通过模型 `tool_calls` 进入 answer↔tool 续轮（`route_after_answer` → `tool` → `route_after_tool` → `answer`）。原生 RAG 检索始终走 graph retrieve 节点。

## Testing And Deployment

### 测试

`pyproject.toml` 配置 pytest testpath 为 `tests`，并默认加 `--cov=app`。测试覆盖面较广：

- Agent graph：`tests/test_rag_state_graph.py`、`tests/test_rag_agent_graph_runtime.py`。
- RAG trace/session/audit：`tests/test_chat_retrieval_trace.py`、`tests/test_session_store_service.py`、`tests/test_retrieval_audit.py`。
- ingestion/indexing/knowledge spaces：`tests/test_vector_index_service_ingestion.py`、`tests/test_file_upload_ingestion.py`、`tests/test_ingestion_foundation.py`、`tests/test_knowledge_spaces_lifecycle.py`。
- provider/config/health/security：`tests/test_provider_factory.py`、`tests/test_provider_contracts.py`、`tests/test_config_groups.py`、`tests/test_phase7_operations.py`、`tests/test_authz_foundation.py`、`tests/test_upload_security.py`。
- evaluation/smoke：`tests/test_evaluation_foundation.py`、`tests/test_postgres_smoke.py`。
- frontend history：`tests/chat-history.test.js`。

`tests/test_phase0_baseline.py` 要求多个 `docs/...` 文件存在；当前文件树已恢复这些文档。后续仍需要通过 baseline 测试确认恢复内容与测试断言同步。

### 部署和本地运行

`README.md` 和 `start.ps1` 描述 Windows 本地启动。`start.ps1` 会：

- 检查或安装 Python 依赖：`Ensure-PythonDependencies`。
- 调用 `app.operations.config_validation`：`Assert-AppConfiguration`。
- 启动或复用 Milvus Docker stack：`Start-MilvusStack`。
- 可选只做 `-PreflightOnly`。
- 检查 API 端口并启动 Uvicorn：`Start-FastApiForeground`。

`vector-database.yml` 定义 Milvus 所需 `etcd`、`minio`、`standalone`，并用 profile `ui` 可选启动 Attu。`.github/workflows/ci.yml` 在 Windows 上安装 Python/Node，运行 `scripts/validate-baseline.ps1 -SkipPreflight` 和 `scripts/run-evaluation.ps1`，并上传 evaluation report artifact。

## Decisions

1. 本 change 只写 OpenSpec 文档，不写业务代码。
   - 理由：用户明确要求第一阶段只能阅读代码，且目标是重新掌控项目。
   - 替代方案：直接修改 `docs/` 正文或修复风险。该方案会越过本阶段边界。

2. 以 `app/main.py`、`app/services/rag_agent_service.py`、`app/agents/rag_graph.py`、`app/providers/factory.py`、`app/services/vector_index_service.py` 为主线组织架构说明。
   - 理由：这些文件是 HTTP 入口、Agent 编排、provider 装配、RAG 查询和索引流的汇合点。
   - 替代方案：按目录逐个解释。该方式容易丢失跨模块数据流。

3. 风险登记以“证据路径 + 影响 + 后续建议”呈现。
   - 理由：后续要开独立 OpenSpec change 时可以直接引用风险项。
   - 替代方案：只写抽象风险分类。该方式不符合“基于实际文件路径”的约束。

4. 对不确定项只写 open questions。
   - 理由：当前审计没有运行真实 Milvus/DashScope/Postgres；`docs/` 已通过文件清单确认恢复，但内容同步仍应由 baseline 测试和后续审阅验证。
   - 替代方案：根据经验判断生产行为。该方式会降低文档可信度。

## Risks / Trade-offs

- [Risk] 本 change 会暴露多个未修复问题，但不修复它们 -> Mitigation：全部写入 `risk-register.md`，后续逐项开 change。
- [Risk] 文档可能随着代码变更失效 -> Mitigation：把文件路径和关键函数/类作为索引，后续修改对应模块时同步更新学习文档。
- [Risk] OpenSpec change 额外创建 `specs/codebase-learning-audit/spec.md`，虽然用户未单独点名 -> Mitigation：该 spec 仅用于约束学习/审计文档质量，不改变业务 baseline。
- [Risk] 未运行真实外部依赖验证 -> Mitigation：`risk-register.md` 和 open questions 明确区分“代码静态确认”和“需要运行验证”。

## Migration Plan

无需运行时迁移。本 change 的落地方式是写入 `openspec/changes/learn-rag-agent-codebase/` 下的 Markdown 文档。

回滚方式：删除 `openspec/changes/learn-rag-agent-codebase/`。不会影响业务代码、数据库、Milvus collection、上传文件、SQLite/Postgres 状态或部署脚本。

## Open Questions

### 需要运行验证

- `vector-database.yml` 使用 external network `milvus`，`start.ps1` 是否保证首次运行前该网络存在？当前审计未看到显式创建网络逻辑。
- LangChain Milvus 当前版本对 `similarity_search(..., filter=dict)` 的真实行为是否与预期一致？证据：`app/providers/milvus.py` 的 `similarity_search()`；需要真实 Milvus 或库文档/集成测试确认。

### 需要产品决策

- `space_id="default"` 在检索时当前不会加 filter；这是设计为“默认空间”还是“全局不隔离检索”？证据：`app/services/rag_agent_service.py` 的 `retrieve_context()`、`app/agents/rag_graph.py` 的 `_filters_from_space_id()`。
- 恢复后的 `docs/` 与 `openspec/specs/` 是否需要建立固定同步规则，避免 baseline docs 和 OpenSpec baseline specs 继续双轨漂移？证据：`docs/architecture/baseline-audit.md` 与 `openspec/specs/*/spec.md` 都记录架构/能力边界。

### 已澄清的模型配置真相

- 所有 chat provider 共用唯一模型来源 `CHAT_MODEL`（默认 `deepseek-v4-pro`）。`RAG_MODEL`、`DASHSCOPE_MODEL`、`OPENAI_COMPATIBLE_MODEL` 及其 Python 字段已删除，不再作为配置或回退路径。
- RAG 没有专用模型变量；答案生成（含 answer↔tool 续轮）、evaluation judge 与检索增强器（rewrite / rerank / compress）在需要 LLM 时都复用 `ProviderContainer.chat_model_provider`。
- DashScope embedding 继续使用独立的 `DASHSCOPE_EMBEDDING_MODEL`，与 `CHAT_MODEL` 解耦。证据：`app/config.py` 的 `Settings.chat_model`、`app/providers/factory.py` 的 `create_default_provider_container()`、`app/providers/selection.py`。

### 需要代码修复

- 生产或内部试运行中的反向代理是否会可信地注入并清洗 `X-RAG-User`、`X-RAG-Roles`、`X-RAG-Spaces`？证据：`app/security/auth.py` 的 `SimpleAuthProvider.authenticate()` 信任 configured headers。
