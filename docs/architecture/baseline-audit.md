# RAGqs Baseline Audit

## Current Architecture

RAGqs is a FastAPI application with a static browser UI, a LangChain/LangGraph RAG agent, DashScope chat and embedding models, and Milvus for vector storage.

- `app/main.py`: FastAPI entry point, static file mount, router registration, Milvus startup check.
- `app/api/chat.py`: non-streaming and SSE chat endpoints plus session history, list, and search APIs.
- `app/api/file.py`: upload endpoint for configured TXT, Markdown, CSV, HTML, and JSON files with upload security validation before indexing.
- `app/services/rag_agent_service.py`: default explicit LangGraph `StateGraph` RAG runtime with a legacy `create_agent` compatibility mode.
- `app/tools/knowledge_tool.py`: retrieval tool that calls the vector store retriever.
- `app/services/vector_index_service.py`: file indexing from uploads, with synchronous execution by default and optional background job execution.
- `app/services/document_splitter_service.py`: Markdown header splitting and recursive text splitting.
- `app/services/vector_store_manager.py`: Milvus LangChain vector store wrapper, currently bound to collection `biz`.
- `app/core/milvus_client.py`: low-level Milvus connection and collection initialization.
- `static/`: single-page chat UI with backend-first history and localStorage fallback.

## Runtime Dependencies

- FastAPI serves the API and browser UI.
- Milvus, etcd, and MinIO are started by the default `vector-database.yml` stack; Attu is available through the optional `ui` Docker profile.
- DashScope is the default real chat and embedding provider; fake and OpenAI-compatible providers are selectable for tests or alternate deployments.
- LangGraph checkpoints default to local SQLite, with `CHECKPOINT_PROVIDER=memory` available for throwaway tests and `CHECKPOINT_PROVIDER=postgres` for multi-instance checkpoints.

## Known Limitations

- Settings now expose typed groups over the existing `.env` variables, but some runtime modules still read flat global config fields directly.
- Most runtime dependencies are now behind provider interfaces, but production collection selection and some document-management APIs are still tied to the current vector index service.
- Session state defaults to local SQLite so transcripts survive FastAPI restarts; memory remains available for throwaway tests, Postgres is available for multi-instance chat history, and the browser uses localStorage only as a fallback cache.
- Indexing defaults to synchronous execution and has retry, idempotent document ids, optional SQLite/Postgres job state, optional SQLite/Postgres document catalog storage, memory/Postgres queue providers, pending-job recovery on worker startup, and an in-process background worker mode for queued uploads.
- Retrieval still defaults to basic top-k vector search, but metadata filters, citation extraction, and debug trace data are now on the default path. `RETRIEVAL_PROFILE=high_recall` enables multi-retriever recall widening with protected space and tenant filters, and LLM-backed query rewrite, rerank, and context compression can be enabled by configuration.
- Agent orchestration now defaults to explicit `StateGraph`; model-driven tool planning and token streaming are available, with SQLite, memory, and Postgres checkpoint providers behind `CHECKPOINT_PROVIDER`.
- API response consistency has an initial shared `ApiEnvelope` helper for `code`, `message`, and `data` payloads. Metrics, health, chat trace/list/audit responses, and file/document/indexing endpoints now use the shared envelope path where their public shape already matches it; legacy response models such as session history remain for compatibility.
- Observability and operations now include request trace id propagation, structured access logs, in-process runtime metrics, Prometheus-compatible metrics export, retrieval audit storage, production deployment guardrails, running API health preflight, non-destructive Milvus/Postgres smoke gates, upload security validation, hosted CI baseline validation, and evaluation JSON artifacts.
- Tests now cover provider boundaries, ingestion, upload security, retrieval traces and audit storage, session storage/listing, SQLite session and indexing job persistence, frontend history state, startup scripts, and evaluation scaffolding.

## Target Foundation Direction

Move toward a layered foundation: configuration profiles, provider interfaces, ingestion pipeline, retrieval pipeline, explicit LangGraph orchestration, service-side session/document stores, evaluation harness, and operational guardrails. Each layer should be independently testable and replaceable without rewriting API handlers or UI behavior.

## Phase 1 Progress

The repository now has an initial `app/providers/` boundary:

- `app/config.py`: typed grouped views for app, CORS, uploads, providers, storage, agent, OpenAI-compatible, DashScope, Milvus, RAG, and chunking settings while preserving the existing flat environment variable names.
- `contracts.py`: runtime-checkable protocols for chat models, embeddings, vector stores, retrievers, session stores, and ingestion.
- `fakes.py`: in-memory fake providers for backend tests that do not require DashScope or Milvus.
- `dashscope.py`: DashScope embedding provider using the OpenAI-compatible API.
- `milvus.py`: lazy Milvus vector store provider.
- `retrieval.py`: structured retriever provider that returns documents plus debug data.
- `ingestion.py`: compatibility adapter around the current vector index service.
- `postgres_session.py`: optional PostgreSQL `SessionStoreProvider` for multi-instance chat history.
- `factory.py`: default provider container that wires chat, embedding, vector store, retriever, session store, retrieval audit store, ingestion, and checkpoint providers without opening external connections during construction.

`app/services/vector_embedding_service.py` is now a lazy compatibility wrapper instead of constructing the DashScope client at import time. `app/services/vector_store_manager.py` delegates to `MilvusVectorStoreProvider`, so vector store construction is behind a provider boundary. `app/tools/knowledge_tool.py` now retrieves through `RetrieverProvider`, which gives agents and future evaluators the same retrieval contract. `app/api/file.py` now sends upload indexing through `ProviderContainer.ingestion_provider` while preserving upload security validation and the existing indexing-status response.

Current Phase 1 test coverage includes grouped settings, provider protocols, fake providers, lazy DashScope/Milvus construction, provider container wiring, `RagAgentService` chat model injection, provider-backed knowledge retrieval, upload indexing through the ingestion provider, service-side session store injection, SQLite/Postgres session persistence, Postgres indexing-job persistence, Postgres document catalog persistence, and Postgres checkpoint selection through injected factories. Remaining provider work is mainly production collection/knowledge-space selection and gradually moving remaining flat config reads to grouped settings.

## Phase 2 Progress

The repository now has an initial `app/ingestion/` foundation:

- `loaders.py`: UTF-8 TXT, Markdown, CSV, HTML/HTM, and JSON loader interfaces plus an extension-based registry.
- `metadata.py`: stable document metadata and chunk metadata normalization, including document id, chunk id, source path, content hash, extension, file name, heading path, and legacy `_source` fields.
- `jobs.py`: `IndexingJob` status model with pending, running, succeeded, failed, and partial terminal states.
- `job_store.py`: process-local and SQLite indexing job stores with job id, document id, source path, and status filters.
- `queue.py`: indexing queue protocol plus in-memory FIFO and Postgres-backed implementations with job deduplication.
- `worker.py`: in-process background indexing worker that executes queued job ids, can recover persisted pending jobs on startup, and shuts down through the FastAPI lifespan.
- `app/knowledge/catalog.py`: process-local, SQLite, and Postgres knowledge-space/document catalog implementations.

`app/services/vector_index_service.py` now accepts injectable loader, splitter, vector store, metadata normalizer, job store, and document catalog dependencies. Single-file indexing uses the loader registry, deletes previous chunks by stable `document_id`, clears legacy source-only chunks by source path, enriches chunks with normalized metadata, records successful and failed jobs, and returns an `IndexingJob`. Directory indexing now returns per-file job summaries. `INDEXING_EXECUTION_MODE=background` makes upload ingestion create a pending job and enqueue it for the FastAPI-managed background worker; `INDEXING_QUEUE_PROVIDER=memory` selects the local queue and `INDEXING_QUEUE_PROVIDER=postgres` plus `INDEXING_QUEUE_POSTGRES_DSN` selects a shared Postgres queue with row locking and lease reclaiming; `INDEXING_WORKER_RECOVER_PENDING_JOBS=true` re-enqueues persisted pending jobs on worker startup; and `sync` remains the default compatibility mode. `app/api/file.py` includes indexing status in successful upload responses, validates upload filename, extension, size, UTF-8 content, and prompt-injection risk before writing files, returns HTTP 500 details when synchronous indexing fails, and exposes `/index-jobs` endpoints for list, detail, and retry. `INDEXING_JOB_STORE_PROVIDER=sqlite` is the local durable default, `INDEXING_JOB_STORE_PROVIDER=postgres` enables multi-instance job status, `DOCUMENT_CATALOG_PROVIDER=sqlite` is the local durable document lifecycle metadata default, and `DOCUMENT_CATALOG_PROVIDER=postgres` enables multi-instance document lifecycle metadata. Milvus and fake vector store providers delete source-scoped chunks across `_source`, `source_path`, and `source` metadata keys for historical index compatibility.

