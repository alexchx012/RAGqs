# 代码库地图

## 1. 项目目录树摘要

### 一级目录职责

| 路径 | 职责 | 关键入口/对象 | 风险或备注 |
| --- | --- | --- | --- |
| `.codex/` | Codex/OpenSpec 本地技能和项目规则目录。 | `.codex/skills/openspec-propose/SKILL.md` | 非业务代码；影响 AI 工作流，不影响运行时。 |
| `.github/` | GitHub Actions CI 配置。 | `.github/workflows/ci.yml` | CI 在 Windows 上安装依赖、跑基线校验和评估报告。 |
| `app/` | FastAPI 后端、RAG/Agent、索引、Provider、配置、安全、观测和评估核心代码。 | `app/main.py::create_app`, `app/services/rag_agent_service.py::RagAgentService` | 最高优先级学习目录。 |
| `artifacts/` | 本地评估输出产物。 | `artifacts/evaluation-report.json` | 由脚本生成；`.gitignore` 忽略。 |
| `data/` | 本地运行状态 SQLite 和评估数据。 | `data/evaluation/golden.jsonl`, `data/*.sqlite3` | SQLite 文件保存会话、审计、索引任务、文档目录和 checkpoint；`.gitignore` 忽略数据库文件。 |
| `docs/` | 项目说明、部署、运维、评估、扩展和业务样例文档。 | `docs/deployment.md`, `docs/operations.md`, `docs/evaluation.md`, `docs/extension-guide.md` | 当前已恢复，但 `.gitignore` 仍包含 `docs/`，是否纳入版本管理需确认。 |
| `logs/` | 本地日志目录。 | `app/utils/logger.py::logger` 相关配置使用日志目录。 | `.gitignore` 忽略。 |
| `openspec/` | OpenSpec 基线规格和当前 change 文档。 | `openspec/config.yaml`, `openspec/specs/*/spec.md`, `openspec/changes/learn-rag-agent-codebase/` | 本次只编辑此 change 下的文档。 |
| `rag_knowledge_agent.egg-info/` | Python editable install 生成的包元数据。 | `pyproject.toml` 触发生成 | 生成物；不应作为业务学习重点。 |
| `scripts/` | 校验、评估、烟测和健康检查脚本。 | `scripts/validate-baseline.ps1`, `scripts/run-evaluation.ps1`, `scripts/run-integration-smoke.ps1` | 是测试/部署链路的重要入口。 |
| `static/` | 浏览器前端 UI。 | `static/index.html`, `static/app.js::RAGApp` | 用户问题和文件上传的前端入口。 |
| `tests/` | Python/Node/PowerShell 测试。 | `tests/test_rag_state_graph.py`, `tests/test_file_upload_ingestion.py`, `tests/chat-history.test.js` | 覆盖面较广，但真实模型质量、真实并发、生产多实例仍需外部验证。 |
| `volumes/` | Docker/Milvus/MinIO/etcd 本地持久化数据。 | `vector-database.yml` volume 映射 | `.gitignore` 忽略；不要手工修改业务数据。 |

### 核心文件职责

