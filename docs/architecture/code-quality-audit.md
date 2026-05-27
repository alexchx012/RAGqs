# RAGqs Code Quality Audit

Status: complete

## Scope

This audit covers non-generated code, executable scripts, workflow/configuration files, and
frontend assets. Generated or local-runtime state is excluded: `.venv/`, `.git/`,
`.pytest_cache/`, `.ruff_cache/`, `logs/`, `volumes/`, `artifacts/`, coverage output,
`*.egg-info/`, upload directories, `__pycache__/`, local `.env`, and runtime SQLite files under
`data/`.

Primary file inventory source:

```powershell
rg --files -uu -g '!**/.git/**' -g '!**/.venv/**' -g '!**/.pytest_cache/**' -g '!**/.ruff_cache/**' -g '!logs/**' -g '!volumes/**' -g '!artifacts/**' -g '!rag_knowledge_agent.egg-info/**' -g '!**/__pycache__/**' -g '!.coverage'
```

## Expected Behavior Sources

| Source | Purpose | Status | Notes |
| --- | --- | --- | --- |
| `AGENTS.md` | Repository workflow, quality, and security instructions. | Reviewed | UTF-8 text reading, full inventory coverage, and validation expectations confirmed. |
| `README.md` | Public setup, API, provider defaults, and architecture summary. | Reviewed | Baseline behavior for local startup, provider switching, API list, auth boundary, and RAG flow. |
| `docs/architecture/baseline-audit.md` | Current architecture and phase history. | Reviewed | Provider, ingestion, retrieval, graph, session, evaluation, operations, and extension foundations reviewed. |
| `docs/architecture/risk-register.md` | Known limitations and retained risks. | Reviewed | Existing real-provider, production-data, observability, and security-hardening risks considered. |
| `docs/operations.md` | Operational contract and guardrails. | Reviewed | Health, metrics, tracing, runtime controls, audit store, smoke gates, upload, and CORS behavior compared. |
| `docs/deployment.md` | Deployment runbook and staging gates. | Reviewed | Production validation and multi-instance provider guidance compared with implementation. |
| `docs/evaluation.md` | Evaluation harness and dataset contract. | Reviewed | Fake baseline and real-quality preflight requirements compared with runner implementation. |
| `docs/extension-guide.md` | Extension and provider-switching contract. | Reviewed | Provider ids, tool registry, prompt profiles, retrieval enhancers, and templates compared. |
| `.env.example` | Configuration defaults and documented environment variables. | Reviewed | Defaults align after `.markdown` upload support correction. |
| `pyproject.toml` | Packaging, dependencies, lint, format, and pytest config. | Reviewed | Python, ruff, pytest, and optional dependency groups are coherent with docs and CI. |
| `tests/**` | Executable expectations and regression coverage. | Reviewed | All test files are included in the inventory and were reviewed for false assertions and missing high-risk coverage. |

## Code And Configuration File Inventory

Review status values:

- `Reviewed - no change`: audited and no change needed.
- `Fixed`: audited and modified with targeted regression coverage.
- `Debt recorded`: audited, not changed now, and recorded in retained technical debt.

Total tracked code/config files: 133.

