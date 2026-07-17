# 术语表

本术语表解释本项目中的关键概念、模块、类、函数和配置项。每个条目都指向实际文件路径。

| 术语 | 含义 | 路径 / 关键函数或类 |
| --- | --- | --- |
| FastAPI 应用入口 | 创建 Web 应用、安装 middleware、挂载 API 和静态文件的入口。 | `app/main.py`：`create_app()` |
| Lifespan | 应用启动/关闭时连接 Milvus、启动/停止后台索引 worker 的生命周期函数。 | `app/main.py`：`create_lifespan()` |
| Uvicorn 配置 | 从 settings 读取 host、port、reload 的运行参数。 | `app/main.py`：`build_uvicorn_options()` |
| Chat API | 非流式知识库问答接口。 | `app/api/chat.py`：`chat()` |
| Chat Stream API | SSE 流式知识库问答接口。 | `app/api/chat.py`：`chat_stream()` |
| SSE chunk formatter | 把 graph event chunk 映射为前端可消费的类型。 | `app/api/chat.py`：`format_stream_chunk()` |
| Session API | 查询、列出、清空会话历史。 | `app/api/chat.py`：`list_sessions()`、`get_session_info()`、`clear_session()` |
| Retrieval Audit API | 查询最近检索审计记录。 | `app/api/chat.py`：`list_retrieval_audits()` |
| Upload API | 上传文件并触发索引。 | `app/api/file.py`：`upload_file()` |
| Knowledge Space API | 创建和列出知识空间。 | `app/api/file.py`：`create_knowledge_space()`、`list_knowledge_spaces()` |
| Document Lifecycle API | 列出、查看、删除、重建文档。 | `app/api/file.py`：`list_documents()`、`get_document()`、`delete_document()`、`rebuild_document()` |
| Index Job API | 查询和重试索引任务。 | `app/api/file.py`：`list_indexing_jobs()`、`get_indexing_job()`、`retry_indexing_job()` |
| ApiEnvelope | 标准 JSON 响应封装。 | `app/models/response.py`：`ApiEnvelope`、`success_envelope()`、`error_envelope()` |
| ChatRequest | Chat 请求模型，接受 `Id`、`Question`、`spaceId` 别名。 | `app/models/request.py`：`ChatRequest` |
| Settings | Pydantic settings 根对象，从 `.env` 读取配置。 | `app/config.py`：`Settings` |
| Grouped Config | 对 flat env settings 的只读分组视图。 | `app/config.py`：`AppConfig`、`ProviderConfig`、`StorageConfig`、`AgentConfig`、`RagConfig` |
| Config Validation | 启动前配置校验。 | `app/operations/config_validation.py`：`validate_settings()` |
| ProviderSelection | 根据 settings 解析 provider id。 | `app/providers/selection.py`：`ProviderSelection` |
| ProviderContainer | 当前应用所有可替换 provider 的组合对象。 | `app/providers/factory.py`：`ProviderContainer` |
| Provider Factory | 创建默认 provider graph 的中心。 | `app/providers/factory.py`：`create_default_provider_container()` |
| ChatModelProvider | 创建 chat model 的协议。 | `app/providers/contracts.py`：`ChatModelProvider` |
| EmbeddingProvider | 文档和 query embedding 协议。 | `app/providers/contracts.py`：`EmbeddingProvider` |
| VectorStoreProvider | 向量库写入、删除、相似度检索协议。 | `app/providers/contracts.py`：`VectorStoreProvider` |
| RetrieverProvider | 结构化检索协议。 | `app/providers/contracts.py`：`RetrieverProvider` |
| SessionStoreProvider | 会话消息持久化协议。 | `app/providers/contracts.py`：`SessionStoreProvider` |
| RetrievalAuditStoreProvider | 检索审计持久化协议。 | `app/providers/contracts.py`：`RetrievalAuditStoreProvider` |
| IngestionProvider | 文件/目录索引 provider 协议。 | `app/providers/contracts.py`：`IngestionProvider` |
| RetrievalRequest | 检索请求，包含 query、top_k、filters。 | `app/providers/contracts.py`：`RetrievalRequest` |
| RetrievalResult | 检索结果，包含 documents、sources、debug、rewritten query。 | `app/providers/contracts.py`：`RetrievalResult` |
| RetrievalSource | 引用友好的来源信息。 | `app/providers/contracts.py`：`RetrievalSource` |
| IngestionResult | 索引 provider 的统一返回结构。 | `app/providers/contracts.py`：`IngestionResult` |
| DeepSeekChatModelProvider | 默认候选 chat model provider（DeepSeek OpenAI-compatible API）。 | `app/providers/deepseek.py`：`DeepSeekChatModelProvider` |
| DashScopeChatModelProvider | DashScope chat model provider（需显式 `CHAT_PROVIDER=dashscope`）。 | `app/providers/dashscope.py`：`DashScopeChatModelProvider` |
| DashScopeEmbeddingProvider | 默认 embedding provider（DashScope OpenAI-compatible embedding）。 | `app/providers/dashscope.py`：`DashScopeEmbeddingProvider` |
| OpenAICompatible Provider | 通用 OpenAI-compatible chat/embedding provider。 | `app/providers/openai_compatible.py`：`OpenAICompatibleChatModelProvider`、`OpenAICompatibleEmbeddingProvider` |
| MilvusClientManager | Milvus 连接、collection、schema、index 管理器。 | `app/core/milvus_client.py`：`MilvusClientManager` |
| MilvusVectorStoreProvider | LangChain Milvus vector store provider。 | `app/providers/milvus.py`：`MilvusVectorStoreProvider` |
| VectorStoreManager | 兼容旧服务层的 vector store wrapper。 | `app/services/vector_store_manager.py`：`VectorStoreManager` |
| LazyEmbeddingProvider | 延迟初始化 embedding provider 的兼容 wrapper。 | `app/services/vector_embedding_service.py`：`LazyEmbeddingProvider` |
| RetrievalPipeline | 可组合 RAG 检索 pipeline。 | `app/retrieval/pipeline.py`：`RetrievalPipeline` |
| Query Rewriter | 可选 LLM 查询重写阶段。 | `app/retrieval/pipeline.py`：`LLMQueryRewriter` |
| Reranker | 可选 LLM 文档重排阶段。 | `app/retrieval/pipeline.py`：`LLMReranker` |
| Context Compressor | 可选 LLM 上下文压缩阶段。 | `app/retrieval/pipeline.py`：`LLMContextCompressor` |
| Retrieval Profile | 命名检索策略，如 `default`、`high_recall`。 | `app/retrieval/profiles.py`：`RetrievalProfile` |
| RequestTransformingRetriever | 根据 retrieval profile 调整 top_k 和 filters 的 retriever wrapper。 | `app/retrieval/profiles.py`：`RequestTransformingRetriever` |
| RagAgentService | RAG Agent 服务门面，负责编排 graph/legacy agent、会话、审计、指标。 | `app/services/rag_agent_service.py`：`RagAgentService` |
| Explicit Graph Runtime | 默认 Agent runtime，使用显式 LangGraph `StateGraph`。 | `app/services/rag_agent_service.py`：`agent_runtime`；`app/agents/rag_graph.py`：`build_rag_state_graph()` |
| Legacy Runtime | 可选 LangChain `create_agent` 兼容路径。 | `app/services/rag_agent_service.py`：`_run_legacy_query()` |
| RagGraphState | 显式 RAG graph 状态结构。 | `app/agents/rag_graph.py`：`RagGraphState` |
| RagGraphNodes | 显式 RAG graph 的节点集合。 | `app/agents/rag_graph.py`：`RagGraphNodes` |
| normalize_input | 清洗问题并初始化 graph state。 | `app/agents/rag_graph.py`：`RagGraphNodes.normalize_input()` |
| decide_retrieval | 决定走 retrieval、tool 或 handoff。 | `app/agents/rag_graph.py`：`RagGraphNodes.decide_retrieval()` |
| retrieve | 执行结构化检索。 | `app/agents/rag_graph.py`：`RagGraphNodes.retrieve()` |
| handoff | 无上下文或空问题时给出拒答。 | `app/agents/rag_graph.py`：`RagGraphNodes.handoff()` |
| answer | 用 chat model 基于 retrieved context 生成答案。 | `app/agents/rag_graph.py`：`RagGraphNodes.answer()` |
| error_policy | 将节点异常转为结构化失败状态。 | `app/agents/rag_graph.py`：`RagGraphNodes.error_policy()` |
| final_response | 生成 graph 终态响应。 | `app/agents/rag_graph.py`：`RagGraphNodes.final_response()` |
| ChatModelAnswerGenerator | 显式 graph 的答案生成器。 | `app/agents/rag_graph.py`：`ChatModelAnswerGenerator` |
| LangChainToolExecutor | 在显式 graph 中执行 LangChain tool。 | `app/agents/rag_graph.py`：`LangChainToolExecutor` |
| answer↔tool continuation | answer 阶段模型产出 tool_calls 后路由 tool，再回到 answer 续轮。 | `app/agents/rag_graph.py`：`route_after_answer` / `route_after_tool` |
| ToolRegistry | 工具注册表。 | `app/extensions/tools.py`：`ToolRegistry` |
| retrieve_knowledge | LangChain 知识检索工具。 | `app/tools/knowledge_tool.py`：`retrieve_knowledge()` |
| enforce_knowledge_space | 用 contextvar 强制检索工具使用请求 space。 | `app/tools/knowledge_tool.py`：`enforce_knowledge_space()` |
| get_current_time | 时间工具。 | `app/tools/time_tool.py`：`get_current_time()` |
| PromptProfile | 命名系统提示词。 | `app/prompts/profiles.py`：`PromptProfile` |
| build_system_prompt | 根据 profile name 返回系统提示词。 | `app/prompts/profiles.py`：`build_system_prompt()` |
| UploadSecurityPolicy | 上传安全策略。 | `app/security/uploads.py`：`UploadSecurityPolicy` |
| secure_upload_payload | 上传文件验证和安全路径解析。 | `app/security/uploads.py`：`secure_upload_payload()` |
| PromptInjectionFinding | 上传内容中发现的 prompt injection 模式。 | `app/security/uploads.py`：`PromptInjectionFinding` |
| DocumentLoaderRegistry | 根据扩展名选择 loader。 | `app/ingestion/loaders.py`：`DocumentLoaderRegistry` |
| TextDocumentLoader | UTF-8 TXT loader。 | `app/ingestion/loaders.py`：`TextDocumentLoader` |
| MarkdownDocumentLoader | UTF-8 Markdown loader。 | `app/ingestion/loaders.py`：`MarkdownDocumentLoader` |
| CSVDocumentLoader | UTF-8 CSV loader，一行一个 Document。 | `app/ingestion/loaders.py`：`CSVDocumentLoader` |
| HTMLDocumentLoader | HTML 可见文本 loader。 | `app/ingestion/loaders.py`：`HTMLDocumentLoader` |
| JSONDocumentLoader | JSON loader，顶层 list 时一项一个 Document。 | `app/ingestion/loaders.py`：`JSONDocumentLoader` |
| DocumentMetadataNormalizer | 生成文档和 chunk 的稳定 metadata。 | `app/ingestion/metadata.py`：`DocumentMetadataNormalizer` |
| DocumentSplitterService | Markdown/text 分块服务。 | `app/services/document_splitter_service.py`：`DocumentSplitterService` |
| VectorIndexService | 文档索引编排服务。 | `app/services/vector_index_service.py`：`VectorIndexService` |
| IndexingJob | 单次文档索引任务。 | `app/ingestion/jobs.py`：`IndexingJob` |
| IndexingJobStatus | 索引任务状态枚举。 | `app/ingestion/jobs.py`：`IndexingJobStatus` |
| IndexingQueue | 后台索引 job id 队列协议。 | `app/ingestion/queue.py`：`IndexingQueue` |
| BackgroundIndexingWorker | 进程内后台索引 worker。 | `app/ingestion/worker.py`：`BackgroundIndexingWorker` |
| KnowledgeSpace | 知识空间元数据。 | `app/knowledge/catalog.py`：`KnowledgeSpace` |
| DocumentRecord | 文档目录记录。 | `app/knowledge/catalog.py`：`DocumentRecord` |
| KnowledgeCatalog | 知识空间和文档目录存储。 | `app/knowledge/catalog.py`：`InMemoryKnowledgeCatalog`、`SQLiteKnowledgeCatalog`、`PostgresKnowledgeCatalog` |
| AuthContext | 当前用户、角色、可访问知识空间。 | `app/security/auth.py`：`AuthContext` |
| SimpleAuthProvider | disabled/dev_header/reverse_proxy 认证 provider。 | `app/security/auth.py`：`SimpleAuthProvider` |
| ROLE_PERMISSIONS | 角色到权限的映射。 | `app/security/auth.py`：`ROLE_PERMISSIONS` |
| Runtime Controls | 进程内并发和请求超时控制。 | `app/security/runtime_controls.py`：`RuntimeControlSettings`、`install_runtime_controls_middleware()` |
| CORS Options | FastAPI CORSMiddleware 参数构建。 | `app/security/cors.py`：`build_cors_options()` |
| Trace ID | 请求级追踪 id，header 是 `X-Trace-Id`。 | `app/observability/request_context.py`：`TRACE_ID_HEADER` |
| RuntimeMetrics | 进程内 HTTP/RAG 指标。 | `app/observability/metrics.py`：`RuntimeMetrics` |
| RetrievalAuditRecord | 一次 traced RAG 回答的审计快照。 | `app/observability/retrieval_audit.py`：`RetrievalAuditRecord` |
| HealthChecker | 聚合依赖健康检查。 | `app/operations/health.py`：`HealthChecker` |
| ConfigIssue | 配置校验问题。 | `app/operations/config_validation.py`：`ConfigIssue` |
| Integration Smoke | Milvus/API 健康 smoke。 | `app/operations/integration_smoke.py`：`run_integration_smoke()` |
| Postgres Smoke | Postgres store 配置和可选写路径 smoke。 | `app/operations/postgres_smoke.py`：`run_postgres_smoke()` |
| GoldenExample | 评测数据集样例。 | `app/evaluation/models.py`：`GoldenExample` |
| EvaluationReport | 评测报告模型。 | `app/evaluation/models.py`：`EvaluationReport` |
| Fake Evaluation | 不依赖真实 provider 的确定性评测。 | `app/evaluation/fake.py`：`run_fake_evaluation()` |
| Service Evaluation | 调用进程内 RAG service 的评测模式。 | `app/evaluation/service.py`：`run_service_evaluation()` |
| HTTP Evaluation | 调用运行中 API 的评测模式。 | `app/evaluation/http.py`：`HttpEvaluationClient` |
| start.ps1 | Windows 本地启动脚本。 | `start.ps1`：`Ensure-PythonDependencies`、`Start-MilvusStack`、`Start-FastApiForeground` |
| vector-database.yml | 本地 Milvus/etcd/MinIO/Attu compose 配置。 | `vector-database.yml` |
| baseline validation | 维护者定义的本地 baseline 检查集合。 | `scripts/validate-baseline.ps1` |
| run-evaluation | 评测 PowerShell wrapper。 | `scripts/run-evaluation.ps1` |
| CI baseline | Windows GitHub Actions baseline job。 | `.github/workflows/ci.yml` |