| 文件路径 | 职责 | 关键函数/类 |
| --- | --- | --- |
| `app/main.py` | FastAPI 应用入口、生命周期、路由挂载、静态文件挂载、Uvicorn 参数。 | `create_lifespan`, `create_app`, `build_uvicorn_options`, `app` |
| `app/config.py` | `.env` 配置读取和分组配置视图。 | `Settings`, `AppConfig`, `ProviderConfig`, `AgentConfig`, `RagConfig`, `ChunkingConfig` |
| `app/api/chat.py` | 问答、流式问答、会话、检索审计 API。 | `chat`, `chat_stream`, `_call_query_with_trace`, `format_stream_chunk`, `list_retrieval_audits` |
| `app/api/file.py` | 文件上传、知识空间、文档生命周期、索引任务 API。 | `upload_file`, `_call_index_single_file`, `build_upload_security_policy`, `delete_document`, `rebuild_document` |
| `app/services/rag_agent_service.py` | RAG/Agent 运行时编排，连接 API、图、Provider、会话、审计、指标。 | `RagAgentService`, `query_with_trace`, `query_stream_with_trace`, `_invoke_explicit_graph`, `_build_default_explicit_graph`, `retrieve_context` |
| `app/agents/rag_graph.py` | 显式 LangGraph RAG 状态图。 | `RagGraphState`, `RagGraphNodes`, `build_rag_state_graph`, `ChatModelAnswerGenerator`, `LangChainToolExecutor`, `_build_answer_prompt` |
| `app/providers/factory.py` | Provider 组合根，按配置创建 chat/embedding/vector/retriever/session/audit/ingestion/checkpoint provider。 | `ProviderContainer`, `create_default_provider_container`, `get_default_provider_container` |
| `app/providers/contracts.py` | Provider 边界协议和核心数据结构。 | `RetrievalRequest`, `RetrievalResult`, `RetrievalSource`, `EmbeddingProvider`, `ChatModelProvider`, `VectorStoreProvider`, `RetrieverProvider`, `IngestionProvider` |
| `app/retrieval/pipeline.py` | 检索流水线：改写、检索、去重、重排、压缩、source 序列化。 | `RetrievalPipeline.retrieve`, `LLMQueryRewriter`, `LLMReranker`, `LLMContextCompressor`, `_deduplicate_documents`, `_source_from_document` |
| `app/retrieval/profiles.py` | 检索 profile：默认严格检索和 high_recall 分支。 | `RetrievalProfile`, `RetrievalProfileRegistry`, `RequestTransformingRetriever`, `build_default_retrieval_profile_registry` |
| `app/services/vector_index_service.py` | 文档索引服务，驱动加载、切分、元数据、向量写入、索引任务和文档目录。 | `VectorIndexService`, `index_single_file`, `run_indexing_job`, `_load_document_metadata`, `_delete_existing_document_chunks` |
| `app/services/document_splitter_service.py` | 文档 chunk 切分。 | `DocumentSplitterService`, `split_document`, `split_markdown`, `split_text`, `_merge_small_chunks` |
| `app/ingestion/loaders.py` | 本地文档加载器。 | `DocumentLoaderRegistry`, `TextDocumentLoader`, `MarkdownDocumentLoader`, `CSVDocumentLoader`, `HTMLDocumentLoader`, `JSONDocumentLoader` |
| `app/ingestion/metadata.py` | document/chunk 元数据和稳定 ID 生成。 | `DocumentMetadataNormalizer.document_metadata`, `DocumentMetadataNormalizer.chunk_metadata` |
| `app/providers/milvus.py` | Milvus 向量库 Provider。 | `MilvusVectorStoreProvider`, `get_vector_store`, `add_documents`, `delete_by_document_id`, `similarity_search` |
| `app/core/milvus_client.py` | Milvus collection 连接、创建、索引和健康检查。 | `MilvusClientManager`, `connect`, `_create_collection`, `_create_index`, `health_check` |
| `app/services/vector_embedding_service.py` | 默认 DashScope embedding 的 lazy provider 包装（与 chat provider 独立）。 | `build_vector_embedding_service`, `LazyEmbeddingProvider`, `vector_embedding_service` |
| `app/extensions/tools.py` | 工具注册和启用工具构建。 | `ToolRegistry`, `build_default_tool_registry`, `build_enabled_tools`, `parse_enabled_tool_names` |
| `app/tools/knowledge_tool.py` | legacy/tool 路径下的知识库检索工具。 | `retrieve_knowledge`, `retrieve_knowledge_with_provider`, `enforce_knowledge_space` |
| `app/security/auth.py` | 内部认证授权和知识空间访问控制。 | `AuthContext`, `SimpleAuthProvider.authenticate`, `require_permission`, `require_space_access` |
| `app/security/uploads.py` | 上传文件名、扩展名、大小、UTF-8、prompt injection 扫描。 | `secure_upload_payload`, `sanitize_upload_filename`, `resolve_upload_path`, `scan_prompt_injection` |
| `app/observability/metrics.py` | 运行时 HTTP/RAG 指标和 Prometheus 文本输出。 | `RuntimeMetrics`, `record_http_request`, `record_rag_query`, `render_prometheus_metrics` |
| `static/app.js` | 前端单页应用逻辑。 | `RAGApp`, `sendMessage`, `sendQuick`, `sendStream`, `handleFileUpload`, `refreshKnowledgeSpaces` |

### 入口文件

| 入口 | 进入方式 | 关键函数/类 | 后续调用 |
| --- | --- | --- | --- |
| `app/main.py` | `python -m uvicorn app.main:app` 或直接执行 `python app/main.py` | `app = create_app()`, `if __name__ == "__main__"` | 挂载 `app.api.chat.router`, `app.api.file.router`, `app.api.health.create_health_router`, `app.api.metrics.router` |
| `start.ps1` | Windows 本地启动/预检脚本 | `Ensure-PythonDependencies`, `Assert-AppConfiguration`, `Start-MilvusStack`, `Start-FastApiForeground` | 调用 `app.operations.config_validation`, `app.operations.health_preflight`, Docker Compose, Uvicorn |
| `static/index.html` | 浏览器访问 `/` 后由 `app/main.py::root` 返回 | 加载 `static/app.js` | 创建 `RAGApp`，调用 `/api/chat`, `/api/chat_stream`, `/api/upload` |
| `app/evaluation/runner.py` | `scripts/run-evaluation.ps1` 间接调用 | `main`, `build_arg_parser` | 调用 fake/service/http/real preflight 评估路径 |
| `app/operations/config_validation.py` | `start.ps1` 和测试/运维脚本调用 | `validate_settings`, `main` | 校验 `.env`、Provider、生产安全配置 |

### 配置文件

| 文件路径 | 配置范围 | 关键内容 |
| --- | --- | --- |
| `app/config.py` | 运行时配置源代码默认值。 | `Settings` 从 `.env` 读取，提供 `settings.app/providers/storage/agent/rag/chunking` 等分组属性。 |
| `.env.example` | 本地环境变量模板。 | Provider、auth、runtime controls、storage、Milvus、RAG、chunking、LangSmith。 |
| `.env` | 本地实际环境变量。 | 未纳入版本控制；包含真实运行配置。 |
| `pyproject.toml` | Python 包、依赖、ruff/black/pytest 配置。 | 依赖 FastAPI、LangChain、LangGraph、Milvus、DashScope/OpenAI-compatible。 |
| `vector-database.yml` | Milvus 本地依赖栈。 | `etcd`, `minio`, `standalone`, 可选 `attu` profile。 |
| `.github/workflows/ci.yml` | Hosted CI。 | Windows + Python 3.12 + Node 22；运行 `scripts/validate-baseline.ps1 -SkipPreflight` 和评估脚本。 |
| `openspec/config.yaml` | OpenSpec schema 配置。 | `schema: spec-driven`。 |
| `.gitignore` | 本地生成物忽略规则。 | 忽略 `.env`, `uploads/`, `logs/`, `volumes/`, `artifacts/`, `data/*.sqlite3`, `docs/` 等。 |