| File | Area | Expected responsibility | Review status | Conclusion |
| --- | --- | --- | --- | --- |
| `.env.example` | configuration | Document local/default environment variables and safe placeholders. | Fixed | Added `.markdown` to documented upload extensions; placeholder DashScope key is rejected by code. |
| `.github/workflows/ci.yml` | CI | Hosted baseline validation and evaluation artifact workflow. | Reviewed - no change | Windows baseline and fake evaluation artifact path match documented lightweight CI gates. |
| `.gitignore` | repository config | Keep secrets, generated data, caches, and local runtime state out of Git. | Reviewed - no change | Excludes env, uploads, logs, artifacts, caches, coverage, and runtime SQLite state. |
| `pyproject.toml` | packaging/config | Define package metadata, dependencies, lint, format, and pytest settings. | Reviewed - no change | Dependency groups and lint/test configuration align with repository commands. |
| `start.ps1` | scripts | Windows local startup and preflight orchestration for Milvus and API. | Reviewed - no change | Preflight, Docker, fake-provider skip, and health-gate behavior align with operations docs. |
| `vector-database.yml` | infrastructure | Local Milvus, etcd, MinIO, and optional Attu Compose services. | Reviewed - no change | Service names and ports match startup and smoke-script assumptions. |
| `scripts/check-api-health.ps1` | scripts | CLI health/readiness gate for a running API. | Reviewed - no change | Delegates to provider-aware health preflight with explicit URL/timeout. |
| `scripts/run-evaluation.ps1` | scripts | Evaluation command wrapper and report generation. | Reviewed - no change | Fake default is intentional and documented as software-path validation only. |
| `scripts/run-fake-load.ps1` | scripts | Fake-provider API concurrency and timeout smoke command. | Reviewed - no change | Scope message correctly avoids claiming real capacity or answer quality. |
| `scripts/run-integration-smoke.ps1` | scripts | Non-destructive Milvus/config/API integration smoke gate. | Reviewed - no change | Requires local Milvus availability; skipped/not run condition is documented below. |
| `scripts/run-postgres-smoke.ps1` | scripts | Non-destructive Postgres provider smoke gate. | Fixed | Grouped storage settings are honored by the smoke path. |
| `scripts/validate-baseline.ps1` | scripts | Local/CI baseline validation wrapper. | Reviewed - no change | Runs lint, tests, frontend test, script validation, evaluation, and Postgres smoke as documented. |
| `static/app.js` | frontend | Browser chat/session/upload UI behavior and API integration. | Fixed | Chat message styling hooks now match CSS and tests cover user/assistant class names. |
| `static/index.html` | frontend | Static UI shell served by FastAPI. | Reviewed - no change | DOM ids/classes are consistent with JavaScript usage after styling fix. |
| `static/styles.css` | frontend | Static UI styling and responsive layout. | Fixed | Supports current message classes and active mode dropdown state. |
| `tests/chat-history.test.js` | tests | Frontend chat-history regression tests. | Fixed | Added class-name and dropdown assertions for the frontend styling fix. |
| `tests/start-script.validation.ps1` | tests | Static validation for `start.ps1`. | Reviewed - no change | Validates key startup script behavior without launching services. |
| `tests/test_agent_provider_injection.py` | tests | Agent/provider injection regression coverage. | Reviewed - no change | Provider injection assertions remain aligned with factory and service boundaries. |
| `tests/test_api_models.py` | tests | Pydantic request/response model contracts. | Reviewed - no change | Aliases and response envelopes match route usage. |
| `tests/test_authz_foundation.py` | tests | Authentication and authorization boundary coverage. | Fixed | Added session-space filtering and denial regression coverage. |
| `tests/test_background_indexing_worker.py` | tests | Background indexing worker behavior. | Fixed | Covers grouped indexing worker settings after worker fix. |
| `tests/test_chat_retrieval_trace.py` | tests | Chat trace, sources, audit, and SSE behavior. | Fixed | Covers graph failure status propagation. |
| `tests/test_config_groups.py` | tests | Grouped settings and config validation coverage. | Fixed | Covers `.markdown` upload extension configuration. |
| `tests/test_evaluation_foundation.py` | tests | Evaluation dataset, runners, metrics, and readiness behavior. | Reviewed - no change | Fake, service, HTTP, readiness, and judge boundaries are coherent with docs. |
| `tests/test_file_upload_ingestion.py` | tests | Upload endpoint and ingestion behavior. | Reviewed - no change | Upload behavior remains covered with provider-backed ingestion. |
| `tests/test_indexing_queue.py` | tests | Memory/SQLite/Postgres indexing queue behavior. | Reviewed - no change | Lease, requeue, and provider behavior match operations docs. |
| `tests/test_ingestion_foundation.py` | tests | Loader, metadata, job, and catalog behavior. | Fixed | Covers `.markdown` loader behavior. |
| `tests/test_knowledge_spaces_lifecycle.py` | tests | Knowledge-space and document lifecycle APIs. | Reviewed - no change | Space/document lifecycle behavior matches catalog and auth boundaries. |
| `tests/test_knowledge_tool_provider.py` | tests | Provider-backed knowledge retrieval tool. | Reviewed - no change | Tool honors provider retrieval and scoped space context. |
| `tests/test_phase0_baseline.py` | tests | Baseline API, indexing, splitting, and health behavior. | Reviewed - no change | Baseline smoke remains broad and fake-provider friendly. |
| `tests/test_phase7_operations.py` | tests | Operations, metrics, health, smoke, and deployment guardrails. | Fixed | Covers fake vector-store startup and provider-aware health alignment. |
| `tests/test_phase8_foundation_templates.py` | tests | Extension templates, provider selection, and prompts. | Reviewed - no change | Template and extension contracts remain aligned with docs. |
| `tests/test_postgres_checkpoint_provider.py` | tests | Postgres LangGraph checkpoint provider selection. | Reviewed - no change | Confirms lazy Postgres checkpoint creation. |
| `tests/test_postgres_document_catalog.py` | tests | Postgres document catalog behavior. | Reviewed - no change | Fake DB coverage matches catalog SQL contract. |
| `tests/test_postgres_indexing_job_store.py` | tests | Postgres indexing job store behavior. | Reviewed - no change | Fake DB coverage matches job store SQL contract. |
| `tests/test_postgres_session_store.py` | tests | Postgres session store behavior. | Reviewed - no change | Durable session summaries and lazy connection behavior are covered. |
| `tests/test_postgres_smoke.py` | tests | Postgres smoke gate behavior. | Fixed | Covers grouped settings in the Postgres smoke path. |
| `tests/test_provider_aware_health.py` | tests | Provider-aware health checks. | Fixed | Covers grouped provider/store health status and placeholder-key rejection. |
| `tests/test_provider_contracts.py` | tests | Provider protocols and fake provider contracts. | Fixed | Covers DashScope placeholder key rejection. |
| `tests/test_provider_factory.py` | tests | Default provider container wiring and lazy construction. | Reviewed - no change | Confirms provider selection, lazy Milvus, retrieval pipeline, and storage wiring. |
| `tests/test_rag_agent_graph_runtime.py` | tests | RAG service graph runtime behavior. | Fixed | Covers graph failure status propagation. |
| `tests/test_rag_state_graph.py` | tests | LangGraph state graph orchestration behavior. | Reviewed - no change | Explicit graph tests cover routing, retrieval, tool, source, and error events. |
| `tests/test_retrieval_audit.py` | tests | Retrieval audit stores and serialization. | Reviewed - no change | Memory/SQLite/Postgres audit store contracts are covered. |
| `tests/test_retrieval_enhancers.py` | tests | Query rewrite, rerank, and compression enhancers. | Reviewed - no change | Optional LLM enhancer behavior and fallbacks are covered. |
| `tests/test_retrieval_pipeline.py` | tests | Retrieval pipeline composition. | Reviewed - no change | Rewrite, multi-retrieval, dedup, rerank, compression, and sources are covered. |
| `tests/test_retrieval_profiles.py` | tests | Retrieval profile behavior and metadata-filter relaxation. | Reviewed - no change | High-recall profile preserves space/tenant filters. |
| `tests/test_runtime_controls.py` | tests | Runtime request concurrency/timeout controls. | Reviewed - no change | Process-local concurrency and timeout behavior are covered. |
| `tests/test_session_store_service.py` | tests | Service-side session storage behavior. | Fixed | Added service-level session-space filtering coverage. |
| `tests/test_sqlite_session_store.py` | tests | SQLite session store persistence. | Reviewed - no change | Durable local session persistence is covered. |
| `tests/test_upload_security.py` | tests | Upload filename, size, encoding, and prompt-injection validation. | Reviewed - no change | Filename/path, extension, UTF-8, size, and prompt-injection checks are covered. |
| `tests/test_vector_index_service_ingestion.py` | tests | Vector index service ingestion and document lifecycle. | Fixed | Covers indexing all supported directory document types. |
| `app/__init__.py` | app | Package marker. | Reviewed - no change | No runtime logic. |
| `app/main.py` | app | FastAPI app factory, middleware, routers, static UI, and lifespan. | Fixed | Fake vector-store startup no longer attempts Milvus startup. |
| `app/config.py` | app | Settings model and grouped configuration views. | Fixed | Upload extension defaults now include `.markdown`. |
| `app/agents/__init__.py` | agents | Agent package marker. | Reviewed - no change | Exports match graph implementation. |
| `app/agents/rag_graph.py` | agents | Explicit LangGraph RAG orchestration graph. | Reviewed - no change | Error, retrieval, tool, and final-response logic are covered by graph tests. |
| `app/api/__init__.py` | API | API package marker. | Reviewed - no change | No runtime logic. |
| `app/api/chat.py` | API | Chat, streaming, sessions, knowledge spaces, documents, and audit routes. | Fixed | Preserves graph failure status and enforces session-space authorization. |
| `app/api/file.py` | API | Upload and indexing job routes. | Fixed | Upload policy now accepts `.markdown` consistently with frontend/docs. |
| `app/api/health.py` | API | Health endpoint route. | Reviewed - no change | Delegates to provider-aware health checker. |
| `app/api/metrics.py` | API | Metrics snapshot and Prometheus routes. | Reviewed - no change | Permission boundary and metrics serialization are coherent. |
| `app/core/__init__.py` | core | Core package marker. | Reviewed - no change | No runtime logic. |
| `app/core/milvus_client.py` | core | Low-level Milvus connection and collection utilities. | Reviewed - no change | Connection, schema, and collection helpers align with local Milvus assumptions. |
| `app/evaluation/__init__.py` | evaluation | Evaluation package marker. | Reviewed - no change | Public exports match evaluation package modules. |
| `app/evaluation/context.py` | evaluation | Evaluation runtime context helpers. | Reviewed - no change | Context selection stays explicit and simple. |
| `app/evaluation/dataset_quality.py` | evaluation | Dataset quality/readiness checks. | Reviewed - no change | Readiness gates align with docs for real-provider evaluation. |
| `app/evaluation/datasets.py` | evaluation | JSONL golden dataset loading. | Reviewed - no change | Loader validates dataset contract without external dependencies. |
| `app/evaluation/fake.py` | evaluation | Deterministic fake evaluation runner. | Reviewed - no change | Deterministic fake behavior is intentionally limited to software-path validation. |
| `app/evaluation/http.py` | evaluation | Live HTTP evaluation runner. | Reviewed - no change | HTTP mode uses documented endpoint contract and timeout settings. |
| `app/evaluation/judges.py` | evaluation | Static and model-backed faithfulness judges. | Reviewed - no change | Static/model judge fallbacks match evaluation docs. |
| `app/evaluation/metrics.py` | evaluation | Evaluation metric aggregation. | Reviewed - no change | Metric calculations match report schema. |
| `app/evaluation/models.py` | evaluation | Evaluation Pydantic/domain models. | Reviewed - no change | Dataset and result models match JSONL/report expectations. |
| `app/evaluation/readiness.py` | evaluation | Preflight/readiness gate for real evaluation. | Reviewed - no change | Rejects fake/weak real-evaluation setups as documented. |
| `app/evaluation/runner.py` | evaluation | Evaluation CLI entry point. | Reviewed - no change | CLI wiring matches PowerShell wrapper and docs. |
| `app/evaluation/service.py` | evaluation | In-process service evaluation runner. | Reviewed - no change | Service runner preserves space metadata and trace fields. |
| `app/extensions/__init__.py` | extensions | Extension package marker. | Reviewed - no change | Public exports are coherent. |
| `app/extensions/tools.py` | extensions | Ordered LangChain tool registry and selection. | Reviewed - no change | Tool selection is deterministic and validates names. |
| `app/ingestion/__init__.py` | ingestion | Ingestion package marker. | Reviewed - no change | Public exports match ingestion modules. |
| `app/ingestion/job_store.py` | ingestion | Memory/SQLite/Postgres indexing job stores. | Reviewed - no change | Status updates and query filters match indexing tests. |
| `app/ingestion/jobs.py` | ingestion | Indexing job status model. | Reviewed - no change | State transitions and serialization are explicit. |
| `app/ingestion/loaders.py` | ingestion | UTF-8 document loaders and registry. | Fixed | Loader registry now supports `.markdown` and directory indexing all supported types. |
| `app/ingestion/metadata.py` | ingestion | Document/chunk metadata normalization. | Reviewed - no change | Metadata normalization preserves document, source, chunk, heading, and space fields. |
| `app/ingestion/queue.py` | ingestion | Memory/SQLite/Postgres indexing queues. | Reviewed - no change | Lease/reclaim semantics match background worker requirements. |
| `app/ingestion/worker.py` | ingestion | In-process background indexing worker. | Fixed | Grouped worker settings are honored. |
| `app/knowledge/__init__.py` | knowledge | Knowledge package marker. | Reviewed - no change | Public exports are coherent. |
| `app/knowledge/catalog.py` | knowledge | Memory/SQLite/Postgres knowledge-space and document catalog. | Reviewed - no change | Space/document lifecycle and status persistence align with API tests. |
| `app/models/__init__.py` | models | Model package marker. | Reviewed - no change | No runtime logic. |
| `app/models/request.py` | models | API request models. | Reviewed - no change | Field aliases remain backward compatible. |
| `app/models/response.py` | models | API response models and envelope helpers. | Reviewed - no change | Envelope helpers match route expectations. |
| `app/observability/__init__.py` | observability | Observability package marker. | Reviewed - no change | Public exports are coherent. |
| `app/observability/metrics.py` | observability | Process-local HTTP/RAG metrics collector. | Reviewed - no change | Metrics are process-local by design and documented. |
| `app/observability/request_context.py` | observability | Request trace id middleware and context helpers. | Reviewed - no change | Trace id propagation matches audit and metrics usage. |
| `app/observability/retrieval_audit.py` | observability | Memory/SQLite/Postgres retrieval audit stores. | Reviewed - no change | Store filters, JSON serialization, and lazy Postgres connection behavior are covered. |
| `app/operations/__init__.py` | operations | Operations package marker. | Reviewed - no change | Public exports are coherent. |
| `app/operations/config_validation.py` | operations | Startup configuration validation CLI. | Fixed | Grouped provider/store settings and placeholder keys are validated consistently. |
| `app/operations/health.py` | operations | Provider-aware health check logic. | Fixed | Health status honors grouped config and fake/openai/dashscope provider branches. |
| `app/operations/health_preflight.py` | operations | Running API health preflight CLI. | Reviewed - no change | Required dependency set aligns with operations docs. |
| `app/operations/integration_smoke.py` | operations | Non-destructive integration smoke logic. | Reviewed - no change | Local Milvus requirement is explicit and non-destructive. |
| `app/operations/postgres_smoke.py` | operations | Postgres smoke logic. | Fixed | Grouped storage DSNs/provider ids are honored. |
| `app/prompts/__init__.py` | prompts | Prompt package marker. | Reviewed - no change | Public exports are coherent. |
| `app/prompts/profiles.py` | prompts | Named RAG prompt profiles. | Reviewed - no change | Prompt profile registry and validation match extension docs. |
| `app/providers/__init__.py` | providers | Provider package marker. | Reviewed - no change | Public provider exports are coherent. |
| `app/providers/checkpoints.py` | providers | Memory/SQLite/Postgres checkpoint provider factories. | Reviewed - no change | Checkpointers are lazily created and close hooks are present. |
| `app/providers/contracts.py` | providers | Runtime provider protocols and typed results. | Reviewed - no change | Protocols match service, retrieval, ingestion, and audit boundaries. |
| `app/providers/dashscope.py` | providers | DashScope-compatible embedding provider. | Fixed | Placeholder and empty API keys are rejected before client/model use. |
| `app/providers/factory.py` | providers | Default provider container and wiring. | Reviewed - no change | Provider composition is lazy for external dependencies and matches selection docs. |
| `app/providers/fakes.py` | providers | In-memory fake providers for tests and local demos. | Reviewed - no change | Fake providers are deterministic and avoid external dependencies. |
| `app/providers/ingestion.py` | providers | Ingestion provider adapter. | Reviewed - no change | Sync/background adapter maps provider results into indexing outcomes. |
| `app/providers/milvus.py` | providers | Lazy Milvus vector-store provider. | Debt recorded | Retrieval exceptions are logged and returned as empty results; retained for compatibility but recorded below. |
| `app/providers/openai_compatible.py` | providers | OpenAI-compatible chat and embedding providers. | Reviewed - no change | Provider validates required settings and stays lazy for chat model creation. |
| `app/providers/postgres_session.py` | providers | Postgres session store provider. | Reviewed - no change | Durable summaries and JSON metadata handling match fake DB tests. |
| `app/providers/retrieval.py` | providers | Structured retrieval provider adapter. | Reviewed - no change | Delegates filters/top-k and returns structured debug data. |
| `app/providers/selection.py` | providers | Provider id validation helpers. | Reviewed - no change | Supported provider ids match docs and config validation. |
| `app/providers/sqlite_session.py` | providers | SQLite session store provider. | Reviewed - no change | Durable local summaries and JSON metadata handling match tests. |
| `app/retrieval/__init__.py` | retrieval | Retrieval package marker. | Reviewed - no change | Public exports match retrieval modules. |
| `app/retrieval/pipeline.py` | retrieval | Retrieval pipeline, enhancers, dedup, sources, and debug timings. | Debt recorded | Optional LLM enhancers use synchronous model calls in request flow; retained and recorded below. |
| `app/retrieval/profiles.py` | retrieval | Retrieval profile construction. | Reviewed - no change | High-recall profile preserves isolation filters while relaxing business filters. |
| `app/security/__init__.py` | security | Security package marker. | Reviewed - no change | Public exports are coherent. |
| `app/security/auth.py` | security | Authn/authz identity, roles, spaces, and FastAPI dependencies. | Reviewed - no change | Role and space checks are consistent; session route enforcement was fixed in `app/api/chat.py`. |
| `app/security/cors.py` | security | CORS option builder and validation. | Reviewed - no change | Credentialed wildcard origin rejection aligns with operations docs. |
| `app/security/runtime_controls.py` | security | Process-local request concurrency and timeout controls. | Reviewed - no change | Middleware behavior is bounded and covered by tests. |
| `app/security/uploads.py` | security | Upload filename, path, size, UTF-8, and prompt-injection validation. | Reviewed - no change | Upload boundary rejects unsafe names, paths, oversized files, invalid UTF-8, and high-risk prompts. |
| `app/services/__init__.py` | services | Services package marker. | Reviewed - no change | No runtime logic. |
| `app/services/document_splitter_service.py` | services | Markdown/text chunk splitting. | Fixed | `.markdown` files now follow markdown splitting behavior. |
| `app/services/rag_agent_service.py` | services | RAG service facade, graph runtime, session persistence, and streaming. | Fixed | Preserves graph failure status and records/filters session space metadata. |
| `app/services/vector_embedding_service.py` | services | Lazy embedding compatibility service. | Reviewed - no change | Provider-backed embedding service remains lazy. |
| `app/services/vector_index_service.py` | services | File/directory indexing, document lifecycle, and job recording. | Fixed | Directory indexing now covers all supported document extensions. |
| `app/services/vector_store_manager.py` | services | Milvus vector store compatibility manager. | Reviewed - no change | Manager delegates to provider and honors grouped Milvus settings. |
| `app/tools/__init__.py` | tools | Tools package marker. | Reviewed - no change | Public exports are coherent. |
| `app/tools/knowledge_tool.py` | tools | Knowledge retrieval tool and request-scoped space enforcement. | Reviewed - no change | Tool honors request-scoped knowledge space filters. |
| `app/tools/time_tool.py` | tools | Current-time LangChain tool. | Reviewed - no change | Tool handles timezone failures without affecting core RAG flow. |
| `app/utils/__init__.py` | utils | Utils package marker. | Reviewed - no change | Public exports are coherent. |
| `app/utils/logger.py` | utils | Loguru logger setup. | Reviewed - no change | Logging setup creates the documented local log sink. |

