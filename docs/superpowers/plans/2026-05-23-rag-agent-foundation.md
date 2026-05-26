# RAG Agent Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn RAGqs from a demo RAG MVP into a modular, configurable, observable, evaluable, and extensible foundation RAG agent.

**Architecture:** Evolve the app in layers: configuration, provider interfaces, ingestion, retrieval, LangGraph orchestration, session/document persistence, evaluation, observability, and extension templates. Keep FastAPI and the current UI working during each phase.

**Tech Stack:** Python 3.11+, FastAPI, Pydantic Settings, LangChain, LangGraph StateGraph, Milvus, DashScope/OpenAI-compatible APIs, pytest-compatible tests, Node-based frontend state tests, PowerShell startup validation.

---

## File Structure Direction

- `app/config.py`: split current flat settings into validated groups.
- `app/providers/`: provider protocols and concrete DashScope/Milvus implementations.
- `app/ingestion/`: loaders, chunkers, metadata normalization, indexing jobs.
- `app/retrieval/`: query rewrite, retriever composition, rerank, compression, attribution.
- `app/agents/`: explicit LangGraph StateGraph construction and event mapping.
- `app/sessions/`: server-side session and conversation storage.
- `app/evaluation/`: datasets, evaluators, CLI runner, and metrics.
- `docs/architecture/`: baseline audit, risk register, target architecture, ADRs.
- `scripts/`: validation, evaluation, and local operations commands.

## Phase 0: Baseline

- [x] Add `.env.example` covering every current `Settings` field.
- [x] Add `docs/architecture/baseline-audit.md` describing current modules, runtime dependencies, and Known Limitations.
- [x] Add `docs/architecture/risk-register.md` with Configuration, Retrieval Quality, Session Persistence, Indexing Reliability, Observability, Security, and Extensibility risks.
- [x] Add `tests/test_phase0_baseline.py` so these baseline artifacts remain present.
- [ ] Run `scripts/validate-baseline.ps1 -SkipPreflight` after each foundation change. Current validation includes Phase 0 docs, provider contracts, provider container wiring, provider-backed knowledge retrieval, ingestion, retrieval trace and audit tests, explicit graph skeleton/runtime tests, service-side session store/listing tests, frontend backend-first history state, and startup script checks.

## Phase 1: Provider Boundaries

- [x] Create provider protocols for chat models, embeddings, vector stores, retrievers, session stores, and ingestion pipelines.
- [x] Move DashScope embedding construction out of import-time globals and behind an `EmbeddingProvider`.
- [x] Move Milvus vector store construction behind a `VectorStoreProvider`.
- [x] Add fake providers for unit tests so backend tests do not require DashScope or Milvus.
- [x] Keep existing API behavior unchanged while switching service construction to providers. Current progress: `RagAgentService` accepts an injected `ChatModelProvider`, `SessionStoreProvider`, `CheckpointProvider`, custom agent factory, tools, checkpointer, and configurable `AGENT_RUNTIME`; `knowledge_tool` retrieves through `RetrieverProvider`; file upload indexing now runs through `ProviderContainer.ingestion_provider`; and `ProviderContainer` wires chat, embedding, vector store, retriever, session, ingestion, and checkpoint providers.

Current progress: `app/config.py` now exposes typed grouped settings for app, CORS, uploads, providers, storage, agent, OpenAI-compatible, DashScope, Milvus, RAG, and chunking configuration while preserving the existing flat `.env` names. `ProviderSelection` and startup config validation can consume these grouped settings. `PostgresSessionStoreProvider` adds the first multi-instance session backend behind `SESSION_STORE_PROVIDER=postgres` and `SESSION_STORE_POSTGRES_DSN`; `PostgresIndexingJobStore` adds multi-instance indexing status behind `INDEXING_JOB_STORE_PROVIDER=postgres` and `INDEXING_JOB_STORE_POSTGRES_DSN`; `PostgresKnowledgeCatalog` adds multi-instance document lifecycle metadata behind `DOCUMENT_CATALOG_PROVIDER=postgres` and `DOCUMENT_CATALOG_POSTGRES_DSN`; `PostgresCheckpointProvider` adds multi-instance graph checkpoint state behind `CHECKPOINT_PROVIDER=postgres` and `CHECKPOINT_POSTGRES_DSN`. Remaining config work is migrating scattered flat `config.*` reads in runtime modules to the grouped views where it improves ownership boundaries.