### 测试文件

| 测试路径 | 覆盖对象 | 代表性测试 |
| --- | --- | --- |
| `tests/test_rag_state_graph.py` | 显式 LangGraph RAG 节点、路由、工具、错误、stream、checkpoint。 | `test_explicit_rag_state_graph_retrieves_answers_and_records_events`, `test_explicit_rag_state_graph_passes_knowledge_space_filter_to_retriever` |
| `tests/test_rag_agent_graph_runtime.py` | `RagAgentService` explicit_graph runtime。 | `test_rag_agent_service_query_with_trace_can_use_explicit_graph_runtime`, `test_rag_agent_service_refuses_without_calling_model_when_graph_retrieval_is_empty` |
| `tests/test_chat_retrieval_trace.py` | Chat API、trace、stream chunk 格式。 | `test_chat_api_returns_sources_and_retrieval_debug`, `test_chat_stream_chunk_formatter_maps_graph_routing_events` |
| `tests/test_file_upload_ingestion.py` | 上传 API 到索引 provider。 | `test_upload_file_returns_indexing_status_on_success`, `test_upload_file_applies_upload_security_before_indexing` |
| `tests/test_vector_index_service_ingestion.py` | 文档索引服务。 | `test_vector_index_service_uses_loader_and_normalized_chunk_metadata`, `test_vector_index_service_runs_existing_pending_job` |
| `tests/test_ingestion_foundation.py` | loader、splitter、metadata、job store。 | `test_loader_registry_reads_csv_html_and_json_files`, `test_metadata_normalizer_assigns_stable_document_ids_and_chunk_metadata` |
| `tests/test_retrieval_pipeline.py` | 检索流水线。 | `test_retrieval_pipeline_rewrites_queries_combines_and_deduplicates_sources` |
| `tests/test_retrieval_profiles.py` | 检索 profile。 | `test_high_recall_profile_adds_widened_and_relaxed_retrievers` |
| `tests/test_provider_factory.py` | Provider 组合。 | `test_default_provider_container_wires_all_boundaries_without_connecting` |
| `tests/test_provider_contracts.py` | Provider 协议和 fake/Milvus/DashScope 基础行为。 | `test_milvus_vector_store_provider_is_lazy_and_factory_backed` |
| `tests/test_authz_foundation.py` | 权限和知识空间授权。 | `test_chat_api_denies_space_from_client_when_user_lacks_access` |
| `tests/test_upload_security.py` | 上传安全策略。 | `test_secure_upload_payload_rejects_prompt_injection_when_enabled` |
| `tests/test_phase7_operations.py` | 运维、健康、指标、配置验证、CI 文档。 | `test_config_validation_rejects_unsafe_production_defaults` |
| `tests/test_evaluation_foundation.py` | 评估数据、指标、runner、readiness。 | `test_real_evaluation_readiness_rejects_fake_mode` |
| `tests/chat-history.test.js` | 前端会话、知识空间、上传管理逻辑。 | `testChatAndUploadSendSelectedKnowledgeSpace`, `testManagementFlowsUseBackendSpaceAndLifecycleApis` |
| `tests/start-script.validation.ps1` | 启动脚本文本/行为验证。 | PowerShell 断言脚本内容。 |

## 2. 运行链路

### 非流式问答链路

1. 用户在前端输入问题：`static/app.js::RAGApp.sendMessage` 读取输入框，按 `currentMode` 调用 `sendQuick` 或 `sendStream`。
2. 非流式请求由 `static/app.js::RAGApp.sendQuick` 发送到 `POST /api/chat`，请求体字段为 `Id`, `Question`, `spaceId`。
3. 后端请求模型是 `app/models/request.py::ChatRequest`，通过 alias 把 `Id` 映射到 `id`，`Question` 映射到 `question`，`spaceId` 映射到 `space_id`。
4. `app/api/chat.py::chat` 进入后先执行 `require_permission("chat:write")` 和 `require_space_access`，权限逻辑来自 `app/security/auth.py::SimpleAuthProvider.authenticate`、`AuthContext.has_permission`、`AuthContext.can_access_space`。
5. `app/api/chat.py::_call_query_with_trace` 调用全局 `app/services/rag_agent_service.py::rag_agent_service.query_with_trace`。
6. 默认 `agent_runtime=explicit_graph` 时，`RagAgentService.query_with_trace` 调用 `_invoke_explicit_graph`，图由 `_build_default_explicit_graph` 使用 `app/agents/rag_graph.py::build_rag_state_graph` 构造。
7. 图执行顺序在 `app/agents/rag_graph.py::build_rag_state_graph` 中定义：`START -> normalize_input -> decide_retrieval -> retrieve/tool/handoff -> answer/error_policy -> final_response -> END`。
8. RAG 检索发生在 `app/agents/rag_graph.py::RagGraphNodes.retrieve`，它构造 `RetrievalRequest(query=normalized_question, top_k=default_top_k, filters=_filters_from_space_id(space_id))` 并调用 `RetrieverProvider.retrieve`。
9. Prompt 构造发生在 `app/agents/rag_graph.py::_build_answer_prompt`，把 `retrieval_result.documents` 拼成 `[n] source=...` context block，并要求模型只使用检索上下文回答。
10. System prompt 来自 `app/services/rag_agent_service.py::RagAgentService._build_system_prompt`，再进入 `app/prompts/profiles.py::build_system_prompt`。
11. 模型调用发生在 `app/agents/rag_graph.py::ChatModelAnswerGenerator.generate`，它调用 `chat_model_provider.create_chat_model(streaming=False)` 得到模型，再执行 `model.invoke([SystemMessage, HumanMessage])`。
12. 返回值由 `app/services/rag_agent_service.py::_serialize_graph_state` 转成 `{answer, sources, retrieval}`。
13. `RagAgentService.query_with_trace` 同时调用 `_record_session_exchange` 写会话、`_record_retrieval_audit` 写检索审计、`_record_rag_query_metric` 写运行指标。
14. `app/api/chat.py::chat` 使用 `app/models/response.py::success_envelope` 返回 `answer`, `sources`, `retrievalDebug`, `retrieval`；异常时使用 `error_envelope`。

