# Operations

This document captures the current runtime guardrails for local development and staged deployment.

## Request Tracing

Every HTTP request is assigned a trace id by `app.observability.request_context`.

- If the client sends `X-Trace-Id`, the server reuses it.
- If no trace id is provided, the server generates a UUID.
- The response always includes `X-Trace-Id` for successful FastAPI responses.
- Code running inside the request can call `get_current_trace_id()` to attach the same id to downstream logs or events.

RAG agent execution passes the same request context into LangChain/LangGraph runtime config. The
config includes metadata for `traceId`, `sessionId`, `spaceId`, `agentRuntime`, and `promptProfile`,
with tags such as `ragqs`, `runtime:<runtime>`, `space:<space_id>`, and `prompt:<profile>`. When
LangSmith tracing is enabled, these fields let you correlate a trace with access logs and retrieval
audit records.

## Structured Access Logs

The request middleware emits one structured `http_request` record per request. Current fields:

- `event`: always `http_request`.
- `traceId`: request trace id.
- `method`: HTTP method.
- `path`: request path.
- `statusCode`: response status code, or `500` when an unhandled exception escapes.
- `latencyMs`: request duration in milliseconds.

These records are emitted as JSON payloads through Loguru. Keep business logs concise and include the current trace id when debugging a request path.

## Runtime Metrics

JSON API routes use the public response envelope with `code`, `message`, and `data` fields where
their compatibility contract allows it. The shared `ApiEnvelope` helpers live in
`app.models.response`; keep new JSON endpoints on that helper path unless a route has an older
response model that must remain unchanged.

`GET /api/metrics` returns an in-process operational snapshot for the current
FastAPI worker. This is a lightweight local signal, not a replacement for a
central metrics backend in multi-instance deployments.

The response includes:

- `http.totalRequests`, `statusCodes`, per-route counts, `averageLatencyMs`, and `latencyBucketsMs`.
- `rag.totalQueries`, `successes`, `failures`, per-space counts, RAG `averageLatencyMs`, and `latencyBucketsMs`.
- `rag.tokenUsage` with `promptTokens`, `completionTokens`, and `totalTokens` when providers expose usage metadata.

Use this endpoint for local smoke checks and dashboards. For production,
scrape or export the same fields into the chosen central observability system.

`GET /api/metrics/prometheus` exposes the same snapshot as Prometheus-compatible
plain text for pull-based collectors. Metric names use the `ragqs_` prefix, for
example `ragqs_http_requests_total`, `ragqs_http_status_codes_total`,
`ragqs_rag_queries_total`, `ragqs_rag_space_queries_total`, and
`ragqs_rag_token_usage_total`.

## Runtime Request Controls

`app.security.runtime_controls` can add a process-local concurrency and timeout
guard around HTTP requests. It is disabled by default so local development keeps
the previous behavior:

```env
RUNTIME_CONTROLS_ENABLED=false
RUNTIME_MAX_CONCURRENT_REQUESTS=40
RUNTIME_QUEUE_TIMEOUT_SECONDS=2.0
RUNTIME_REQUEST_TIMEOUT_SECONDS=60.0
RUNTIME_CONTROL_EXCLUDED_PATHS=/health,/static
```

When enabled, requests that cannot acquire a worker-local concurrency slot before
`RUNTIME_QUEUE_TIMEOUT_SECONDS` return HTTP `429` with the standard envelope.
Requests that exceed `RUNTIME_REQUEST_TIMEOUT_SECONDS` return HTTP `504`. These
controls are a software guardrail for the current FastAPI process, not a capacity
claim for multiple workers or multiple instances.

Use the fake-provider load command after starting an API configured with
`CHAT_PROVIDER=fake`, `EMBEDDING_PROVIDER=fake`, and `VECTOR_STORE_PROVIDER=fake`:

```powershell
.\scripts\run-fake-load.ps1 -ApiUrl http://127.0.0.1:9900 -Concurrency 20 -Requests 40 -Json
```

The command reports `verified`, `skipped`, or `failed`. It validates API
concurrency, timeout, and error response paths only; it does not prove real
80-user capacity, real answer quality, or production multi-instance behavior.

## Retrieval Audit

Successful traced RAG answers are written to the configured retrieval audit store. The development default is local SQLite:

```env
RETRIEVAL_AUDIT_STORE_PROVIDER=sqlite
RETRIEVAL_AUDIT_SQLITE_PATH=data/retrieval-audits.sqlite3
```

Use process memory only for throwaway tests:

```env
RETRIEVAL_AUDIT_STORE_PROVIDER=memory
```

Use PostgreSQL when multiple FastAPI instances should write to the same audit store:

```env
RETRIEVAL_AUDIT_STORE_PROVIDER=postgres
RETRIEVAL_AUDIT_POSTGRES_DSN=postgresql://rag:secret@db/ragqs
```

`GET /api/chat/audits` returns recent audit records and accepts `session_id`, `space_id`, `trace_id`, and `limit` query parameters. Each record includes the request trace id, session id, knowledge-space id, question, answer, serialized sources, and retrieval debug payload.

## Health Checks

`GET /health` returns the existing response envelope:

```json
{"code":200,"data":{"status":"healthy","dependencies":{}}}
```

The `dependencies` object is split by runtime boundary:

- `app`: application metadata and liveness.
- `modelProvider`: selected chat provider configuration status (`deepseek`, `dashscope`,
  `openai_compatible`, or `fake`). All chat providers use shared `CHAT_MODEL`; the selected
  provider also requires its own key (for example `DEEPSEEK_API_KEY` or `DASHSCOPE_API_KEY`).
- `embeddingProvider`: DashScope, OpenAI-compatible, or fake embedding provider configuration
  status. `DASHSCOPE_EMBEDDING_MODEL` is embedding-only and is independent of `CHAT_MODEL`.
- `vectorStore`: Milvus connectivity status or fake vector-store boundary status.
- `sessionStore`: backend session store configuration.
- `checkpointStore`: LangGraph checkpoint provider configuration.
- `retrievalAuditStore`: retrieval audit provider configuration.
- `indexingQueue`: background indexing queue provider configuration.
- `indexingJobStore`: indexing status provider configuration.
- `documentCatalog`: knowledge-space/document catalog provider configuration.

The route returns HTTP `503` when any required dependency is unhealthy. Use this endpoint as a
configuration readiness check; keep startup preflight and Docker health checks aligned with the
same dependency names. A healthy `modelProvider` means the selected chat provider is configured,
not that a real DeepSeek request has succeeded or that answer quality is validated.

## Configuration Validation

Startup uses the same Python config validation module as tests:

```powershell
.\.venv\Scripts\python.exe -m app.operations.config_validation
```

The validator fails fast for unsafe startup values, including a missing or placeholder selected
chat key (default candidate: `DEEPSEEK_API_KEY`), missing or blank `CHAT_MODEL`, missing or
placeholder DashScope embedding key when embedding uses DashScope, invalid `RAG_TOP_K`, invalid
`CHUNK_MAX_SIZE` / `CHUNK_OVERLAP`, invalid upload security settings, and out-of-range app or
Milvus ports. `start.ps1` calls this module during preflight so the PowerShell script and
application share one config validation boundary.

Configuration validation and `/health` only prove the hybrid provider settings are complete. Real
DeepSeek chat traffic is a separate opt-in smoke path; do not treat a green health gate as answer
quality approval.

## Deployment Environment

`DEPLOYMENT_ENVIRONMENT` selects the validation profile:

```env
DEPLOYMENT_ENVIRONMENT=local
```

Supported values are `local`, `staging`, and `production`. Production mode rejects unsafe runtime settings: `DEBUG=true`, wildcard or localhost CORS origins, `fake` providers, and explicit `memory` stores for sessions, retrieval audits, indexing jobs, document catalog, or checkpoints. Use production mode before staging a deployment so unsafe local settings fail during preflight instead of at runtime.

## Background Indexing

Uploads use synchronous indexing by default:

```env
INDEXING_EXECUTION_MODE=sync
```

Set `INDEXING_EXECUTION_MODE=background` when uploads should return a pending indexing job immediately and let the FastAPI process execute it through an in-process worker. The worker starts and stops in the FastAPI lifespan, uses persisted indexing job ids, and drains queued jobs during graceful shutdown.