## Phase 2: Ingestion Foundation

- [x] Create document loader interfaces for uploaded text and Markdown.
- [x] Normalize metadata with stable document id, chunk id, source path, content hash, extension, and heading fields.
- [x] Add an indexing job model with pending, running, succeeded, failed, and partial states.
- [x] Make re-indexing idempotent by document id instead of raw path string.
- [x] Return indexing errors to API callers instead of only logging them.
- [x] Add process-local indexing job storage, per-file directory job summaries, indexing status APIs, and failed-job retry.
- [x] Add durable indexing job storage when a persistence backend is selected. Current progress: `SQLiteIndexingJobStore` persists indexing jobs locally when `INDEXING_JOB_STORE_PROVIDER=sqlite`, and `PostgresIndexingJobStore` persists indexing jobs for multi-instance deployments when `INDEXING_JOB_STORE_PROVIDER=postgres`.
- [x] Add optional background job execution for uploads. Current progress: `INDEXING_EXECUTION_MODE=background` creates pending jobs and enqueues them through an indexing queue boundary for an in-process FastAPI lifespan worker; `INDEXING_WORKER_RECOVER_PENDING_JOBS=true` re-enqueues persisted pending jobs on startup; `sync` remains the default compatibility mode.

Current progress: `app/ingestion/` now contains UTF-8 text/Markdown loaders, metadata normalization, an indexing job state model, in-memory, SQLite, and Postgres job stores, an indexing queue protocol with an in-memory FIFO implementation, pending-job recovery, and an in-process background worker. `VectorIndexService` accepts loader, splitter, vector store, normalizer, job store, and document catalog dependencies; defaults local development to SQLite job/catalog storage while still allowing memory/Postgres selection; deletes previous chunks by stable `document_id` and also clears legacy source-only chunks by source path; enriches chunks with normalized metadata; records successful and failed jobs; creates pending jobs for queued execution; and returns per-file job summaries for directory indexing. Upload responses include indexing status, synchronous indexing failures return HTTP 500 details, background indexing returns pending jobs, and `/index-jobs` APIs support list, detail, and retry. Remaining work includes an external/distributed queue implementation for multi-instance production ingestion.

## Phase 3: Retrieval Foundation

- [x] Introduce a retrieval request/response model containing query, rewritten query, filters, documents, scores, citations, and debug timing.
- [x] Add optional query rewrite before retrieval.
- [x] Add metadata filtering and configurable top-k.
- [x] Add rerank and context compression extension points.
- [x] Include citations and retrieval trace data in chat responses.

Current progress: `app/retrieval/` now provides a composable `RetrievalPipeline` that implements `RetrieverProvider` and wraps one or more retrievers with optional query rewrite, deduplication, rerank, context compression, citation extraction, and per-stage timing debug data. `ProviderContainer` now wires the default vector-store retriever through this pipeline, so `knowledge_tool` uses the new retrieval foundation by default. `RETRIEVAL_PROFILE=high_recall` enables a multi-retriever profile that widens top-k and adds a relaxed metadata-filter fallback while preserving space and tenant filter keys. `QUERY_REWRITER_PROVIDER=llm` enables an LLM-backed query rewriter, `RERANKER_PROVIDER=llm` enables an LLM-backed listwise reranker, `CONTEXT_COMPRESSOR_PROVIDER=llm` enables an LLM-backed context compressor, and `CONTEXT_COMPRESSOR_MAX_CHARACTERS` caps compressed chunk size. `RagAgentService.query_with_trace()` and `query_stream_with_trace()` expose sources and retrieval debug data through `/chat` and `/chat_stream`. Real-provider evaluation remains pending before production retrieval-quality claims.

## Phase 4: Explicit LangGraph Agent