### 流式问答链路

1. 前端 `static/app.js::RAGApp.sendStream` 请求 `POST /api/chat_stream`。
2. `app/api/chat.py::chat_stream` 创建 `event_generator`，调用 `RagAgentService.query_stream_with_trace`。
3. explicit graph 路径调用 `RagAgentService._stream_explicit_graph`，如果图支持 `stream`，以 `stream_mode=["custom", "updates"]` 拉取事件。
4. `app/agents/rag_graph.py::RagGraphNodes.answer` 在可用 stream writer 时调用 `_stream_answer_tokens`，逐 token 发送 `{"type": "token", "node": "answer"}`。
5. `app/services/rag_agent_service.py::_stream_chunk_from_custom_payload` 和 `_stream_chunks_from_graph_update` 把图事件转换成 chunk。
6. `app/api/chat.py::format_stream_chunk` 把 `token` 映射为前端的 `content`，把 `retrieval/tool_call/tool_result/done/error` 等映射为 SSE JSON。
7. 前端 `static/app.js::RAGApp.sendStream` 读取 `ReadableStream`，只把 `type === "content"` 追加到当前 assistant 消息。

### Agent 工具调用位置

| 路径 | 函数/类 | 工具调用行为 |
| --- | --- | --- |
| `app/agents/rag_graph.py` | `RagGraphNodes.decide_retrieval` | 仅当 state 中已有显式 `tool_request.name` 时路由到 `tool`；否则空问题 handoff，其余走 retrieve。无 pre-retrieval planner。 |
| `app/agents/rag_graph.py` | `RagGraphNodes.answer` / `route_after_answer` | answer 阶段模型可产出 `tool_calls`；有 tool_calls 则 `answer → tool`，工具结果后 `tool → answer` 续轮。 |
| `app/agents/rag_graph.py` | `RagGraphNodes.tool` | 优先执行模型 AIMessage.tool_calls；否则执行显式 `tool_request`。 |
| `app/agents/rag_graph.py` | `LangChainToolExecutor.execute` | 按名称找到 LangChain tool，优先调用 `tool.invoke(args)`。 |
| `app/extensions/tools.py` | `build_default_tool_registry`, `build_enabled_tools` | 默认注册 `retrieve_knowledge` 和 `get_current_time`。 |
| `app/tools/knowledge_tool.py` | `retrieve_knowledge`, `retrieve_knowledge_with_provider`, `enforce_knowledge_space` | legacy/tool 路径下调用 retriever provider；`enforce_knowledge_space` 保证工具内 space_id 不越权到请求外空间。 |
| `app/services/rag_agent_service.py` | `_run_legacy_query`, `query_stream` legacy 分支 | `agent_runtime=legacy` 时使用 `langchain.agents.create_agent` 和工具执行。 |

### 异常处理位置

| 位置 | 处理方式 | 风险备注 |
| --- | --- | --- |
| `app/api/chat.py::chat` | 捕获非 `HTTPException`，记录日志并返回 `error_envelope`。 | HTTP 状态仍可能是 200 业务 envelope；调用方需看 `code/data.success`。 |
| `app/api/chat.py::chat_stream.event_generator` | 捕获异常后输出 SSE `{"type":"error"}`。 | 流式客户端必须处理 error event。 |
| `app/services/rag_agent_service.py::query/query_stream` | 记录日志后重新抛出或 yield error。 | 非流式由 API envelope 包装；流式可能已输出部分 token。 |
| `app/agents/rag_graph.py::RagGraphNodes.retrieve/answer/tool` | 调用 `_error_update` 写入 `errors` 和 `error` event，然后路由到 `error_policy/final_response`。 | graph 错误被结构化，但业务是否应该重试未实现。 |
| `app/providers/milvus.py::MilvusVectorStoreProvider.similarity_search` | 捕获异常并返回空列表。 | 高风险：真实检索故障会表现为“无上下文”，可能掩盖服务故障。 |
| `app/services/vector_index_service.py::run_indexing_job` | 失败时 `job.complete(..., errors=[str(e)])` 并保存 job/catalog，然后重新抛出。 | 上传 API 会转成 500；失败任务会进入目录。 |
| `app/providers/ingestion.py::VectorIndexIngestionProvider.index_file` | 捕获异常并返回 `IngestionResult(success=False)`。 | 上层 `_indexing_job_from_ingestion_result` 再转 RuntimeError。 |

## 3. 数据链路

### 文档导入到向量库

