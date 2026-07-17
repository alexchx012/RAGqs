## 1. 项目地图学习

- [x] 1.1 阅读 `app/main.py` 的 `create_app()`、`create_lifespan()`、`build_uvicorn_options()`，画出 FastAPI 启动、middleware、router、static UI 的入口图。
- [x] 1.2 阅读 `pyproject.toml`、`.env.example`、`app/config.py` 的 `Settings` 和 grouped config properties，整理依赖、环境变量和默认运行配置。
- [x] 1.3 阅读 `openspec/specs/` 下现有 baseline specs，对照当前 `app/` 实现确认哪些是已归档 baseline，哪些仍只是运行限制或非声明。
- [x] 1.4 阅读 `README.md`、`start.ps1`、`vector-database.yml`、`.github/workflows/ci.yml`，整理本地启动、CI、Docker/Milvus 和 smoke gate。

## 2. HTTP API 和 UI 学习

- [x] 2.1 阅读 `app/api/chat.py` 的 `chat()`、`chat_stream()`、`list_sessions()`、`list_retrieval_audits()`，整理 chat/session/audit API 的请求、响应和权限。
- [x] 2.2 阅读 `app/api/file.py` 的 `upload_file()`、`create_knowledge_space()`、`list_documents()`、`delete_document()`、`retry_indexing_job()`，整理上传、知识空间、文档生命周期和索引任务 API。
- [x] 2.3 阅读 `app/api/health.py` 的 `create_health_router()` 和 `app/api/metrics.py` 的 `create_metrics_router()`，整理运维 API。
- [x] 2.4 阅读 `static/app.js` 的 `RAGApp`，对照后端 API 标出 UI 调用路径和本地会话历史逻辑。

## 3. RAG 查询链路学习

- [x] 3.1 阅读 `app/services/rag_agent_service.py` 的 `RagAgentService.query_with_trace()`、`query_stream_with_trace()`、`retrieve_context()`，整理非流式和流式 RAG 服务入口。
- [x] 3.2 阅读 `app/agents/rag_graph.py` 的 `build_rag_state_graph()` 和 `RagGraphNodes`，画出 `normalize_input -> decide_retrieval -> retrieve/tool/handoff -> answer/error_policy -> final_response` 的图。
- [x] 3.3 阅读 `app/retrieval/pipeline.py` 的 `RetrievalPipeline.retrieve()`，整理 rewrite、retrieve、deduplicate、rerank、compress、source 序列化阶段。
- [x] 3.4 阅读 `app/retrieval/profiles.py` 的 `build_default_retrieval_profile_registry()` 和 `build_retrievers_for_profile()`，理解 `default` 与 `high_recall` profile 的差异。
- [x] 3.5 阅读 `app/providers/retrieval.py` 的 `VectorStoreRetrieverProvider.retrieve()` 和 `app/providers/milvus.py` 的 `MilvusVectorStoreProvider.similarity_search()`，确认向量检索 provider 边界。

## 4. Agent 和工具学习

- [x] 4.1 阅读 `app/extensions/tools.py` 的 `ToolRegistry`、`build_default_tool_registry()`、`build_enabled_tools()`，整理工具注册和 `ENABLED_TOOLS` 配置。
- [x] 4.2 阅读 `app/tools/knowledge_tool.py` 的 `retrieve_knowledge()`、`enforce_knowledge_space()`、`resolve_knowledge_space_id()`，确认 legacy Agent 工具如何强制知识空间。
- [x] 4.3 阅读 `app/tools/time_tool.py` 的 `get_current_time()`，确认非 RAG 工具示例。
- [x] 4.4 阅读 `app/agents/rag_graph.py` 的 `LangChainToolExecutor`、`route_after_answer` / `route_after_tool`，确认显式图工具分支：`decide_retrieval` 仅显式 `tool_request`，answer 阶段可 answer↔tool 续轮。
- [x] 4.5 阅读 `app/prompts/profiles.py` 的 `PromptProfileRegistry` 和 `build_system_prompt()`，整理 prompt profile 对 Agent 的影响。

## 5. 文档上传和索引学习