- [x] Replace `create_agent` as the core orchestration path with explicit `StateGraph` nodes. Current progress: `AGENT_RUNTIME=explicit_graph` is the default, and `legacy` keeps the old `create_agent` compatibility path for targeted tests and fallback.
- [x] Add nodes for input normalization, retrieval decision, retrieval, answer generation, tool execution, error handling, and final response. Current progress: input normalization, retrieval decision, retrieval, no-context handoff/refusal, explicit tool planning/execution, answer generation, error policy, and final response nodes exist in `app/agents/rag_graph.py`; retrieval, tool, and answer failures become structured `error` events routed through `error_policy`, empty retrieval no longer calls the chat model, and every graph path emits a graph-owned `done` event.
- [x] Use checkpoint persistence through a provider boundary, starting with memory and leaving room for Redis/Postgres. Current progress: `CheckpointProvider` covers local durable SQLite checkpoints by default, process-local memory checkpoints for throwaway tests, and multi-instance Postgres checkpoints through `CHECKPOINT_PROVIDER`; `RagAgentService` receives checkpointers from the provider container, and the explicit graph builder compiles with the selected checkpointer.
- [x] Standardize stream events for token, tool_call, retrieval, source, error, and done. Current progress: explicit graph state records input, retrieval decision, retrieval, tool call/result, answer, handoff, error, error policy, and final response events, resets per-run transient events across checkpointed invocations, and the chat API maps `retrieval_decision`, `retrieval`, `handoff`, `error_policy`, `source`, `tool_call`, `tool_result`, `token`, `error`, and `done` chunks. Streaming explicit-graph calls consume LangGraph `stream_mode=["custom", "updates"]`, so answer tokens are emitted as `token` chunks before the graph-owned `done` event.

Current progress: `app/agents/` now contains a tested explicit `StateGraph` RAG skeleton with typed state, injectable retriever, answer generator, tool planner, and tool executor, source/debug propagation, retrieval decision routing, explicit tool execution for structured tool requests, optional LangChain `bind_tools` planning for non-retrieval tools, no-context handoff/refusal, token-level answer streaming through LangGraph custom stream events, event accumulation, structured error events, explicit error policy events, final response events, space-aware retrieval filters, provider-selected checkpoint compilation, and per-run event reset under checkpointing. `RagAgentService` builds a default explicit graph with `ChatModelAnswerGenerator`, `LangChainToolExecutor`, and optional `LangChainToolPlanner` when `TOOL_PLANNING_ENABLED=true`; the compatibility `create_agent` path remains selectable through `AGENT_RUNTIME=legacy`. Remaining graph work includes real database integration validation for Postgres checkpoint deployments.

## Phase 5: Product Capabilities

- [x] Move conversation history from browser-only localStorage to backend session APIs. Current progress: `RagAgentService` records successful non-streaming and streaming exchanges through `SessionStoreProvider`, `get_session_history()` reads stored messages first, `list_sessions()` exposes searchable summaries, `SQLiteSessionStoreProvider` offers local durable storage, `PostgresSessionStoreProvider` offers multi-instance chat history storage, and the browser sidebar refreshes from backend APIs with localStorage only as a fallback cache.
- [x] Add knowledge base spaces so collections/documents can be separated by business context. Current progress: indexing jobs and document metadata now carry `space_id`; upload accepts `space_id`; retrieval can pass `space_id` as a metadata filter; `/knowledge-spaces` exposes configured catalog spaces; and legacy `create_agent` tool execution enforces the request-selected knowledge space through a context-scoped boundary.
- [x] Add document list, delete, rebuild, and status APIs. Current progress: `VectorIndexService` maintains an in-memory or SQLite document catalog and exposes list/get/delete/rebuild methods; FastAPI routes under `/knowledge-spaces/{space_id}/documents` serialize document lifecycle status.
- [x] Add history search and consistent frontend session switching. Current progress: `/chat/sessions?query=...` searches session titles and message content; the frontend search box calls that API and lazily loads `/chat/session/{id}` before rendering a selected backend history.

Current progress: Phase 5 now has server-side session recording, searchable session summaries, default SQLite session persistence, optional Postgres session persistence, backend-first frontend history, SQLite-first memory/Postgres knowledge spaces/document catalog storage, default SQLite indexing job persistence, SQLite/memory/Postgres checkpoint storage, document lifecycle APIs, and request-scoped space enforcement for both direct retrieval and legacy tool retrieval. A non-destructive Postgres smoke gate now verifies configured Postgres DSN reachability; deeper schema/write-path validation against deployment credentials remains open.

## Phase 6: Evaluation