1. 用户通过前端 `static/app.js::RAGApp.handleFileUpload` 选择文件，发送 `POST /api/upload?space_id=...`。
2. `app/api/file.py::upload_file` 读取 `UploadFile` 内容，执行权限检查 `require_permission("document:upload")` 和 `require_space_access`。
3. 上传安全由 `app/security/uploads.py::secure_upload_payload` 完成：`sanitize_upload_filename` 清理文件名，`parse_allowed_extensions` 校验扩展名，`resolve_upload_path` 防目录逃逸，`scan_prompt_injection` 做简单 prompt injection 字符串扫描。
4. 文件写入 `app/api/file.py::UPLOAD_DIR = Path("./uploads")` 指向的 `uploads/`。
5. `app/api/file.py::_call_index_single_file` 取 `app/providers/factory.py::get_default_provider_container().ingestion_provider`，调用 `IngestionProvider.index_file`。
6. 默认 provider 是 `app/providers/ingestion.py::VectorIndexIngestionProvider.index_file`；`INDEXING_EXECUTION_MODE=sync` 时直接调用 `VectorIndexService.index_single_file`，`background` 时调用 `create_pending_indexing_job` 并通过 `BackgroundIndexingWorker.enqueue` 入队。
7. `app/services/vector_index_service.py::VectorIndexService.index_single_file` 创建 pending job 后调用 `run_indexing_job`。
8. `VectorIndexService.run_indexing_job` 通过 `_load_document_metadata` 调用 `DocumentLoaderRegistry.load`，支持 `txt/md/markdown/csv/html/htm/json`。
9. `app/ingestion/metadata.py::DocumentMetadataNormalizer.document_metadata` 根据 `space_id + source_path + content` 生成 `document_id`, `content_hash`, `source_path`, `_source`, `_file_name` 等。
10. `app/services/document_splitter_service.py::DocumentSplitterService.split_document` 根据扩展名选择 `split_markdown` 或 `split_text`。
11. `split_markdown` 使用 `MarkdownHeaderTextSplitter` 先按 `#`/`##` 保留标题，再用 `RecursiveCharacterTextSplitter` 二次切分，并调用 `_merge_small_chunks` 合并小块。
12. `split_text` 使用 `RecursiveCharacterTextSplitter.create_documents`，当前 `chunk_size=self.chunk_size * 2`，默认配置来自 `CHUNK_MAX_SIZE=800`，实际 splitter chunk_size 为 1600。
13. 每个 chunk 经 `DocumentMetadataNormalizer.chunk_metadata` 加上 `chunk_index`, `chunk_id`, `heading_path`。
14. `VectorIndexService._delete_existing_document_chunks` 在写入前调用 `vector_store.delete_by_document_id` 和 `delete_by_source` 清理旧 chunk。
15. `VectorIndexService.run_indexing_job` 调用 `self.vector_store.add_documents(documents)` 写入向量库；默认 `self.vector_store` 是 `app/services/vector_store_manager.py::vector_store_manager`。
16. `app/services/vector_store_manager.py::VectorStoreManager.add_documents` 转到 `app/providers/milvus.py::MilvusVectorStoreProvider.add_documents`。
17. `MilvusVectorStoreProvider.add_documents` 懒加载 `get_vector_store`，通过 LangChain Milvus 调用 embedding provider 并写入 Milvus collection。
18. 默认 embedding provider 由 `app/services/vector_embedding_service.py::build_vector_embedding_service` 创建 `DashScopeEmbeddingProvider`，模型为 `text-embedding-v4`，维度 1024。
19. 索引任务和文档目录分别保存到 `app/ingestion/job_store.py` 的 SQLite/Postgres/Memory store 和 `app/knowledge/catalog.py` 的 SQLite/Postgres/Memory catalog；默认 SQLite 路径来自 `app/config.py::Settings.storage`。

### 向量库存储位置

| 数据 | 默认位置/collection | 代码路径 |
| --- | --- | --- |
| 向量 chunk | Milvus collection `biz` | `app/core/milvus_client.py::MilvusClientManager.COLLECTION_NAME`, `app/providers/factory.py::create_default_provider_container` |
| Milvus 数据文件 | `volumes/milvus` | `vector-database.yml` |
| MinIO/etcd 数据 | `volumes/minio`, `volumes/etcd` | `vector-database.yml` |
| 会话历史 | `data/sessions.sqlite3` | `app/config.py::Settings.session_store_sqlite_path`, `app/providers/factory.py` |
| 检索审计 | `data/retrieval-audits.sqlite3` | `app/config.py::Settings.retrieval_audit_sqlite_path`, `app/observability/retrieval_audit.py` |
| 索引任务 | `data/indexing-jobs.sqlite3` | `app/config.py::Settings.indexing_job_store_sqlite_path`, `app/ingestion/job_store.py` |
| 文档目录 | `data/document-catalog.sqlite3` | `app/config.py::Settings.document_catalog_sqlite_path`, `app/knowledge/catalog.py` |
| LangGraph checkpoint | `data/checkpoints.sqlite3` | `app/config.py::Settings.checkpoint_sqlite_path`, `app/providers/checkpoints.py` |

### 检索、排序和上下文注入