## Supporting Data Contracts

These files are not counted in the 133 code/config files above, but they are part of the behavior
baseline because the evaluation harness reads them.

| File | Purpose | Review status | Conclusion |
| --- | --- | --- | --- |
| `data/evaluation/golden.jsonl` | Default deterministic fake evaluation dataset. | Reviewed - no change | JSONL schema and fake-provider expectations match evaluation runner tests. |
| `data/evaluation/business.example.jsonl` | Space-scoped business example evaluation dataset. | Reviewed - no change | Uses `metadata.spaceId` consistently with evaluation docs. |

## Fixed Issues

Severity is based on functional correctness, security boundary impact, and likelihood in the
documented local/staging workflows.

| Severity | Commit | Files | Issue | Resolution |
| --- | --- | --- | --- | --- |
| P1 | `867ff36` | `app/api/chat.py`, `app/services/rag_agent_service.py`, `tests/test_authz_foundation.py`, `tests/test_session_store_service.py` | Session list/detail/clear routes checked permissions but not session knowledge-space ownership when auth was enabled. | Session messages now record `spaceId`; service lists can filter by allowed spaces; detail and clear routes deny sessions outside the authenticated user's spaces. |
| P1 | `d122d57` | `app/api/chat.py`, `app/services/rag_agent_service.py`, `tests/test_chat_retrieval_trace.py`, `tests/test_rag_agent_graph_runtime.py` | Explicit graph failures could be serialized as successful chat responses. | Graph trace serialization and API success handling now preserve failure status and errors. |
| P1 | `68a7d72` | `app/main.py`, `tests/test_phase7_operations.py` | Fake vector-store startup still attempted Milvus startup/health paths. | Startup now skips Milvus dependency startup when `VECTOR_STORE_PROVIDER=fake`. |
| P1 | `327be0f` | `app/providers/dashscope.py`, `tests/test_provider_contracts.py` | `.env.example` placeholder DashScope keys could reach provider initialization as configured values. | DashScope chat and embedding providers reject empty/placeholder keys before use. |
| P1 | `1fd27a8` | `app/ingestion/loaders.py`, `app/services/vector_index_service.py`, `tests/test_vector_index_service_ingestion.py` | Directory indexing did not cover every supported document loader extension. | Directory ingestion now indexes all supported document extensions. |
| P1 | `f465a34` | `.env.example`, `app/api/file.py`, `app/config.py`, `app/ingestion/loaders.py`, `app/services/document_splitter_service.py`, docs, tests | Frontend/docs allowed `.markdown`, but backend upload/config/loader/splitter only consistently handled `.md`. | Upload defaults, backend policy, loader registry, splitter, docs, and tests now include `.markdown`. |
| P2 | `21c4370` | `app/operations/config_validation.py`, `app/operations/health.py`, tests | Provider-aware health/config validation missed grouped configuration values in some paths. | Health and validation now honor grouped settings consistently. |
| P2 | `2184a48` | `app/ingestion/worker.py`, `tests/test_background_indexing_worker.py` | Background indexing worker settings could ignore grouped storage/runtime configuration. | Worker reads grouped settings consistently. |
| P2 | `c86d807` | `app/operations/postgres_smoke.py`, `tests/test_postgres_smoke.py` | Postgres smoke validation could ignore grouped storage settings. | Smoke logic now honors grouped provider ids and DSNs. |
| P2 | `4342054` | `static/app.js`, `static/styles.css`, `tests/chat-history.test.js` | Frontend JavaScript emitted message class names that did not match CSS styling hooks. | Message classes and mode dropdown active styling are aligned and covered by Node tests. |