- [x] 5.1 阅读 `app/security/uploads.py` 的 `secure_upload_payload()`、`sanitize_upload_filename()`、`scan_prompt_injection()`，整理上传前安全边界。
- [x] 5.2 阅读 `app/providers/ingestion.py` 的 `VectorIndexIngestionProvider.index_file()`，区分同步索引和后台索引模式。
- [x] 5.3 阅读 `app/services/vector_index_service.py` 的 `VectorIndexService.index_single_file()`、`create_pending_indexing_job()`、`run_indexing_job()`，画出索引任务生命周期。
- [x] 5.4 阅读 `app/ingestion/loaders.py` 的 `DocumentLoaderRegistry` 和各 loader，整理 TXT、Markdown、CSV、HTML/HTM、JSON 的加载方式。
- [x] 5.5 阅读 `app/services/document_splitter_service.py` 的 `DocumentSplitterService.split_document()`，整理 Markdown 和普通文本分块策略。
- [x] 5.6 阅读 `app/ingestion/metadata.py` 的 `DocumentMetadataNormalizer`，确认 `document_id`、`chunk_id`、`space_id`、`content_hash` 如何生成。
- [x] 5.7 阅读 `app/knowledge/catalog.py` 的 `SQLiteKnowledgeCatalog`、`PostgresKnowledgeCatalog`，整理知识空间和文档目录状态。

## 6. Provider、状态存储和配置学习

- [x] 6.1 阅读 `app/providers/factory.py` 的 `create_default_provider_container()`，整理 chat、embedding、vector store、retriever、session、audit、ingestion、checkpoint provider 的装配规则。
- [x] 6.2 阅读 `app/providers/selection.py` 的 `ProviderSelection` 和 `validate_provider_selection()`，列出支持的 provider id。
- [x] 6.3 阅读 `app/providers/contracts.py` 的 `RetrievalRequest`、`RetrievalResult`、`RetrievalSource`、`StoredMessage`、`IngestionResult` 和 Provider Protocol。
- [x] 6.4 阅读 `app/providers/sqlite_session.py`、`app/providers/postgres_session.py`、`app/providers/checkpoints.py`，整理 session 和 checkpoint 持久化边界。
- [x] 6.5 阅读 `app/ingestion/queue.py`、`app/ingestion/job_store.py`、`app/ingestion/worker.py`，整理后台索引队列、job store 和 worker 恢复逻辑。

## 7. 安全、观测和运维学习

- [x] 7.1 阅读 `app/security/auth.py` 的 `SimpleAuthProvider`、`AuthContext`、`require_permission()`、`require_space_access()`，整理角色、权限和知识空间访问控制。
- [x] 7.2 阅读 `app/security/cors.py`、`app/security/runtime_controls.py`，整理 CORS 和进程内并发/超时控制。
- [x] 7.3 阅读 `app/observability/request_context.py`，整理 `X-Trace-Id`、访问日志和 HTTP metrics。
- [x] 7.4 阅读 `app/observability/retrieval_audit.py`，整理检索审计记录的字段、存储 provider 和查询路径。
- [x] 7.5 阅读 `app/operations/health.py`、`app/operations/config_validation.py`、`app/operations/integration_smoke.py`、`app/operations/postgres_smoke.py`，整理健康检查和上线前门禁。

## 8. 测试和风险复核

- [x] 8.1 阅读 `tests/test_rag_state_graph.py`、`tests/test_rag_agent_graph_runtime.py`、`tests/test_chat_retrieval_trace.py`，确认 RAG/Agent 主链测试覆盖。
- [x] 8.2 阅读 `tests/test_vector_index_service_ingestion.py`、`tests/test_file_upload_ingestion.py`、`tests/test_knowledge_spaces_lifecycle.py`，确认索引和知识空间测试覆盖。
- [x] 8.3 阅读 `tests/test_phase0_baseline.py`，确认已恢复的 `docs/` 文件清单与 baseline 文档断言一致。
- [x] 8.4 阅读 `risk-register.md`，为每个高风险项决定是否另开 OpenSpec change。
- [x] 8.5 把 `design.md` 的 open questions 分成“需要运行验证”“需要产品决策”“需要代码修复”三类。