1. `app/agents/rag_graph.py::RagGraphNodes.retrieve` 或 `app/services/rag_agent_service.py::retrieve_context` 创建 `RetrievalRequest`。
2. `app/providers/factory.py::create_default_provider_container` 默认把 `VectorStoreRetrieverProvider` 包进 `app/retrieval/pipeline.py::RetrievalPipeline`。
3. `app/providers/retrieval.py::VectorStoreRetrieverProvider.retrieve` 调用 `vector_store_provider.similarity_search(query, k=top_k, filters=request.filters)`。
4. `app/providers/milvus.py::MilvusVectorStoreProvider.similarity_search` 把 filters 作为 `filter` 传给 LangChain Milvus，并返回 `Document` 列表。
5. `RetrievalPipeline.retrieve` 可选调用 `LLMQueryRewriter.rewrite`，再合并 primary/additional retriever 结果。
6. `RetrievalPipeline._deduplicate_documents` 优先按 `chunk_id` 去重，没有 `chunk_id` 时按 `source_path/_source/source + page_content` 去重。
7. 如果 `RERANKER_PROVIDER=llm`，`RetrievalPipeline.retrieve` 调用 `LLMReranker.rerank`；否则保留向量库返回顺序。
8. 如果 `CONTEXT_COMPRESSOR_PROVIDER=llm`，`RetrievalPipeline.retrieve` 调用 `LLMContextCompressor.compress`；否则原样注入。
9. `RetrievalPipeline.retrieve` 最后截断到 `top_k`，用 `_source_from_document` 生成 `RetrievalSource`。
10. `app/agents/rag_graph.py::_build_answer_prompt` 把 `retrieval_result.documents` 拼入 `Retrieved context`，传给 `ChatModelAnswerGenerator.generate/stream`。

## 4. 关键模块表