## Retained Technical Debt

| Area | Impact | Reason not changed now | Recommended follow-up |
| --- | --- | --- | --- |
| `app/providers/milvus.py` retrieval errors | `similarity_search()` logs an exception and returns `[]`, so a Milvus outage can look like "no context" instead of an unhealthy retrieval path. | Changing this now would alter public fallback behavior and may require API/SSE error-shape decisions across graph and legacy runtimes. | Introduce a typed retrieval error/status in `RetrievalResult.debug`, surface it through chat/audit/health, then tighten Milvus provider behavior behind a compatibility flag. |
| `app/retrieval/pipeline.py` optional LLM enhancers | LLM query rewrite/rerank/compression use synchronous `model.invoke()` calls; when enabled they can block async request handling under load. | Enhancers are optional and disabled by default; converting them safely requires async model-provider capability checks and broader latency tests. | Add async enhancer interfaces or run synchronous calls through a bounded thread pool; add load tests with enhancers enabled. |
| Historical sessions before `867ff36` | Existing persisted session messages may not have `spaceId`; the new authorization helper treats those sessions as `default`. Non-default scoped users may not see old sessions unless they also have `default`. | No reliable historical mapping exists in the old session rows/checkpoints. Guessing from session id or content would weaken the security fix. | If preserving old non-default session visibility matters, write a one-time admin migration that backfills `spaceId` from audit records or external session ownership data. |
| Fake evaluation and fake load gates | Passing fake evaluation/load proves software-path execution, not real answer quality, provider latency, or real concurrent-user capacity. | This is an intentional local/CI baseline boundary documented in operations/evaluation docs. | Keep real-provider evaluation, staged load tests, and domain golden datasets as release gates for business deployments. |
| Postgres smoke scope | The smoke command validates configuration and optional write connectivity, not true multi-instance queue/session semantics. | Multi-instance behavior requires deployed topology and concurrent workers, which is beyond local non-destructive smoke scope. | Add a staging-only multi-instance scenario with two workers claiming the same Postgres-backed queue. |
| Integration smoke dependency | `scripts/run-integration-smoke.ps1 -Json` requires local Milvus availability and cannot be meaningfully run without the service. | The command is intentionally non-destructive but depends on local Docker/Milvus state. | Run it whenever `milvus-standalone` is reachable; keep CI on fake/lightweight gates unless Milvus service containers are added. |