```env
INDEXING_QUEUE_PROVIDER=sqlite
INDEXING_QUEUE_SQLITE_PATH=data/indexing-queue.sqlite3
INDEXING_QUEUE_POSTGRES_DSN=
INDEXING_QUEUE_LEASE_TIMEOUT_SECONDS=300.0
INDEXING_WORKER_POLL_INTERVAL_SECONDS=0.25
INDEXING_WORKER_SHUTDOWN_TIMEOUT_SECONDS=5.0
INDEXING_WORKER_RECOVER_PENDING_JOBS=true
```

`INDEXING_QUEUE_PROVIDER=sqlite` is the development default and persists queued background indexing job ids under `data/`. Use `INDEXING_QUEUE_PROVIDER=memory` only for throwaway tests. Set `INDEXING_QUEUE_PROVIDER=postgres` plus `INDEXING_QUEUE_POSTGRES_DSN` when multiple API or worker processes must claim background indexing jobs from the same queue. SQLite and Postgres queues reclaim expired running jobs; tune `INDEXING_QUEUE_LEASE_TIMEOUT_SECONDS` to exceed expected indexing duration. `INDEXING_WORKER_RECOVER_PENDING_JOBS=true` re-enqueues persisted pending jobs when the worker starts, so a FastAPI restart does not strand jobs that were created before shutdown. The development default SQLite queue, job store, and document catalog preserve background indexing state across FastAPI restarts; for multi-instance ingestion, pair the Postgres queue with Postgres indexing job and document catalog stores.

## Docker Profiles

`vector-database.yml` keeps the core Milvus stack available by default: etcd, MinIO, and Milvus standalone. The Attu browser UI is optional and belongs to the `ui` Docker Compose profile.

Start the core Milvus services plus FastAPI:

```powershell
.\start.ps1
```

Start the same core Milvus services with Attu enabled:

```powershell
.\start.ps1 -DockerProfile ui
```

The default `core` profile does not start Attu, so port `8000` remains free for other local services. When using Docker Compose directly, run `docker compose -f vector-database.yml --profile ui up -d` to include Attu.

## Dependency Health Preflight

Use `scripts/check-api-health.ps1` after FastAPI is up, or when `start.ps1` detects an existing service on the configured port:

```powershell
.\scripts\check-api-health.ps1 -Url http://127.0.0.1:9900/health
```

The script calls `app.operations.health_preflight`, parses the `/health` response envelope, and fails when any required dependency is missing or unhealthy. Required dependency names currently match the health endpoint: `app`, `modelProvider`, `embeddingProvider`, `vectorStore`, and `sessionStore`.
The provider-aware preflight also requires `checkpointStore`, `retrievalAuditStore`,
`indexingQueue`, `indexingJobStore`, and `documentCatalog`.

## Integration Smoke Checks

Use `scripts/run-integration-smoke.ps1` when Milvus should already be reachable and you want a
non-destructive real-dependency gate:

```powershell
.\scripts\run-integration-smoke.ps1 -Json
```

Add `-ApiUrl http://127.0.0.1:9900/health` to include a running FastAPI health check. The command
validates startup configuration, requires `VECTOR_STORE_PROVIDER=milvus`, opens a read-only Milvus
client, lists collections, and exits non-zero on failures. It does not create, delete, start, stop,
or restart Milvus.

On Windows, Docker cannot bind a port that appears in `netsh interface ipv4 show excludedportrange
protocol=tcp`. If `19530` is reserved, set an alternate host port in `.env` and recreate the Milvus
standalone container so Docker can apply the mapping:

```env
MILVUS_HOST=localhost
MILVUS_PORT=19630
MILVUS_HEALTH_PORT=19091
```

`vector-database.yml` maps `${MILVUS_PORT}` to container port `19530`, so the application and Docker
Compose must use the same `.env` value.

## Postgres Smoke Checks

Use `scripts/run-postgres-smoke.ps1` when one or more runtime stores are configured with
`postgres` and you want to verify database reachability without touching data:

```powershell
.\scripts\run-postgres-smoke.ps1 -Json
```

The command checks the selected Postgres-backed stores for sessions, retrieval audits, indexing queue,
indexing jobs, document catalog metadata, and LangGraph checkpoints. It opens each configured DSN, executes a
read-only `SELECT 1`, redacts passwords in output, and does not create, delete, start, stop, or
restart databases. When no Postgres-backed store is selected, it reports a skipped check and exits
successfully so local SQLite development and CI baselines remain lightweight.

