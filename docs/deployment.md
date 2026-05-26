# Deployment Runbook

This runbook captures the current Phase 7 operating contract for local and staged deployments.

## Prerequisites

- Create `.env` from `.env.example` and set a real `DASHSCOPE_API_KEY`.
- Keep `CORS_ALLOW_ORIGINS` explicit for deployed frontends, for example `https://rag.example.com`.
- Set `DEPLOYMENT_ENVIRONMENT=production` for production-like validation before release.
- Ensure Docker is running before starting the Milvus stack.
- On Windows, if port `19530` is in an excluded TCP range, set an available `MILVUS_PORT`
  such as `19630` in `.env`; recreate existing Milvus containers after changing host port mappings.

Production mode rejects debug mode, wildcard or localhost CORS origins, disabled auth, disabled runtime request controls, fake providers, and process-memory stores for runtime state. Use SQLite for a single local durable deployment or Postgres for multi-instance session, indexing queue, indexing status, document catalog, checkpoint, and retrieval audit state.

For background indexing, `INDEXING_QUEUE_PROVIDER=sqlite` is the local durable queue boundary. Use `INDEXING_QUEUE_PROVIDER=memory` only for throwaway tests. For multi-instance ingestion, set `INDEXING_QUEUE_PROVIDER=postgres` plus `INDEXING_QUEUE_POSTGRES_DSN` and use Postgres-backed indexing job and document catalog stores. `INDEXING_WORKER_RECOVER_PENDING_JOBS=true` lets FastAPI-managed workers recover persisted pending jobs on startup.

For an 80-person internal trial, prefer multiple Uvicorn workers or multiple API
instances behind a reverse proxy only after enabling shared durable stores for
session, retrieval audit, checkpoint, indexing queue, indexing jobs, and document
catalog. Keep long RAG calls isolated with `RUNTIME_CONTROLS_ENABLED=true`,
bounded `RUNTIME_MAX_CONCURRENT_REQUESTS`, and a finite
`RUNTIME_REQUEST_TIMEOUT_SECONDS`. These settings are readiness guardrails; this
repository has not validated real 80-user business traffic or multi-instance
production data consistency.

## Start

Start the default API and core Milvus services:

```powershell
.\start.ps1
```

Use `-DockerProfile ui` only when Attu is needed:

```powershell
.\start.ps1 -DockerProfile ui
```

Milvus stays running by default after FastAPI exits. Use `-StopMilvusOnExit` only when you intentionally want the script to stop the local Milvus containers.

## Health Gate

Before starting the API, run the non-destructive integration smoke gate when Milvus should already be available:

```powershell
.\scripts\run-integration-smoke.ps1 -Json
```

After FastAPI is reachable, run the dependency health gate:

```powershell
.\scripts\check-api-health.ps1 -Url http://127.0.0.1:9900/health
```

Add the API URL to combine both checks:

```powershell
.\scripts\run-integration-smoke.ps1 -ApiUrl http://127.0.0.1:9900/health -Json
```

The health gate fails if any required provider boundary is missing or unhealthy:
`app`, `modelProvider`, `embeddingProvider`, `vectorStore`, `sessionStore`,
`checkpointStore`, `retrievalAuditStore`, `indexingQueue`, `indexingJobStore`,
or `documentCatalog`. The smoke gate checks configuration and Milvus without
creating, deleting, starting, stopping, or restarting Milvus.

Before running any local load path without real LLM credentials, start the API
with fake providers and run:

```powershell
.\scripts\run-fake-load.ps1 -ApiUrl http://127.0.0.1:9900 -Concurrency 20 -Requests 40 -Json
```

This verifies local API concurrency, timeout, and error response behavior. A
passing result is not evidence of real answer quality, 80-user capacity, or
multi-instance production readiness.

When using Postgres-backed runtime stores, run the non-destructive Postgres smoke gate before release:

```powershell
.\scripts\run-postgres-smoke.ps1 -RequireConfigured -Json
```

This verifies that configured session, retrieval audit, indexing queue, indexing job, document catalog, and checkpoint DSNs are reachable with a read-only `SELECT 1`. Output redacts DSN passwords. Omit `-RequireConfigured` for local SQLite development; the command then skips cleanly when no Postgres-backed store is selected.

For staging gates that must prove database write permissions without touching application tables, add
`-ValidateWritePath`. The command uses a temporary table and rollback:

```powershell
.\scripts\run-postgres-smoke.ps1 -RequireConfigured -ValidateWritePath -Json
```

## CI Artifacts

Run the deterministic evaluation command and keep the JSON report as a CI artifact:

```powershell
.\scripts\run-evaluation.ps1 -ReportPath artifacts\evaluation-report.json
```

The wrapper calls `app.evaluation.runner --report-path` and still enforces the configured metric thresholds before returning success.

## GitHub Actions

Hosted CI is defined in `.github/workflows/ci.yml`. The workflow runs on `windows-latest`, creates `.venv`, installs `.[dev]`, runs:

```powershell
.\scripts\validate-baseline.ps1 -SkipPreflight
```

That baseline includes `run-postgres-smoke.ps1` without `-RequireConfigured`, so hosted CI verifies the smoke command itself while still allowing SQLite defaults.

It then writes `artifacts\evaluation-report.json` with:

```powershell
.\scripts\run-evaluation.ps1 -ReportPath artifacts\evaluation-report.json
```

The report is uploaded as the `evaluation-report` artifact. The hosted workflow intentionally skips `start.ps1 -PreflightOnly` because CI does not provide a local DashScope key or Milvus stack by default.

## Shutdown

Stop FastAPI with `Ctrl+C`. Leave Milvus running for the next local run, or stop it manually:

```powershell
docker stop milvus-etcd milvus-minio milvus-standalone
```