## Documentation And Implementation Alignment

- Provider switching: aligned after fake vector-store startup and grouped health/config fixes.
- Upload policy: aligned after `.markdown` support was made consistent across `.env.example`,
  upload route, loader registry, splitter, operations docs, template docs, and tests.
- Ingestion/indexing: aligned after directory indexing covered all supported loader extensions and
  background worker grouped settings were honored.
- Retrieval/agent graph: aligned after graph failure status now propagates to API responses,
  retrieval audit, and metrics.
- Session/security: aligned after session routes enforce knowledge-space authorization. Historical
  sessions without `spaceId` remain a documented migration debt.
- Evaluation: aligned; fake evaluation remains a deterministic software-path baseline and does not
  claim real answer quality.
- Observability: aligned for trace id, retrieval audit, metrics, health dependencies, and smoke
  command scope.
- Deployment scripts: aligned for local Windows preflight, fake-provider load scope, Postgres smoke
  scope, and Milvus integration smoke precondition.

## Verification Log

Targeted verification already run during module-level fixes:

| Command | Result |
| --- | --- |
| `node tests\chat-history.test.js` | Passed after frontend class/style fix. |
| `node --check static\app.js` | Passed after frontend class/style fix. |
| `.\.venv\Scripts\python.exe -m pytest tests\test_config_groups.py tests\test_ingestion_foundation.py tests\test_file_upload_ingestion.py tests\test_upload_security.py tests\test_phase0_baseline.py -q` | Passed after `.markdown` support fix. |
| `.\.venv\Scripts\python.exe -m pytest tests\test_authz_foundation.py::test_session_routes_filter_and_deny_by_session_space tests\test_session_store_service.py::test_rag_agent_service_filters_session_summaries_by_allowed_spaces -q` | Passed after session-space authorization fix. |
| `.\.venv\Scripts\python.exe -m pytest tests\test_authz_foundation.py tests\test_session_store_service.py tests\test_chat_retrieval_trace.py tests\test_rag_agent_graph_runtime.py -q` | Passed, 40 tests. |
| `.\.venv\Scripts\python.exe -m ruff check app tests` | Passed after session-space authorization fix. |