## 关键配置项

| 配置项 | 作用 | 默认值来源 |
| --- | --- | --- |
| `CHAT_PROVIDER` | 可选 chat model provider；留空时按有效 Key 自动选择（双 Key 时 DeepSeek-first）。 | `app/config.py`：`Settings.chat_provider` |
| `CHAT_MODEL` | 所有 chat provider 共用的模型名（默认 `deepseek-v4-pro`）。RAG 没有专用模型变量。 | `app/config.py`：`Settings.chat_model` |
| `DEEPSEEK_API_KEY` / `DEEPSEEK_BASE_URL` | DeepSeek chat 凭证与 endpoint。 | `app/config.py`：`Settings.deepseek_api_key`、`Settings.deepseek_base_url` |
| `EMBEDDING_PROVIDER` | 选择 embedding provider。 | `app/config.py`：`Settings.embedding_provider` |
| `VECTOR_STORE_PROVIDER` | 选择 vector store provider。 | `app/config.py`：`Settings.vector_store_provider` |
| `SESSION_STORE_PROVIDER` | 选择 session store。 | `app/config.py`：`Settings.session_store_provider` |
| `RETRIEVAL_AUDIT_STORE_PROVIDER` | 选择 retrieval audit store。 | `app/config.py`：`Settings.retrieval_audit_store_provider` |
| `INGESTION_PROVIDER` | 选择 ingestion provider。 | `app/config.py`：`Settings.ingestion_provider` |
| `CHECKPOINT_PROVIDER` | 选择 LangGraph checkpoint provider。 | `app/config.py`：`Settings.checkpoint_provider` |
| `AGENT_RUNTIME` | 选择 `explicit_graph` 或 `legacy`。 | `app/config.py`：`Settings.agent_runtime` |
| `ENABLED_TOOLS` | Agent 启用工具列表。 | `app/config.py`：`Settings.enabled_tools` |
| `ENABLED_TOOLS` | 启用的工具名列表。 | `app/config.py`：`Settings.enabled_tools` |
| `PROMPT_PROFILE` | 系统提示词 profile。 | `app/config.py`：`Settings.prompt_profile` |
| `RAG_TOP_K` | 默认检索 top_k。 | `app/config.py`：`Settings.rag_top_k` |
| `RETRIEVAL_PROFILE` | retrieval profile 名称。 | `app/config.py`：`Settings.retrieval_profile` |
| `QUERY_REWRITER_PROVIDER` | query rewrite provider，默认 `none`；`llm` 时复用 `chat_model_provider`。 | `app/config.py`：`Settings.query_rewriter_provider` |
| `RERANKER_PROVIDER` | reranker provider，默认 `none`；`llm` 时复用 `chat_model_provider`。 | `app/config.py`：`Settings.reranker_provider` |
| `CONTEXT_COMPRESSOR_PROVIDER` | context compressor provider，默认 `none`；`llm` 时复用 `chat_model_provider`。 | `app/config.py`：`Settings.context_compressor_provider` |
| `CHUNK_MAX_SIZE` | 文档分块基础大小。 | `app/config.py`：`Settings.chunk_max_size` |
| `CHUNK_OVERLAP` | 文档分块重叠。 | `app/config.py`：`Settings.chunk_overlap` |
| `AUTH_ENABLED` | 是否启用内部认证。 | `app/config.py`：`Settings.auth_enabled` |
| `AUTH_PROVIDER` | `dev_header` 或 `reverse_proxy`。 | `app/config.py`：`Settings.auth_provider` |
| `RUNTIME_CONTROLS_ENABLED` | 是否启用进程内并发/超时控制。 | `app/config.py`：`Settings.runtime_controls_enabled` |
| `INDEXING_EXECUTION_MODE` | `sync` 或 `background`。 | `app/config.py`：`Settings.indexing_execution_mode` |
| `INDEXING_QUEUE_PROVIDER` | 后台索引队列 provider。 | `app/config.py`：`Settings.indexing_queue_provider` |
| `DOCUMENT_CATALOG_PROVIDER` | 文档目录 provider。 | `app/config.py`：`Settings.document_catalog_provider` |
| `DASHSCOPE_API_KEY` | DashScope embedding（及显式 DashScope chat）密钥。 | `app/config.py`：`Settings.dashscope_api_key` |
| `DASHSCOPE_EMBEDDING_MODEL` | 仅用于 embedding 的模型名，与 `CHAT_MODEL` 独立。 | `app/config.py`：`Settings.dashscope_embedding_model` |
| `MILVUS_HOST` / `MILVUS_PORT` | Milvus 连接地址。 | `app/config.py`：`Settings.milvus_host`、`Settings.milvus_port` |
| `DEPLOYMENT_ENVIRONMENT` | local/staging/production 部署环境。 | `app/config.py`：`Settings.deployment_environment` |