## Phase 3 Progress

The repository now has an initial `app/retrieval/` foundation:

- `pipeline.py`: a `RetrieverProvider` implementation that composes query rewrite, one or more retrievers, deduplication, rerank, context compression, source extraction, and per-stage timing debug data.
- `RetrievalResult` now includes citation-friendly `sources` alongside retrieved documents and debug metadata.
- `ProviderContainer` now wraps the default vector-store retriever in `RetrievalPipeline`, so the knowledge tool uses the pipeline path without changing its public behavior.

`ProviderContainer` can select `RETRIEVAL_PROFILE=default` or `high_recall`, enable `LLMQueryRewriter` with `QUERY_REWRITER_PROVIDER=llm`, `LLMReranker` with `RERANKER_PROVIDER=llm`, and `LLMContextCompressor` with `CONTEXT_COMPRESSOR_PROVIDER=llm`; `CONTEXT_COMPRESSOR_MAX_CHARACTERS` bounds compressed chunk size before answer generation. `RagAgentService` now has `query_with_trace()` and `query_stream_with_trace()` methods backed by the explicit graph by default. `/chat` keeps the existing `answer` field and adds `sources`, `retrievalDebug`, and full `retrieval` metadata. `/chat_stream` emits graph events and a final `done` payload. Remaining retrieval-quality work includes real-provider evaluation against business datasets.

## Phase 4 Progress

The repository now has an initial `app/agents/` graph foundation:

- `rag_graph.py`: explicit LangGraph `StateGraph` builder with typed state and `normalize_input`, `decide_retrieval`, `retrieve`, `handoff`, `tool`, `answer`, `error_policy`, and `final_response` nodes plus `ChatModelAnswerGenerator`, `LangChainToolExecutor`, and `LangChainToolPlanner`.
- The graph accepts injectable `RetrieverProvider`, `AnswerGenerator`, `ToolExecutor`, `ToolPlanner`, and LangGraph checkpointer implementations, propagates sources and retrieval debug data into state, accumulates routing/retrieval/tool/handoff/answer/error-policy/final events, resets per-run transient events across checkpointed invocations, and can compile with memory, SQLite, or Postgres checkpoint providers.
- `RagAgentService` routes `query()`, `query_with_trace()`, `query_stream()`, and `query_stream_with_trace()` through a default explicit graph when `AGENT_RUNTIME=explicit_graph`.
- `TOOL_PLANNING_ENABLED=true` enables LangChain `bind_tools` planning for configured non-retrieval tools; `TOOL_PLANNING_EXCLUDED_TOOLS=retrieve_knowledge` keeps native RAG retrieval on the graph retrieval path by default.
- Streaming explicit-graph calls use LangGraph `stream_mode=["custom", "updates"]`; `ChatModelAnswerGenerator.stream()` emits token chunks through custom stream events while node updates carry retrieval, tool, error, and final `done` events.
- The graph records retrieval and answer-generation failures as structured `error` events instead of letting node exceptions escape, routes failures through an explicit `error_policy` node, skips answer generation when retrieval fails, returns a deterministic refusal when retrieval returns no usable context, and emits a graph-owned `done` payload for every terminal path.
- `app/api/chat.py` maps stream chunks for `retrieval_decision`, `retrieval`, `handoff`, `error_policy`, `source`, `tool_call`, `tool_result`, `token`, `error`, and `done`, keeping the SSE envelope stable while the graph runtime evolves.

This graph is tested as a standalone orchestration skeleton and through the service-level default runtime path. The compatibility `create_agent` path remains selectable through `AGENT_RUNTIME=legacy`; Postgres checkpoint selection is available for multi-instance graph state, while real database integration testing remains deployment-environment work.