- [x] Add a golden dataset format for question, expected answer traits, expected sources, and refusal expectation.
- [x] Add metrics for retrieval hit rate, answer faithfulness, citation accuracy, latency, and no-answer refusal quality. Current progress: deterministic metrics cover retrieval hit rate, expected answer trait coverage, answer faithfulness verdict score, citation accuracy, average latency, and no-answer refusal; `ModelFaithfulnessJudge` can score traced service or HTTP runs through the configured chat provider container.
- [x] Add a local evaluation command that can run with fake providers and optional real providers. Current progress: `scripts/run-evaluation.ps1` supports `fake`, in-process `service`, and external FastAPI `http` modes against `data/evaluation/golden.jsonl`, with `none`, `static`, and `model` faithfulness judge modes, per-example `metadata.spaceId` routing, real-provider readiness checks through `-PreflightOnly`, and JSON reports through `-ReportPath`.
- [x] Document how to enable LangSmith tracing and evaluation when credentials are available.

Current progress: `app/evaluation/` now provides typed golden examples, run results, faithfulness verdicts, aggregate reports, JSONL loading, deterministic metric calculation, a `FaithfulnessJudge` boundary with static and model-backed implementations, a fake-provider runner, an async traced-service runner, provider-agnostic model judge selection, a space-aware HTTP `/chat` evaluation client, and a real-provider readiness report that validates service/http mode, dataset strength, provider selection, model-judge credentials, HTTP targets, and LangSmith tracing settings before running expensive evaluations. `scripts/run-evaluation.ps1` is included in baseline validation so the evaluation harness itself cannot silently regress, and it writes CI-friendly JSON reports. `.env.example` and `docs/evaluation.md` document LangSmith tracing variables for real service evaluation.

## Phase 7: Operations

- [x] Add structured JSON logs with trace id propagation. Current progress: request middleware propagates `X-Trace-Id`, stores it in request context, returns it in response headers, emits structured `http_request` records with status and latency, and RAG answers now carry the same trace id into retrieval audit records.
- [x] Add selected retrieval audit storage. Current progress: `RetrievalAuditStoreProvider` covers memory, SQLite, and Postgres audit stores; `RagAgentService.query_with_trace()` and `query_stream_with_trace()` persist question, answer, sources, retrieval debug data, session id, knowledge-space id, trace id, and timestamp; `/api/chat/audits` exposes filtered inspection by session, space, trace, and limit.
- [x] Split health checks into app, model provider, embedding provider, vector store, and session store. Current progress: `/health` uses a composable `HealthChecker` and reports `app`, `modelProvider`, `embeddingProvider`, `vectorStore`, and `sessionStore` dependency states.
- [x] Add Docker profiles for local development and optional persistence services. Current progress: `vector-database.yml` keeps core Milvus services in the default stack and gates Attu behind the optional `ui` profile, exposed through `start.ps1 -DockerProfile ui`.
- [x] Add a configurable CORS security boundary. Current progress: FastAPI CORS settings come from `CORS_ALLOW_ORIGINS` and `CORS_ALLOW_CREDENTIALS`; startup validation rejects wildcard origins when credentialed browser requests are enabled.
- [x] Expand startup validation to check configuration profiles and dependency health. Current progress: `start.ps1` invokes `app.operations.config_validation` during preflight, reuses Docker profile validation, validates upload security settings, runs `app.operations.health_preflight` when an API is already listening on the target port, and `scripts/run-integration-smoke.ps1` provides a non-destructive real-Milvus smoke gate for configuration, Milvus connectivity, and optional API health.
- [x] Add production deployment guardrails. Current progress: `DEPLOYMENT_ENVIRONMENT=production` rejects debug mode, wildcard or localhost CORS origins, fake providers, and process-memory stores for sessions, retrieval audits, indexing jobs, document catalog, and checkpoints.
- [x] Harden uploads against unsafe files and prompt-injection content. Current progress: `app.security.uploads` normalizes uploaded filenames into the upload directory, enforces configured extensions and byte limits, rejects invalid UTF-8 text documents, and blocks high-risk prompt-injection patterns before files are written or indexed.
- [x] Add deployment documentation and CI artifact reporting. Current progress: `docs/deployment.md` documents the deployment runbook, `scripts/check-api-health.ps1` checks a running API, `scripts/run-evaluation.ps1 -ReportPath` writes JSON reports for CI collection, and `.github/workflows/ci.yml` runs hosted baseline validation with an uploaded `evaluation-report` artifact.