Use `-RequireConfigured` in staging or production gates that must prove at least one Postgres-backed
store is selected:

```powershell
.\scripts\run-postgres-smoke.ps1 -RequireConfigured -Json
```

Use `-ValidateWritePath` in staging gates when the database user must prove write permissions before
serving traffic:

```powershell
.\scripts\run-postgres-smoke.ps1 -RequireConfigured -ValidateWritePath -Json
```

This opt-in check creates a session-local temporary table, performs create, insert, select, and rollback operations, and reports `writePathValidated=true` without touching application tables.

The Postgres smoke gate is a provider/configuration and database-access check. It has not validated
real multi-instance production data behavior. Use it before trial deployment to catch missing DSNs,
network failures, and insufficient write permissions, then run a separate multi-instance staging
exercise before calling the data layer production-validated.

## DeepSeek Chat Provider Smoke (opt-in)

Real DeepSeek Chat Completions traffic is **not** part of the default test or preflight path.
`/health`, config validation, and CI only prove the selected chat provider is configured. They do
**not** mean a live DeepSeek request succeeded or that answer quality is validated.

Use the opt-in integration suite when you intentionally want live provider evidence:

```powershell
# Default / CI: must SKIP (not PASS) without the opt-in gate
.\.venv\Scripts\python.exe -m pytest tests/integration/test_deepseek_smoke.py -v

# Controlled live run (requires a non-placeholder DEEPSEEK_API_KEY in the environment)
$env:DEEPSEEK_SMOKE = '1'
# optional overrides:
# $env:CHAT_MODEL = 'deepseek-v4-pro'
# $env:DEEPSEEK_BASE_URL = 'https://api.deepseek.com'
.\.venv\Scripts\python.exe -m pytest tests/integration/test_deepseek_smoke.py -v
```

Gate semantics:

| Condition | Result |
|-----------|--------|
| `DEEPSEEK_SMOKE` unset / not `1` | `SKIPPED` |
| `DEEPSEEK_API_KEY` missing or placeholder | `SKIPPED` |
| Both set and valid | live smoke runs (PASS / FAIL) |

The suite covers three paths against Chat Completions only (never Responses API):

1. Non-streaming public text
2. Ordinary streaming public text
3. thinking + tool-call continuation with the zero-side-effect `get_current_time` tool — first
   turn forces `tool_choice` for the tool; second turn asks only for a final public answer from
   the tool result (no re-forced tool call). The smoke enables DeepSeek thinking via
   `extra_body={"thinking": {"type": "enabled"}}` when the adapter forwards it. The second
   adapter request must keep assistant `tool_calls` and the matching `ToolMessage`, then return
   non-empty public text. `reasoning_content` is asserted only when the live first turn actually
   returns it; full reasoning history serialization remains covered by unit tests

Constraints:

- Skipped runs must be reported as **未执行 / SKIPPED**, never as PASS.
- Live failures stay FAIL; do not reclassify rate limits, auth, balance, model-id, or network errors
  as skips.
- Never print `DEEPSEEK_API_KEY` or other secret-bearing material in test output, logs, or reports.
- Do not use business write tools in this smoke path.

## Security Boundaries

CORS is configured from environment variables instead of hard-coded wildcard settings:

```env
CORS_ALLOW_ORIGINS=http://127.0.0.1:9900,http://localhost:9900
CORS_ALLOW_CREDENTIALS=true
```

Use explicit origins for deployed frontends, for example `https://rag.example.com`. The config validator rejects `CORS_ALLOW_ORIGINS=*` when `CORS_ALLOW_CREDENTIALS=true`, because wildcard origins are unsafe with credentialed browser requests.

Uploads are validated by `app.security.uploads` before files are written or indexed:

```env
UPLOAD_ALLOWED_EXTENSIONS=txt,md,markdown,csv,html,htm,json
UPLOAD_MAX_BYTES=10485760
UPLOAD_PROMPT_INJECTION_SCAN_ENABLED=true
```

The upload boundary normalizes filenames into the configured upload directory, rejects unsupported extensions and oversized files, requires valid UTF-8 for text-like documents, and blocks high-risk prompt-injection instructions such as requests to ignore prior instructions or reveal the system prompt. The default loader registry supports TXT, Markdown, CSV, HTML/HTM, and JSON files; add a custom `DocumentLoader` before enabling additional extensions.