Final acceptance matrix:

| Command | Result |
| --- | --- |
| `.\.venv\Scripts\python.exe -m ruff check app tests` | Passed: `All checks passed!` |
| `.\.venv\Scripts\python.exe -m pytest` | Passed: 323 tests passed in 12.26s. |
| `node tests\chat-history.test.js` | Passed: `chat history tests passed`. |
| `powershell -NoProfile -ExecutionPolicy Bypass -File tests\start-script.validation.ps1` | Passed: `start.ps1 validation passed.` |
| `powershell -NoProfile -ExecutionPolicy Bypass -File scripts\run-evaluation.ps1` | Passed: status `completed`; fake/local metrics returned 2 examples with no failures; report explicitly says real quality is not validated. |
| `powershell -NoProfile -ExecutionPolicy Bypass -File scripts\run-postgres-smoke.ps1` | Passed: `ready=true`; `postgres=skipped no postgres-backed stores configured`. |
| `powershell -NoProfile -ExecutionPolicy Bypass -File scripts\run-integration-smoke.ps1 -Json` | Passed because local Milvus was available; configuration healthy, Milvus connected at `localhost:19630`, collections `1`, no errors or warnings. |

## Completion Status

Unreviewed code/config files remain: no. All 133 tracked code/config files are listed above with a
final review status, the two supporting evaluation data contracts were reviewed, and the final
acceptance matrix passed.
