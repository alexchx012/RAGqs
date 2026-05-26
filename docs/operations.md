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
- `modelProvider`: chat model configuration status.
- `embeddingProvider`: embedding model configuration status.
- `vectorStore`: Milvus connectivity status.
- `sessionStore`: backend session store availability.

The route returns HTTP `503` when any required dependency is unhealthy. Use this endpoint as a readiness check; keep startup preflight and Docker health checks aligned with the same dependency names.

## Configuration Validation

Startup uses the same Python config validation module as tests:

```powershell
.\.venv\Scripts\python.exe -m app.operations.config_validation
```

The validator fails fast for unsafe startup values, including a missing or placeholder `DASHSCOPE_API_KEY`, invalid `RAG_TOP_K`, invalid `CHUNK_MAX_SIZE` / `CHUNK_OVERLAP`, invalid upload security settings, and out-of-range app or Milvus ports. `start.ps1` calls this module during preflight so the PowerShell script and application share one config validation boundary.

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
INDEXING_QUEUE_PROVIDER=memory
INDEXING_WORKER_POLL_INTERVAL_SECONDS=0.25
INDEXING_WORKER_SHUTDOWN_TIMEOUT_SECONDS=5.0
INDEXING_WORKER_RECOVER_PENDING_JOBS=true
```

`INDEXING_QUEUE_PROVIDER=memory` is the current queue implementation and defines the boundary for future external queue providers. `INDEXING_WORKER_RECOVER_PENDING_JOBS=true` re-enqueues persisted pending jobs when the worker starts, so a FastAPI restart does not strand jobs that were created before shutdown. The development default SQLite job store preserves status lookups across FastAPI restarts. For multi-instance production ingestion, use Postgres plus an external queue or dedicated worker service instead of relying only on the in-process worker.

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

The command checks the selected Postgres-backed stores for sessions, retrieval audits, indexing jobs,
document catalog metadata, and LangGraph checkpoints. It opens each configured DSN, executes a
read-only `SELECT 1`, redacts passwords in output, and does not create, delete, start, stop, or
restart databases. When no Postgres-backed store is selected, it reports a skipped check and exits
successfully so local SQLite development and CI baselines remain lightweight.

Use `-RequireConfigured` in staging or production gates that must prove at least one Postgres-backed
store is selected:

```powershell
.\scripts\run-postgres-smoke.ps1 -RequireConfigured -Json
```

## Security Boundaries

CORS is configured from environment variables instead of hard-coded wildcard settings:

```env
CORS_ALLOW_ORIGINS=http://127.0.0.1:9900,http://localhost:9900
CORS_ALLOW_CREDENTIALS=true
```

Use explicit origins for deployed frontends, for example `https://rag.example.com`. The config validator rejects `CORS_ALLOW_ORIGINS=*` when `CORS_ALLOW_CREDENTIALS=true`, because wildcard origins are unsafe with credentialed browser requests.

Uploads are validated by `app.security.uploads` before files are written or indexed:

```env
UPLOAD_ALLOWED_EXTENSIONS=txt,md
UPLOAD_MAX_BYTES=10485760
UPLOAD_PROMPT_INJECTION_SCAN_ENABLED=true
```

The upload boundary normalizes filenames into the configured upload directory, rejects unsupported extensions and oversized files, requires valid UTF-8 for text documents, and blocks high-risk prompt-injection instructions such as requests to ignore prior instructions or reveal the system prompt.