| 文件路径 | 模块职责 | 关键类/函数 | 被谁调用 | 调用了谁 | 风险等级 | 我应该优先学习的原因 |
| --- | --- | --- | --- | --- | --- | --- |
| `app/main.py` | FastAPI 组装、生命周期、路由、静态 UI。 | `create_app`, `create_lifespan`, `build_uvicorn_options` | Uvicorn, `start.ps1`, tests | `chat.router`, `file.router`, `health.create_health_router`, `install_runtime_controls_middleware`, `milvus_manager.connect` | 高 | 这是运行时入口，决定哪些中间件、路由和后台 worker 生效。 |
| `app/config.py` | 配置默认值和 `.env` 映射。 | `Settings`, `ProviderConfig`, `StorageConfig`, `AgentConfig`, `RagConfig` | 几乎所有模块 | Pydantic Settings | 高 | 配置决定 Provider、RAG、Agent、存储、安全、部署行为。 |
| `app/api/chat.py` | 问答 API 边界。 | `chat`, `chat_stream`, `format_stream_chunk`, `_require_session_space_access` | 前端 `static/app.js`, HTTP clients, tests | `rag_agent_service`, `require_permission`, `require_space_access`, `success_envelope` | 高 | 用户问题从这里进入，错误和权限边界也在这里。 |
| `app/services/rag_agent_service.py` | RAG/Agent 核心服务。 | `RagAgentService`, `query_with_trace`, `query_stream_with_trace`, `_build_default_explicit_graph`, `_record_retrieval_audit` | `app/api/chat.py`, evaluation service, tests | `build_rag_state_graph`, ProviderContainer, session/audit/metrics stores | 高 | 项目主干调用链汇聚点，连接模型、检索、工具、审计和会话。 |
| `app/agents/rag_graph.py` | 显式 Agent/RAG 状态图。 | `RagGraphNodes`, `build_rag_state_graph`, `ChatModelAnswerGenerator`, `LangChainToolExecutor`, `_build_answer_prompt` | `RagAgentService._build_default_explicit_graph`, graph tests | `RetrieverProvider.retrieve`, `model.invoke/stream`, tool executor | 高 | 决定 RAG、工具、拒答、错误策略和最终响应的状态机。 |
| `app/providers/factory.py` | Provider 组合根。 | `ProviderContainer`, `create_default_provider_container`, `get_default_provider_container` | `RagAgentService`, `app/api/file.py`, tools, tests | DeepSeek/DashScope/OpenAI/fake/Milvus/SQLite/Postgres providers, `RetrievalPipeline` | 高 | 理解可替换架构和默认运行路径的入口；默认 DeepSeek chat + DashScope embedding。 |
| `app/providers/contracts.py` | Provider 协议和跨模块数据模型。 | `RetrievalRequest`, `RetrievalResult`, `RetrievalSource`, protocol classes | Provider、RAG、retrieval、tests | 无业务调用 | 中 | 所有边界类型的 source of truth。 |
| `app/retrieval/pipeline.py` | 检索增强流水线。 | `RetrievalPipeline.retrieve`, `LLMQueryRewriter`, `LLMReranker`, `LLMContextCompressor` | `ProviderFactory` 创建的 retriever provider | primary/additional retrievers, chat model provider | 高 | 排序、去重、重排、压缩和 debug 信息都在这里。 |
| `app/retrieval/profiles.py` | 检索 profile 和 high_recall 分支。 | `RetrievalProfile`, `RequestTransformingRetriever`, `build_retrievers_for_profile` | `ProviderFactory` | `RetrieverProvider.retrieve` | 中 | 影响召回率和 filter 保留策略，关系到知识空间隔离。 |
| `app/api/file.py` | 文件、知识空间、索引任务 API。 | `upload_file`, `_call_index_single_file`, `delete_document`, `rebuild_document` | 前端 `RAGApp`, HTTP clients, tests | `secure_upload_payload`, `VectorIndexIngestionProvider`, `vector_index_service` | 高 | 文档进入系统的外部入口，安全和索引错误首先在这里暴露。 |
| `app/security/uploads.py` | 上传安全边界。 | `secure_upload_payload`, `sanitize_upload_filename`, `resolve_upload_path`, `scan_prompt_injection` | `app/api/file.py`, tests | Python `Path`, regex | 高 | 防路径逃逸、非法文件和简单 prompt injection；策略较基础。 |
| `app/services/vector_index_service.py` | 索引主流程。 | `VectorIndexService`, `run_indexing_job`, `_load_document_metadata`, `_delete_existing_document_chunks` | Ingestion provider, file API, tests | loaders, splitter, metadata normalizer, vector store, job/catalog stores | 高 | 决定文档如何变成 chunk 并写入向量库。 |
| `app/ingestion/loaders.py` | 多格式 UTF-8 文档加载。 | `DocumentLoaderRegistry`, loader classes | `VectorIndexService._load_document_metadata` | 文件系统、csv/json/html parser | 中 | 决定哪些文件可进入索引以及内容如何被抽取。 |
| `app/services/document_splitter_service.py` | chunk 切分策略。 | `DocumentSplitterService.split_document`, `split_markdown`, `split_text` | `VectorIndexService.run_indexing_job` | LangChain splitters | 高 | chunk 质量直接影响检索质量；当前 text splitter 实际 chunk_size 是配置的 2 倍。 |
| `app/ingestion/metadata.py` | 稳定 document/chunk 元数据。 | `DocumentMetadataNormalizer.document_metadata`, `chunk_metadata` | `VectorIndexService` | SHA-256 | 高 | 知识空间隔离、去重、删除和 source 引用依赖这些字段。 |
| `app/providers/milvus.py` | Milvus vector store provider。 | `MilvusVectorStoreProvider.get_vector_store`, `add_documents`, `similarity_search` | `VectorStoreRetrieverProvider`, `VectorStoreManager`, tests | `MilvusClientManager`, LangChain Milvus | 高 | 向量库实际读写点；检索异常返回空列表是重要风险。 |
| `app/core/milvus_client.py` | Milvus collection 管理。 | `MilvusClientManager.connect`, `_create_collection`, `_create_index` | `MilvusVectorStoreProvider`, `app/main.py` | `pymilvus` | 高 | 会创建/加载 collection；维度不匹配时会 drop/recreate `biz`。 |
| `app/services/vector_embedding_service.py` | 默认 embedding provider。 | `build_vector_embedding_service`, `LazyEmbeddingProvider` | `ProviderFactory`, `VectorStoreManager` | `DashScopeEmbeddingProvider` | 中 | embedding 维度、模型和 API key 校验关系到 Milvus schema。 |
| `app/providers/deepseek.py` | DeepSeek chat provider。 | `DeepSeekChatModelProvider` | `ProviderFactory`, tests | OpenAI-compatible SDK against DeepSeek endpoint | 高 | 默认候选 chat provider；使用共享 `CHAT_MODEL`。 |
| `app/providers/dashscope.py` | DashScope chat/embedding provider。 | `DashScopeChatModelProvider`, `DashScopeEmbeddingProvider` | `ProviderFactory`, embedding service, tests | LangChain/OpenAI-compatible client | 中 | 默认 embedding provider；DashScope chat 需显式 `CHAT_PROVIDER=dashscope`。 |
| `app/providers/openai_compatible.py` | OpenAI-compatible chat/embedding provider。 | `OpenAICompatibleChatModelProvider`, `OpenAICompatibleEmbeddingProvider` | `ProviderFactory` | OpenAI-compatible SDK | 中 | 备用模型接入路径；chat 使用共享 `CHAT_MODEL`。 |
| `app/extensions/tools.py` | 工具注册表。 | `ToolRegistry`, `build_default_tool_registry`, `build_enabled_tools` | `RagAgentService.__init__` | `retrieve_knowledge`, `get_current_time` | 中 | 决定 Agent 可调用工具集合。 |
| `app/tools/knowledge_tool.py` | 知识库工具。 | `retrieve_knowledge`, `retrieve_knowledge_with_provider`, `enforce_knowledge_space` | Legacy agent/tool executor/tests | ProviderContainer retriever | 高 | 工具路径下的知识空间隔离和检索输出格式在这里。 |
| `app/security/auth.py` | 认证授权。 | `AuthContext`, `SimpleAuthProvider`, `require_permission`, `require_space_access` | API routes | 配置 `AUTH_*` | 高 | 默认 `AUTH_ENABLED=false`，生产必须确认开启。 |
| `app/security/runtime_controls.py` | 进程内并发和超时控制。 | `RuntimeControlSettings`, `install_runtime_controls_middleware` | `app/main.py` | FastAPI middleware | 中 | 只提供单进程控制，不等于生产多实例容量验证。 |
| `app/observability/request_context.py` | trace id 和 access log middleware。 | `install_request_context_middleware`, `get_current_trace_id` | `app/main.py`, `RagAgentService` | ContextVar, logger, metrics | 中 | trace id 贯穿审计和指标。 |
| `app/observability/retrieval_audit.py` | 检索审计 store。 | `RetrievalAuditRecord`, `SQLiteRetrievalAuditStore`, `PostgresRetrievalAuditStore` | `RagAgentService`, chat audit API | SQLite/Postgres | 中 | 追踪检索证据和问题回答链路。 |
| `app/observability/metrics.py` | 指标聚合和 Prometheus 输出。 | `RuntimeMetrics`, `record_rag_query`, `render_prometheus_metrics` | request middleware, `RagAgentService`, metrics API | 内存 Counter | 中 | 判断运行状态和 RAG latency/token 的基础。 |
| `app/operations/health.py` | 健康检查。 | `HealthChecker`, `create_default_health_checker` | `app/main.py`, health API, smoke tests | Provider/config/Milvus 检查 | 中 | 区分依赖健康和配置健康。 |
| `app/operations/config_validation.py` | 配置验证。 | `validate_settings`, `_validate_production_settings`, `main` | `start.ps1`, tests | `Settings` | 高 | 启动前和生产安全门禁在这里。 |
| `app/evaluation/runner.py` | 评估 CLI。 | `main`, `_build_faithfulness_judge` | `scripts/run-evaluation.ps1`, CI | datasets, metrics, fake/http/service evaluators | 中 | 质量评估入口，但 fake 模式不代表真实质量。 |
| `static/app.js` | 前端交互。 | `RAGApp.sendQuick`, `sendStream`, `handleFileUpload`, `refreshChatHistoriesFromBackend` | 浏览器 | `/api/chat`, `/api/chat_stream`, `/api/upload`, management APIs | 中 | 用户实际操作入口，影响请求字段和展示能力。 |
| `scripts/validate-baseline.ps1` | 基线校验脚本。 | 脚本命令 | CI, 开发者 | pytest, node tests, preflight | 中 | 本地/CI 验证的总入口。 |
| `start.ps1` | 本地启动编排。 | `Ensure-PythonDependencies`, `Assert-AppConfiguration`, `Start-MilvusStack`, `Start-FastApiForeground` | 开发者 | Docker, config validation, Uvicorn | 高 | 本地运行和 Milvus 预检的主入口。 |

