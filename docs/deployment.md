# Deployment Runbook

This runbook captures the current Phase 7 operating contract for local and staged deployments.

## Prerequisites

- Create `.env` from `.env.example` and set a real `DASHSCOPE_API_KEY`.
- Keep `CORS_ALLOW_ORIGINS` explicit for deployed frontends, for example `https://rag.example.com`.
- Set `DEPLOYMENT_ENVIRONMENT=production` for production-like validation before release.
- Ensure Docker is running before starting the Milvus stack.

Production mode rejects debug mode, wildcard or localhost CORS origins, fake providers, and process-memory stores for runtime state. Use SQLite for a single local durable deployment or Postgres for multi-instance session, indexing, document catalog, checkpoint, and retrieval audit state.

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

The health gate fails if `app`, `modelProvider`, `embeddingProvider`, `vectorStore`, or `sessionStore` is missing or unhealthy. The smoke gate checks configuration and Milvus without creating, deleting, starting, stopping, or restarting Milvus.

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