Current progress: `app/observability/`, `app/operations/`, and `app/security/` provide the first operations boundaries for request tracing, structured access logs, retrieval audit persistence, dependency health, configuration validation, production deployment guardrails, running-API health preflight, non-destructive Milvus and Postgres smoke checks, configurable Milvus host ports for Windows reserved-port cases, CORS configuration, upload validation, and prompt-injection screening. `docs/operations.md` and `docs/deployment.md` document the runtime contract, including retrieval audits, Docker profiles, health gates, integration smoke gates, Postgres smoke gates, security boundaries, hosted CI, and CI evaluation artifacts. Production secret management, central log/trace collection, and real Postgres audit write-path validation remain open.

## Phase 8: Foundation Templates

- [x] Add tool/plugin registration for business-specific tools. Current progress: `app/extensions/tools.py` provides an ordered `ToolRegistry`, built-in tool registration, `ENABLED_TOOLS` selection for agent construction, and `TOOL_PLANNING_ENABLED` support for model-selected non-retrieval tools.
- [x] Add prompt profiles for different RAG application styles. Current progress: `app/prompts/profiles.py` provides `default`, `strict`, and `concise` profiles, and `RagAgentService` selects them through `PROMPT_PROFILE`.
- [x] Add provider switching examples for DashScope, OpenAI-compatible APIs, and local test doubles. Current progress: provider ids are configurable through `CHAT_PROVIDER`, `EMBEDDING_PROVIDER`, `VECTOR_STORE_PROVIDER`, `SESSION_STORE_PROVIDER`, and `INGESTION_PROVIDER`; `dashscope`, `openai_compatible`, `fake`, SQLite session, and Postgres session provider paths are covered by tests and docs.
- [x] Add a second-business-template guide showing how to fork configuration without changing core code. Current progress: `docs/extension-guide.md` and `docs/templates/business-rag-template.md` document tool registration, provider switching, prompt profiles, evaluation setup, and the no-core-code customization rule.

Current progress: Phase 8 has a tested extension layer for tool registration, optional model tool planning, prompt profile selection, provider switching, indexing execution mode, indexing queue selection, retrieval profile selection, retrieval enhancer switches, local test doubles, SQLite-by-default session/job/document/checkpoint storage, Postgres session/indexing-job/document/checkpoint storage, configurable agent runtime, and second-business documentation. Remaining foundation work after this plan includes deeper production capabilities such as real provider evaluation and external distributed queue implementations.

## Verification Gates

- [ ] `scripts/validate-baseline.ps1 -SkipPreflight`
- [x] backend unit tests with fake providers
- [x] provider container and provider-backed knowledge retrieval tests
- [x] background indexing worker tests
- [x] indexing queue provider tests
- [x] ingestion loader, metadata, job, and vector indexing service tests
- [x] Postgres indexing job store tests
- [x] Postgres document catalog tests
- [x] Postgres checkpoint provider tests
- [x] upload API indexing status and error propagation tests
- [x] upload security tests
- [x] indexing job store, directory batch summary, status API, and retry API tests
- [x] knowledge-space document lifecycle API tests
- [x] retrieval pipeline and chat citation/trace API tests
- [x] retrieval audit store and API tests
- [x] retrieval profile tests
- [x] explicit LangGraph StateGraph skeleton and service runtime tests
- [x] service-side session store tests
- [x] SQLite session provider tests
- [x] Postgres session provider tests
- [x] initial Phase 7 operations tests
- [x] Phase 8 extension registry, provider switching, prompt profile, and business template tests
- [x] frontend state tests
- [x] startup preflight with real Milvus. Verified locally after rebuilding `milvus-standalone` with `MILVUS_PORT=19630` because Windows reserved port range `19498-19597` blocked default host port `19530`; `scripts/run-integration-smoke.ps1 -Json` and `start.ps1 -PreflightOnly` both passed.
- [x] non-destructive Postgres smoke command for configured Postgres-backed stores
- [x] initial RAG evaluation suite with fake-provider command
- [x] real-provider evaluation readiness preflight