## Phase 5 Progress

The repository now has an initial product capability foundation:

- `SessionStoreProvider` is wired into `RagAgentService` as an injectable dependency, with `InMemorySessionStoreProvider` available through the default provider container.
- Successful non-streaming and streaming answers record user and assistant messages server-side.
- Traced answers attach retrieval metadata and serialized sources to assistant messages.
- `get_session_history()` reads from the session store first and keeps checkpoint lookup as a compatibility fallback.
- `SessionStoreProvider.list_sessions()` returns searchable sidebar summaries, and `/chat/sessions?query=...` exposes them to clients.
- `SQLiteSessionStoreProvider` can persist chat transcripts and summaries locally when `SESSION_STORE_PROVIDER=sqlite`; `PostgresSessionStoreProvider` is available for multi-instance chat history when `SESSION_STORE_PROVIDER=postgres`.
- `clear_session()` clears the session store and attempts to clear the LangGraph checkpoint thread when supported.
- `app/knowledge/catalog.py` stores knowledge spaces and document records in memory, local SQLite, or Postgres.
- `VectorIndexService` records indexed documents by `space_id`, supports list/get/delete/rebuild operations, and preserves `space_id` in indexing jobs and chunk metadata.
- `/knowledge-spaces` and `/knowledge-spaces/{space_id}/documents` expose document lifecycle APIs backed by the configured catalog.
- `ChatRequest.spaceId`, `RagAgentService.retrieve_context(..., space_id=...)`, and `retrieve_knowledge(..., space_id=...)` pass knowledge-space filters into retrieval.
- Legacy `create_agent` tool execution now runs inside an enforced request-scoped knowledge-space context, so tool calls cannot silently fall back to another space during chat.
- The browser sidebar refreshes from backend session summaries, searches through `/chat/sessions`, and lazily loads `/chat/session/{id}` before rendering selected backend histories.

Knowledge-space and document catalog stores can use memory, local SQLite, or Postgres. Session, indexing-job, and checkpoint storage also have Postgres options for multi-instance deployments.

## Phase 6 Progress

The repository now has an initial deterministic evaluation foundation:

- `app/evaluation/`: typed golden examples, agent run results, faithfulness verdicts, aggregate metrics, JSONL dataset loading, a fake-provider runner, an async service runner for traced RAG services, and an HTTP client for running against `/chat` on a live FastAPI server.
- `data/evaluation/golden.jsonl`: a small default golden dataset covering grounded answers and unsupported-question refusal.
- `data/evaluation/business.example.jsonl` and `docs/business-samples/`: a stronger space-scoped business example dataset with matching Markdown sample sources.
- `scripts/run-evaluation.ps1`: a local evaluation command with `fake`, `service`, and `http` modes plus `-ReportPath` JSON artifact output. The baseline uses `fake` mode so it runs without DashScope, Milvus, or network access.
- `scripts/validate-baseline.ps1`: now includes the evaluation unit tests and fake evaluation command as regression gates.

Current metrics cover retrieval hit rate, expected answer trait coverage, answer faithfulness verdict score, citation accuracy, no-answer refusal rate, and average latency. The `FaithfulnessJudge` boundary exists with static deterministic and optional LLM-backed implementations; model judging now uses the configured chat provider container instead of a DashScope-only path. `service` and `http` evaluation propagate per-example `metadata.spaceId`, so golden datasets can target isolated knowledge spaces. Business dataset quality checks reject duplicate ids, unscoped examples, grounded examples without traits or sources, and refusal examples that define answer traits or sources. LangSmith tracing can be enabled through environment variables for real service evaluation, and CI can collect the JSON report written by `-ReportPath`.

## Phase 7 Progress

The repository now has an initial operations foundation:

- `app/observability/request_context.py`: FastAPI middleware for `X-Trace-Id` propagation, request-local trace id lookup, and structured `http_request` access log records with method, path, status code, and latency.
- `app/observability/retrieval_audit.py`: memory, SQLite, and Postgres retrieval audit stores for traced RAG answers, selected sources, retrieval debug payloads, session id, space id, and trace id.
- `app/observability/metrics.py`: process-local HTTP and RAG metrics collector with latency buckets, route/status counters, per-space query counts, token usage totals, and Prometheus-compatible text rendering.
- `app/models/response.py`: shared `ApiEnvelope`, `success_envelope`, `error_envelope`, and `envelope_json_response` helpers for HTTP routes that use the public `code`/`message`/`data` response envelope.
- `app/operations/health.py`: composable dependency health checks with explicit `app`, `modelProvider`, `embeddingProvider`, `vectorStore`, and `sessionStore` boundaries.
- `app/operations/config_validation.py`: shared startup configuration validation for DashScope credentials, RAG retrieval limits, chunking settings, and service ports.
- `DEPLOYMENT_ENVIRONMENT=production`: rejects debug mode, fake providers, process-memory stores, wildcard CORS origins, and localhost CORS origins during startup validation.
- `app/operations/health_preflight.py`: validates a running `/health` response and reports unhealthy dependency names for deployment gates.
- `app/operations/postgres_smoke.py`: non-destructive Postgres DSN reachability checks for configured session, retrieval audit, indexing queue, indexing job, document catalog, and checkpoint stores.
- `app/security/cors.py`: CORS option builder backed by environment settings, with validation that rejects wildcard origins when credentialed requests are enabled.
- `app/security/uploads.py`: upload filename/path normalization, extension and size enforcement, UTF-8 validation, and high-risk prompt-injection screening before document indexing.
- `app/api/health.py`: `/health` now returns the existing response envelope with split dependency status and HTTP 503 when a required dependency is unhealthy.
- `start.ps1`: preflight now calls the shared Python configuration validator instead of carrying separate PowerShell-only credential checks.
- `scripts/check-api-health.ps1`: command-line health gate for a running API.
- `.github/workflows/ci.yml`: hosted GitHub Actions baseline validation on Windows with Python/Node setup and uploaded evaluation report artifact.
- `vector-database.yml`: Attu is now behind the optional Docker Compose `ui` profile, while the core Milvus services remain the default stack.
- `docs/operations.md` and `docs/deployment.md`: document trace id, access log, retrieval audit store, health check, config validation, Docker profile, CORS security-boundary, deployment runbook, and CI artifact behavior.

Durable retrieval audit storage now defaults to local SQLite and can switch to Postgres, while production mode still blocks explicit process-memory stores before startup. `GET /api/metrics` exposes process-local HTTP and RAG counters for local operations and smoke dashboards; `GET /api/metrics/prometheus` exports the same signal in Prometheus text format for external scraping. `scripts/run-postgres-smoke.ps1` verifies configured Postgres DSNs without mutating data. Production secret management, central log/trace/metrics storage, and full schema/write-path database validation remain open.

## Phase 8 Progress

The repository now has an initial extension-template layer:

- `app/extensions/tools.py`: ordered tool registry for built-in and business-specific LangChain tools, with `ENABLED_TOOLS` selecting the runtime tool list and optional `TOOL_PLANNING_ENABLED` model planning for non-retrieval tools.
- `app/prompts/profiles.py`: named prompt profiles for `default`, `strict`, and `concise` RAG behaviors, selected through `PROMPT_PROFILE`.
- `app/providers/selection.py`: provider id validation for chat, embedding, vector store, session store, and ingestion boundaries.
- `app/providers/sqlite_session.py`: local durable SQLite implementation for `SessionStoreProvider`.
- `app/providers/checkpoints.py`: memory, SQLite, and Postgres LangGraph checkpoint providers selected by `CHECKPOINT_PROVIDER`.
- `app/providers/openai_compatible.py`: OpenAI-compatible chat and embedding provider implementations for endpoints that expose OpenAI-style APIs.
- `app/providers/factory.py`: default container now honors provider ids and can build local fake providers without external clients.
- `app/knowledge/catalog.py`: memory, SQLite, and Postgres document catalog implementations selected through `DOCUMENT_CATALOG_PROVIDER`.
- `docs/extension-guide.md` and `docs/templates/business-rag-template.md`: second-development guidance for adding business tools, prompt profiles, provider settings, and evaluation data without modifying core API code.

This completes the first reusable base-agent extension surface. Open product work still includes real provider evaluation, external distributed queue implementations beyond the current queue boundary, and production security hardening.