## 5. 不确定项

1. `docs/` 当前已恢复，但 `.gitignore` 仍包含 `docs/`。Open question：`docs/` 应作为版本化项目文档，还是作为本地/生成文档保留在 ignore 中？
2. `app/core/milvus_client.py::MilvusClientManager.connect` 在 vector dim 不匹配时会 `utility.drop_collection("biz")` 后重建。Open question：生产/试运行环境是否允许这种破坏性重建，是否需要显式迁移或备份门禁？
3. `app/providers/factory.py::create_default_provider_container` 和 `app/core/milvus_client.py::MilvusClientManager.COLLECTION_NAME` 都固定使用 collection `biz`。Open question：是否应将 collection name 做成配置项以支持多环境或多业务隔离？
4. `app/security/auth.py::SimpleAuthProvider.authenticate` 默认 `AUTH_ENABLED=false` 返回 `local-admin/admin/*`。Open question：当前内部试用或生产部署是否已经通过 `.env` 和 `config_validation` 强制开启 auth？
5. `app/security/uploads.py::scan_prompt_injection` 只匹配少量英文短语，且只接受 UTF-8 文本。Open question：是否需要支持中文 prompt injection、混淆写法、压缩包/二进制文档和更强的内容安全策略？
6. `app/providers/milvus.py::MilvusVectorStoreProvider.similarity_search` 捕获异常后返回 `[]`。Open question：检索故障是否应显式上报到 `error_policy`、health 或 metrics，而不是被当作无结果？
7. `app/services/vector_store_manager.py::VectorStoreManager.similarity_search` 没有 `filters` 参数，但 `app/providers/contracts.py::VectorStoreProvider.similarity_search` 有 filters。Open question：该兼容 wrapper 是否还会被检索路径直接使用；若会，知识空间过滤会不会丢失？
8. `app/services/document_splitter_service.py::DocumentSplitterService.__init__` 对 text splitter 使用 `chunk_size=self.chunk_size * 2`。Open question：这是有意扩大 chunk，还是历史兼容导致配置名 `CHUNK_MAX_SIZE` 与实际行为不一致？
9. `app/services/rag_agent_service.py` 同时保留 `explicit_graph` 和 `legacy` 两套 Agent runtime。Open question：`legacy` 是否仍是受支持路径，还是仅用于兼容/回归测试？
10. 已移除 pre-retrieval planner（`LangChainToolPlanner` / `TOOL_PLANNING_*`）。当前工具路径：显式 `tool_request`，或 answer 阶段模型 `tool_calls` 触发的 answer↔tool 续轮。
11. `app/retrieval/profiles.py::high_recall` 会增加 relaxed filter fallback，但保留 `space_id/tenant_id` 等键。Open question：未来是否存在更多租户/权限过滤字段需要加入 `RETRIEVAL_RELAXED_FILTER_PRESERVE_KEYS`？
12. `app/evaluation/*` 已有 fake/service/http/real readiness 路径。Open question：当前是否存在真实业务 golden dataset 和真实 provider 环境用于评估答案质量，还是只完成了评估框架和 fake/预检？
13. `app/security/runtime_controls.py` 是进程内并发/超时控制。Open question：多 worker、多实例、反向代理限流和 Postgres/Milvus 连接池容量是否已有外部设计文档？
14. `app/observability/metrics.py::RuntimeMetrics` 是进程内聚合。Open question：生产是否会接 Prometheus scrape，多实例指标如何汇总？
15. `start.ps1` 会自动尝试安装依赖并启动/复用 Docker Milvus。Open question：CI、开发机、生产环境是否都应使用同一启动脚本，还是 production 应有独立部署流程？
16. `data/*.sqlite3` 保存本地状态但被 `.gitignore` 忽略。Open question：这些 SQLite 文件是否仅用于本地开发，内部试用是否计划切到 Postgres provider？
17. `app/api/chat.py::chat` 对普通异常返回业务 envelope，HTTP status 可能保持成功响应。Open question：外部客户端应以 HTTP status 还是 envelope `code/data.success` 为准？
18. `static/app.js::RAGApp.sendStream` 当前主要渲染 `content/done`，对 `retrieval/tool_call/tool_result/error_policy` 事件展示有限。Open question：前端是否需要显式展示检索证据、工具调用和错误策略？
