# Internal Trial Readiness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fill the remaining software-foundation gaps needed before an 80-person internal trial without claiming real answer quality or validated multi-instance production readiness.

**Architecture:** Extend the existing provider-first RAGqs foundation with minimal auth/authz, runtime request controls, provider-aware readiness checks, UI management flows, and explicit evaluation/deployment limitation gates. Keep existing API fields compatible and add only optional request/response fields.

**Tech Stack:** Python 3.11+, FastAPI dependencies and middleware, Pydantic Settings, pytest, PowerShell smoke scripts, browser JavaScript, Node frontend tests.

---

## File Structure Direction

- `app/security/auth.py`: auth provider boundary, dev header/static users, roles, permissions, and space checks.
- `app/security/runtime_controls.py`: request timeout and concurrency limiter middleware.
- `app/config.py`: additive auth/runtime settings groups and environment variables.
- `app/main.py`: install runtime controls and include protected routers with dependencies.
- `app/api/chat.py`, `app/api/file.py`, `app/api/metrics.py`: route-level authorization dependencies.
- `app/operations/health.py`, `app/operations/health_preflight.py`, `app/operations/config_validation.py`: provider-aware readiness and production gates.
- `scripts/run-fake-load.ps1`: fake-provider local API load command that validates concurrency paths only.
- `static/index.html`, `static/app.js`, `static/styles.css`: backend-first knowledge-space/document/job/audit management UI.
- `tests/test_authz_foundation.py`, `tests/test_runtime_controls.py`, `tests/test_provider_aware_health.py`, `tests/chat-history.test.js`: targeted regression coverage.
- `README.md`, `docs/operations.md`, `docs/deployment.md`, `docs/evaluation.md`, `.env.example`: trial-readiness usage and limitation docs.

## Task 1: Auth/Authz Foundation

- [ ] Write failing tests in `tests/test_authz_foundation.py` for:
  - disabled auth permits default dev user for local compatibility;
  - enabled dev auth reads `X-RAG-User`, maps roles/spaces from config, and denies missing users with HTTP 401;
  - users without the required permission receive HTTP 403;
  - users cannot access a `space_id`/`spaceId` they are not assigned.
- [ ] Run `pytest tests/test_authz_foundation.py -q`; expected result: failure because `app.security.auth` does not exist.
- [ ] Implement `app/security/auth.py` with `AuthContext`, `SimpleAuthProvider`, `get_current_auth_context`, `require_permission`, and `require_space_access`.
- [ ] Add auth settings to `app/config.py` and `.env.example` with defaults that preserve local compatibility.
- [ ] Apply dependencies to chat, upload, knowledge-space, document lifecycle, index-job, audit, and metrics APIs.
- [ ] Run `pytest tests/test_authz_foundation.py tests/test_chat_retrieval_trace.py tests/test_file_upload_ingestion.py tests/test_knowledge_spaces_lifecycle.py -q`.
- [ ] Commit and push with `feat: add authz foundation`.

## Task 2: Runtime Controls and Fake Load Command

- [ ] Write failing tests in `tests/test_runtime_controls.py` for concurrency rejection, request timeout response shape, and disabled controls.
- [ ] Run `pytest tests/test_runtime_controls.py -q`; expected result: failure because runtime controls are absent.
- [ ] Implement `app/security/runtime_controls.py` and install it in `app/main.py`.
- [ ] Add runtime settings to `app/config.py`, `.env.example`, and startup validation.
- [ ] Add `scripts/run-fake-load.ps1` that runs against an API configured with fake providers and prints verified/skipped/failed status without claiming real 80-concurrency success.
- [ ] Run `pytest tests/test_runtime_controls.py tests/test_phase7_operations.py -q`.
- [ ] Commit and push with `feat: add runtime request controls`.

## Task 3: Provider-Aware Health and Preflight

- [ ] Write failing tests in `tests/test_provider_aware_health.py` for DashScope, OpenAI-compatible, fake, Milvus/fake vector store, session, checkpoint, retrieval audit, indexing queue, indexing job, and document catalog dependency reporting.
- [ ] Run `pytest tests/test_provider_aware_health.py -q`; expected result: failure because health currently reports only the initial dependency set.
- [ ] Extend `app/operations/health.py` with provider-aware checks and clear skip/healthy/unhealthy messages.
- [ ] Extend `app/operations/health_preflight.py` required dependencies to include runtime data boundaries.
- [ ] Extend `app/operations/config_validation.py` production checks for auth, fake providers, memory stores, CORS, and runtime controls.
- [ ] Run `pytest tests/test_provider_aware_health.py tests/test_phase7_operations.py -q`.
- [ ] Commit and push with `feat: expand provider-aware health gates`.

## Task 4: Browser Management Closure

- [ ] Extend `tests/chat-history.test.js` with backend-first assertions for selected knowledge space in chat/upload, document list/delete/rebuild, index job retry, retrieval audits, and fallback cache behavior.
- [ ] Run `node tests\\chat-history.test.js`; expected result: failure because the UI controls are absent.
- [ ] Update `static/index.html`, `static/app.js`, and `static/styles.css` with compact management panels for spaces, documents, jobs, and audits.
- [ ] Ensure upload accepts `.txt,.md,.csv,.html,.htm,.json` and sends the selected `spaceId`.
- [ ] Run `node tests\\chat-history.test.js` and `node --check static\\app.js`.
- [ ] Commit and push with `feat: connect browser management flows`.

## Task 5: Evaluation and Multi-Instance Limitation Gates

- [ ] Add focused tests to existing evaluation/operations test files for explicit `not_run`/`not_validated` status language in reports or docs where applicable.
- [ ] Update README, operations, deployment, and evaluation docs to distinguish verified local software gates from skipped external validation gates.
- [ ] Add or update command examples for fake evaluation, real-provider preflight, Postgres read-only smoke, Postgres write-path smoke, and integration smoke.
- [ ] Run `pytest tests/test_evaluation_foundation.py tests/test_postgres_smoke.py tests/test_phase7_operations.py -q`.
- [ ] Commit and push with `docs: document internal trial gates`.

## Final Verification Gates

- [ ] `.\.venv\Scripts\python.exe -m ruff check app tests`
- [ ] `.\.venv\Scripts\python.exe -m pytest`
- [ ] `node tests\chat-history.test.js`
- [ ] `node --check static\app.js`
- [ ] `.\start.ps1 -PreflightOnly` when local credentials and Milvus are available; otherwise record skip reason.
- [ ] `.\scripts\run-evaluation.ps1`
- [ ] `.\scripts\run-postgres-smoke.ps1`
- [ ] `.\scripts\run-integration-smoke.ps1` when Milvus/external prerequisites are available; otherwise record skip reason.
