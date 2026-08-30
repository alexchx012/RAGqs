# RAGqs Deployment

> Normative deployment contract for the V1 production system. This document is derived from `docs/项目设计/后端设计.md`, `docs/项目设计/前端接口需求.md`, and the frontend operations design. It deliberately does not describe legacy code paths.
>
> The reference platform is Linux containers on Kubernetes. A customer may map the same contracts to another orchestrator only when the mapping preserves the state, isolation, lease, fencing, and recovery rules below.

## 1. Scope and non-negotiable boundaries

RAGqs V1 is a strict single-tenant deployment. One deployment serves one company. Each deployment has its own PostgreSQL database, private object-storage namespace, index namespaces, deployment profile, and declarative administrator manifest. There is no runtime tenant router and no `tenant_id` field to use as a substitute for deployment isolation.

The system has three independent business pipelines:

1. Ingestion is an asynchronous persisted job pipeline.
2. Query is a synchronous HTTP/SSE pipeline whose long-running generation work is handed to a persisted worker execution.
3. Evaluation is an asynchronous persisted batch pipeline.

The pipelines share domain and storage contracts but must not call one another through in-process queues or hidden background tasks. PostgreSQL is the authoritative source for relational business facts. Object storage is the authoritative source for uploaded content and durable processing artifacts. Vector, sparse, hierarchy, graph, and cache data are derived and rebuildable.

The following rules are deployment invariants:

- Every long-running job or execution is claimed with a persisted lease and fencing token. An in-memory queue is never the recovery source.
- Query compute and ingestion compute are separate scaling and placement units. They do not share a GPU allocation.
- Generation execution is separate from the HTTP request and SSE connection. A disconnected browser does not immediately cancel the generation.
- Evaluation is started by an external cron or workflow calling the protected API. The API handler never runs an evaluation batch synchronously and no in-process scheduler is used.
- Provider, model, GPU, residency, concurrency, VRAM, and cost choices are versioned deployment-profile data. There are no global production defaults for those choices.
- Configuration changes that affect a provider, model, analyzer, index schema, or business calendar require a new profile/release or a new index generation. They are not hot switches.

## 2. Production Topology A

### 2.1 Logical workloads

The following six logical workloads are the production reference. The independent process, persisted-lease, and resource-isolation boundaries are mandatory where stated by the design. Separate Kubernetes Deployments/Jobs, service accounts, and failure domains are the reference platform mapping; an equivalent platform may combine its packaging only when it preserves every mandatory boundary.

| Workload | Responsibilities | Authoritative state | Placement and scaling rules |
| --- | --- | --- | --- |
| `api-query` | Authentication, ACL checks, HTTP endpoints, SSE subscription, generation identity transactions, and read-model delivery | PostgreSQL sessions, messages, generation identities, events, and read models | Stateless replicas behind the ingress. It must not own ingestion, generation retrieval/provider work, evaluation, graph, or outbox execution. |
| `ingestion-worker` | Parsing, OCR, local image processing, embedding, contextual retrieval, staged publication, and document-version completion | PostgreSQL jobs/attempts/publications plus private object and index staging namespaces | Independently scaled CPU/GPU pools. It never uses the query pool's GPU allocation. |
| `generation-worker` | Retrieval, reranking, provider calls, candidate/final response commits, checkpoint recovery, and generation terminal transitions | PostgreSQL `generation_execution`, leases, checkpoints, events, usage, and business results | Multi-instance persisted worker pool. Each generation has at most one valid running execution at a time. |
| `evaluation-worker` | Shadow evaluation batches, judge calls, report persistence, and policy snapshots | PostgreSQL evaluation runs/attempts and immutable report references | Separate concurrency and rate lane for the judge. Runs are created by an external scheduler. |
| `graph-worker` | Explicit public-library graph build runs and staged graph generation publication | PostgreSQL graph runs, source revisions, leases, and generation manifests | Separate worker pool. No automatic trigger, automatic replay, or private-space graph build. |
| `maintenance-worker` | Lease reconciliation, outbox dispatch, retention/deletion progress, derived-index GC, and consistency checks | PostgreSQL maintenance state and idempotent component progress | Can be split into several Deployments/CronJobs for capacity, but must not be embedded in the API process. |

The logical grouping is an operational boundary, not a requirement that every row above be a separate repository or image. A shared image is acceptable only when the startup command, service account, network policy, resource profile, and allowed capabilities are still distinct.

### 2.2 Kubernetes baseline

Each customer/environment deployment uses a dedicated namespace and dedicated service accounts. The namespace name, database name, object prefix/bucket, Milvus collection prefix, sparse index name, and secret references are derived from the immutable deployment ID; they are never inferred from a request.

The platform must provide:

- separate node pools or device allocations for query, ingestion, generation, and any local reranker/VLM workloads;
- resource requests and hard limits for CPU, memory, ephemeral storage, GPU count, VRAM, concurrency, and provider rate lanes;
- pod anti-affinity or an equivalent spread rule for replicas that hold independent leases;
- graceful termination long enough for a worker to stop renewing leases and leave its current attempt in a recoverable state;
- network policies that permit only the documented paths between workloads and dependencies;
- immutable image digests and a release identifier visible in every workload and health signal;
- a Kubernetes CronJob, workflow engine, or equivalent external scheduler for backup, shadow evaluation, reconciliation, and maintenance triggers.

The scheduler creates persisted work by calling a protected API or an operations CLI. It does not execute business work inside the scheduler and it does not bypass idempotency keys. A missed scheduler tick is recoverable from the persisted state; a duplicate tick is harmless.

### 2.3 Ingress, browser, and SSE requirements

The reference deployment serves the SPA and API from the same site origin behind a TLS-terminating ingress or reverse proxy. Cross-origin access is an explicit exception and must be an allowlist in the deployment profile.

The ingress must:

- pass `Authorization`, `Origin`, `X-CSRF-Token`, `Idempotency-Key`, and `Last-Event-ID` without rewriting;
- disable response buffering and compression behavior that delays SSE events;
- allow an SSE read/idle timeout of at least 180 seconds, above the 30-second heartbeat and 90-second subscription lease;
- drain connections during rollout instead of terminating them as soon as a pod is removed;
- reject plaintext production traffic and set the deployment's HSTS policy;
- keep refresh cookies host-only, `Secure`, `HttpOnly`, and `SameSite=Lax`.

The server sends an SSE comment heartbeat every 30 seconds and renews the subscription lease, whose default is 90 seconds. The default disconnect grace period is 60 seconds. These values are part of the profile and frontend contract; changing them requires coordinated client and ingress validation.

### 2.4 Deployment isolation and residency

`deployment_id`, environment, cloud/region, data-residency class, allowed egress destinations, and provider locations are recorded in the release manifest. A provider endpoint outside the declared residency boundary is a profile validation failure, not an operator choice made during an incident.

The deployment must not share a PostgreSQL database, object prefix, index collection, secret, or administrator manifest with another customer deployment. The reference policy uses dedicated resources. If a platform profile deliberately shares an underlying cluster, it must prove hard namespace and credential isolation and record the control owner, evidence reference, verification time, and expiry in the deployment manifest.

## 3. Dependencies and data ownership

| Component | Role in the system | Production rule |
| --- | --- | --- |
| PostgreSQL | Business and relationship fact source: users, ACLs, documents, versions, jobs, publications, sessions, usage, quotas, evaluations, outbox, notifications, and audit | One authoritative database per deployment. Use a managed or HA PostgreSQL service with PITR/WAL support. SQLite is development-only and is not a production fallback. |
| Object storage | Originals, archives, `middle.json`, `model.json`, parser intermediates, and deletion archives | Private S3-compatible namespace with encryption, versioning or equivalent snapshot support, checksum validation, and no static/direct-read exposure. |
| Milvus | Derived vector index | Collection names include the registered embedding dimension/model generation. Never auto-drop a collection during startup or provider changes. |
| Meilisearch | Default derived sparse index | Persistent volume, authentication, health probe, and the configured jieba/pre-tokenized field are required. Switches to OpenSearch require a new index generation and full rebuild. |
| OpenSearch + IK | Optional sparse provider | TLS, authentication, JVM baseline, IK plugin, and real analyzer probe are required when selected. It is not a runtime fallback for a failed Meilisearch instance. |
| Hierarchy, graph, and cache stores | Derived data | They never contain the only copy of business facts. They are rebuilt in the documented recovery order and may be disabled while stale. |
| Transactional outbox | Reliable notification submission | V1 uses PostgreSQL transactional outbox. Kafka, RabbitMQ, and an in-memory event bus are not required dependencies. |
| Secret manager | Credential and signing-key delivery | Use workload identity or an equivalent external secret reference. Secret values never appear in images, profiles, logs, audits, or API responses. |
| Observability backend | Traces, structured logs, metrics, and alerts | Use OpenTelemetry-compatible traces/logs and Prometheus-compatible metrics. The vendor is a platform-profile choice; correlation fields and alert semantics are fixed here. |

### 3.1 Facts, derived data, and visibility

PostgreSQL and object storage jointly form the complete recovery source. An object without its PostgreSQL association, size, checksum, and lifecycle state is not a recoverable business object. An index entry without a matching active publication is never visible, even if the external index reports it as healthy.

Every derived entry carries the relevant `generation_id`, `document_id`, `document_version_id`, and `publication_id`. Query, preview, and download paths verify those identifiers against PostgreSQL before returning content.

### 3.2 Index generation publication

There is one instance-wide `active_generation_id` covering vector, sparse, hierarchy, and applicable graph components. A generation change follows this sequence:

1. Create a staging generation and capture its `base_revision` and configuration manifest.
2. Build the snapshot without changing the active generation.
3. Apply every `index_change` after the base revision in strict revision order.
4. Run the complete consistency and frozen retrieval acceptance gates.
5. Atomically replace `active_generation_id` in PostgreSQL.
6. Retain the previous generation as the sole rollback candidate.

A partial or failed generation is never visible. The default rollback window is seven days and is fixed when a generation becomes retired. Only `ops` may initiate a rollback; `admin` is read-only. Component-level GC is idempotent and cannot remove an active generation, a rollback candidate inside its window, or a generation with active query references.

### 3.3 Backup identity and recovery order

Every backup set has a stable `backup_id` that links:

- the PostgreSQL base/PITR position;
- the object-storage snapshot or version marker; and
- an object manifest containing object key, size, and checksum.

The customer backup profile must declare and audit PostgreSQL PITR/WAL use, base-backup cadence, object snapshot/versioning method, encryption/off-site policy, retention period, RPO, RTO, and restore-drill cadence. The reference profile proposes a daily base backup, 35-day retention, RPO no worse than 15 minutes, RTO no worse than four hours, and a quarterly restore drill; those values are deployment decisions rather than V1 business-contract constants.

Recovery is always performed in this order:

1. PostgreSQL.
2. Object storage and its manifest.
3. Milvus.
4. Sparse index.
5. Hierarchy index.
6. Graph index.
7. Cache.

During recovery, retrieval, preview, download, and derived-index writes are disabled. After PostgreSQL and object storage are restored, validate document/version records, object keys, sizes, and checksums before rebuilding any derived component. Resources with inconsistent facts remain invisible and enter a repair queue; operators do not infer business truth from a derived index.

## 4. Versioned customer deployment profile

The deployment profile is the only authority for customer-specific hardware, provider, residency, concurrency, cost, endpoint, and retention choices. It is reviewed and versioned with the release. A profile may contain references to secrets, but never secret values. `<REQUIRED>` placeholders are permitted in templates and are a production startup failure.

The following is the normative shape. It is embedded here so a separate profile file is optional; a platform may materialize the same structure as a Kubernetes ConfigMap plus Secret references.

```yaml
profile_version: 1
deployment:
  id: "<company>-<environment>"
  environment: "prod"
  data_residency: "<REQUIRED>"
  allowed_egress: ["<REQUIRED>"]
  business_timezone: "<REQUIRED IANA TIMEZONE>"
  region: "<REQUIRED>"

platform:
  orchestrator: "kubernetes"
  namespace: "<deployment-id>"
  image_digest: "<REQUIRED>"
  release_id: "<REQUIRED>"
  isolation_evidence_ref: "<REQUIRED OR dedicated-resource declaration>"

workloads:
  api_query: { replicas: "<REQUIRED>", cpu: "<REQUIRED>", memory: "<REQUIRED>" }
  ingestion: { replicas: "<REQUIRED>", cpu: "<REQUIRED>", memory: "<REQUIRED>", gpu: "<PROFILE>" }
  generation: { replicas: "<REQUIRED>", cpu: "<REQUIRED>", memory: "<REQUIRED>", gpu: "<PROFILE>" }
  evaluation: { replicas: "<REQUIRED>", cpu: "<REQUIRED>", memory: "<REQUIRED>" }
  graph: { replicas: "<REQUIRED>", cpu: "<REQUIRED>", memory: "<REQUIRED>", gpu: "<PROFILE>" }
  maintenance: { replicas: "<REQUIRED>", cpu: "<REQUIRED>", memory: "<REQUIRED>" }

storage:
  postgres: { mode: "managed-ha", database: "<REQUIRED>", credential_ref: "<REQUIRED>" }
  object_store: { type: "s3-compatible", bucket_or_namespace: "<REQUIRED>", credential_ref: "<REQUIRED>" }
  vector: { engine: "milvus", collection_prefix: "<REQUIRED>" }
  sparse: { provider: "meilisearch", index_prefix: "<REQUIRED>" }
  backup_namespace: "<REQUIRED>"

providers:
  generation: { provider: "<REQUIRED>", model: "<REQUIRED>", revision: "<REQUIRED>", credential_ref: "<REQUIRED>" }
  embedding: { model: "<REQUIRED>", revision: "<REQUIRED>", dimension: "<REQUIRED>", metric: "<REQUIRED>" }
  image_vlm: { provider: "bailian", model: "qwen-vl-plus", credential_ref: "IMAGE_VLM_CREDENTIAL_REF" }
  reranker:
    provider: "vllm"
    releases:
      - stage: "coarse"
        model: "qwen3-reranker-0.6b"
        revision: "<REQUIRED>"
        checksum: "<REQUIRED>"
        quantization: "<REQUIRED>"
        tokenizer_revision: "<REQUIRED>"
        max_input: "<REQUIRED>"
        candidate_pool_limit: "<REQUIRED>"
        threshold_config_version: "<REQUIRED>"
      - stage: "final"
        model: "qwen3-reranker-8b"
        revision: "<REQUIRED>"
        checksum: "<REQUIRED>"
        quantization: "int8"
        tokenizer_revision: "<REQUIRED>"
        max_input: "<REQUIRED>"
        candidate_pool_limit: "<REQUIRED>"
        threshold_config_version: "<REQUIRED>"
    hardware_profile: "<REQUIRED>"
  judge:
    provider: "bailian"
    model: "qwen3.7-plus"
    mode: "non_thinking"
    credential_ref: "JUDGE_CREDENTIAL_REF"

auth:
  access_ttl_seconds: 900
  refresh_ttl_seconds: 604800
  refresh_reuse_grace_seconds: 5

retention:
  document_version_days: 30
  user_deletion_days: 30
  user_deletion_archive_dir: "<REQUIRED ABSOLUTE PRIVATE PATH>"
  notification_days: 90
  outbox_delivered_days: 30
  index_generation_rollback_days: 7

network:
  public_origin: "<REQUIRED HTTPS ORIGIN>"
  cors_allowlist: []
  ingress_sse_idle_timeout_seconds: 180

backup:
  base_backup_cadence: "daily"
  retention_days: 35
  rpo_minutes: 15
  rto_hours: 4
  restore_drill_cadence: "quarterly"

operations:
  admin_manifest_ref: "<REQUIRED>"
  secret_manager: "<REQUIRED>"
  observability_profile: "<REQUIRED>"
  backup_profile: "<REQUIRED>"
  provider_resilience:
    sync_max_attempts: 3
    async_max_attempts: 5
    retry_waits: ["250ms", "1s", "4s", "16s"]
    absolute_deadline_profile: "<REQUIRED PER OPERATION>"
    circuit_breaker: { scope: "provider+operation", failures_to_open: 5, open_seconds: 60 }
  worker_policy_refs:
    ingestion: "<REQUIRED lease/heartbeat/reconcile policy>"
    generation: { lease_seconds: 90, heartbeat_seconds: 30, max_physical_executions: 3 }
    evaluation: "<REQUIRED versioned evaluation-policy reference>"
    graph: "<REQUIRED lease/heartbeat/reconcile policy>"
    outbox: { lease_seconds: 60, heartbeat_seconds: 20, max_attempts: 8 }
```

### 4.1 Profile change classes

| Change | Action |
| --- | --- |
| Provider, model, model revision, tokenizer, quantization, hardware, endpoint, credential reference, sparse provider, analyzer, or schema | Create a new profile/release and, where an index is affected, a new staging index generation. Run capability and acceptance gates before activation. |
| `BUSINESS_TIMEZONE` | Set before the first ledger entry. After a business calendar has facts, a different timezone rejects startup; there is no runtime edit. |
| Document, user, notification, outbox, or index rollback retention | New values affect only newly materialized lifecycle records. Existing `purge_after_at`, `retire_after_at`, `compact_after_at`, and `rollback_until` values are not recomputed. |
| Auth TTLs, lease values, heartbeat values, or ingress timeouts | Treat as a coordinated release with frontend, worker, and ingress validation. Do not hot-edit a running deployment. |
| Resource replicas or capacity | Roll out through the platform after confirming the profile still satisfies isolation and rate-lane limits. |
| Backup RPO/RTO, retention, and restore-drill cadence | Change the customer backup profile and obtain explicit approval. The reference values are not business-contract defaults. |

### 4.2 Environment profiles

The repository supports four environment profiles. They share contracts and configuration names, but they do not share production credentials or data namespaces.

| Environment | Purpose | Allowed provider/storage exceptions | Promotion rule |
| --- | --- | --- | --- |
| `dev` | Local functional work | SQLite and deterministic provider stubs are allowed; `IMAGE_VLM_PROVIDER=none` and `RERANKER_PROVIDER=none` are allowed only here and in CI/test. | Never promoted as production evidence. |
| `ci` | Deterministic contract, migration, and compatibility gates | No live customer provider credential or customer object/data namespace. Stubs/fakes validate state machines and serialization. | Must prove migration/head, contract, and baseline validation gates. |
| `staging` | Production-like acceptance and recovery rehearsal | Uses isolated non-production PostgreSQL/object/index namespaces. Real provider probes are permitted only with dedicated non-customer credentials. | Must pass provider, SSE, backup/restore, and index acceptance checks before production. |
| `prod` | Customer-serving deployment | PostgreSQL and object storage are required fact sources; all production provider gates apply; `none` providers are rejected. | Uses immutable image/profile, release evidence, and an approved rollback/restore plan. |

Production and staging have different namespaces, secrets, backup sets, administrator manifests, and index generations. A staging success does not authorize reuse of its credentials or data-residency settings in production.

## 5. Production configuration registry

The deployment system must render a resolved value for every required field before starting a production workload. The registry below is the minimum set; provider-specific profile fields are required in addition to it.

| Configuration | Production value or rule | Startup/change behavior |
| --- | --- | --- |
| `BUSINESS_TIMEZONE` | Valid IANA timezone from the customer profile | Missing/invalid rejects startup; immutable after the first ledger fact. |
| `AUTH_ACCESS_TTL_SECONDS` | `900` | Fixed; changing requires an auth-compatible release. |
| `AUTH_REFRESH_TTL_SECONDS` | `604800` | Fixed absolute family TTL; `AUTH_SESSION_TTL_SECONDS` is not accepted. |
| `AUTH_REFRESH_REUSE_GRACE_SECONDS` | `5` | Fixed; applies only to the immediately consumed predecessor. |
| `DOCUMENT_VERSION_RETENTION_DAYS` | `30` by reference profile | Positive integer; snapshotted when a version enters retention. |
| `USER_DELETION_RETENTION_DAYS` | `30` by reference profile | Positive integer; snapshotted when deletion is accepted. |
| `USER_DELETION_ARCHIVE_DIR` | Dedicated absolute writable encrypted path outside static/upload/direct-read roots | Missing, relative, inaccessible, or unsafe path rejects startup. |
| `NOTIFICATION_RETENTION_DAYS` | `90` by reference profile | Positive integer; snapshotted at notification materialization. |
| `OUTBOX_DELIVERED_RETENTION_DAYS` | `30` by reference profile | Positive integer; applies only after all deliveries are delivered. |
| `INDEX_GENERATION_ROLLBACK_DAYS` | `7` by reference profile | Fixed into `rollback_until` when a generation is retired. |
| `GENERATION_DISCONNECT_GRACE_SECONDS` | `60` by reference profile | Coordinated with the SSE lease and ingress timeout. |
| `SPARSE_INDEX_PROVIDER` | `meilisearch` in the reference profile; `opensearch` is explicit alternative | Startup-only. A change requires a new generation and full acceptance. |
| `IMAGE_VLM_PROVIDER` | `bailian` in the reference profile; `internvl` is explicit alternative | `none` is allowed only in development, CI, and test; production rejects it. |
| `RERANKER_PROVIDER` | Registered vLLM implementation with both Qwen3 reranker stages | `none` is allowed only outside production; release must lock revision, checksum, quantization, tokenizer, and hardware. |
| `JUDGE_PROVIDER`, `JUDGE_MODEL`, `JUDGE_MODE` | `bailian`, `qwen3.7-plus`, `non_thinking` | Production capability probe is mandatory; no fallback to another model or credential. |
| `JUDGE_CREDENTIAL_REF` and `IMAGE_VLM_CREDENTIAL_REF` | Two distinct secret references | Missing, equal, or failed probes reject production startup. |
| Provider resilience policy | Profile reference for absolute deadlines; fixed bounded attempts, waits, and circuit-breaker rules below | Missing deadlines, hidden retries, or a mismatched circuit scope reject the release. |
| Worker policy references | Versioned ingestion/graph lease policy, generation policy, evaluation policy, and fixed outbox policy | Missing policy ownership, attempt budget, or reconcile/alert behavior rejects the release. |

Provider resilience is also a release gate: synchronous operations have at most three attempts, asynchronous operations at most five, waits are 250 ms/1 s/4 s/16 s with jitter and a shared absolute deadline, and the circuit breaker is scoped to `provider + operation`, opens after five retryable failures, and remains open for 60 seconds before one half-open probe. Each operation must declare its absolute deadline in the profile. SDK, proxy, and platform retries must not bypass these limits or hide a new `provider_call_id`.

Persisted worker policy must identify the responsible workload, reconcile cadence, lease/heartbeat source, attempt budget, and alert threshold. Generation execution uses a 90-second lease/30-second heartbeat and at most three physical executions under its locked budget policy. Outbox delivery uses a 60-second lease/20-second renewal and at most eight automatic attempts. Ingestion uses four automatic attempts with its fixed 1-minute/5-minute/30-minute job retry schedule. Evaluation limits are fixed by the versioned evaluation policy. Graph and ingestion lease/heartbeat durations remain explicitly versioned worker-policy data rather than undocumented platform defaults.

Generation, embedding, endpoint, data-residency, rate-limit, cost-catalog, GPU, and concurrency values are profile-required fields even when they do not have a global default. A missing value is not silently replaced by a local development default.

## 6. Secrets and administrator manifest

Secret references are resolved by the platform before the workload starts. At minimum, the deployment must provide separate references for database, object storage, provider credentials, signing keys, CSRF signing material, and the judge/image-VLM lanes. The resolved values must be unavailable to ordinary application read models and must be redacted from logs, traces, metrics, audit records, crash dumps, and support exports.

The `admin` manifest is declarative deployment input. It contains at least one active administrator and the account identity, role, and lifecycle policy needed to reconcile that account. Each manifest entry is an immutable `user_id` and reconciliation only ever matches by `user_id`, so renames never affect a seat. It may contain secret references but not secret values. Runtime APIs cannot create, edit, or delete an `admin` account. A manifest change is applied as a deployment change, audited, and followed by immediate session revocation/archive behavior for removed administrators.

On a new deployment, run `ragqs-identity-bootstrap-admin` once after Alembic and before any API or worker workload. Its Job alone receives the complete `RAG_AUTH_BOOTSTRAP_USERNAME`, `RAG_AUTH_BOOTSTRAP_PASSWORD`, `RAG_AUTH_BOOTSTRAP_REAL_NAME`, `RAG_AUTH_BOOTSTRAP_DISPLAY_NAME`, and `RAG_AUTH_BOOTSTRAP_USER_ID` group; the password is a secret reference and is never mounted into API or worker workloads, command arguments, logs, audits, or metrics. The bootstrap account is created under `RAG_AUTH_BOOTSTRAP_USER_ID`, which must be a pre-declared seat in the resolved administrator manifest; a bootstrap id outside the manifest is rejected. Re-running the Job only accepts the already-active matching initial seat and never repairs or replaces a nonempty identity database.

## 7. Deployment gates

No workload is considered ready until all applicable gates pass. A failing gate keeps the workload out of service; it does not start in a degraded mode that hides the failed dependency.

### 7.1 Preflight gates

1. Validate profile schema, deployment ID, environment, region, residency, required fields, and immutable release identifiers.
2. Validate secret references without logging secret values and verify the judge and image-VLM references are distinct.
3. Verify PostgreSQL connectivity, TLS policy, database identity, and the expected Alembic head.
4. Verify object-storage private access, encryption, persistence, checksum operations, and the deletion archive path.
5. Verify Milvus connectivity, collection naming, dimension, metric, and model registry compatibility. Never drop an unknown collection automatically.
6. Verify the selected sparse provider. Meilisearch requires persistent volume, authentication, and health checks. OpenSearch additionally requires TLS/authentication, JVM baseline, IK, and an analyzer probe.
7. Run generation, embedding, reranker, image-VLM, and judge capability probes using their profile credentials and deadlines. Production `none` providers fail the gate.
8. Verify at least one active administrator in the resolved manifest and confirm the manifest is not exposed through static or object download paths.
9. Verify the business calendar timezone and reject a mismatch with any already-recorded ledger calendar.
10. Verify OpenTelemetry/metrics export, redaction, alert routing, and release correlation fields before accepting traffic.
11. Verify provider resilience limits, circuit-breaker scope, and persisted worker-policy references; reject a profile whose values bypass the backend contract.

### 7.2 First deployment

1. Create the isolated namespace, service accounts, network policies, storage namespaces, and secret references.
2. Create the PostgreSQL database and private object namespace; record the initial backup profile.
3. Run the single-head Alembic migration through a one-shot migration workload using `alembic upgrade head`, then verify it with `alembic current --check-heads`. Multiple migration heads are a release failure. Record the schema revision and release ID.
4. Run the one-shot administrator bootstrap Job, then reconcile the administrator manifest and verify audit output and session behavior.
5. Deploy maintenance, API/query, and worker workloads in that order. Workers must not claim work until the migration and readiness gates pass.
6. Create the baseline index generation, build it from the empty or supplied source snapshot, and run the full consistency gate before exposing retrieval.
7. Run smoke checks for authentication/CSRF, upload and publication, query/SSE/reconnect, non-stream `/chat`, notification delivery, job cancel/replay authorization, and metrics redaction.
8. Enable external schedules only after the first successful backup and recovery-marker check.

### 7.3 Upgrade and migration

Use expand/contract migrations for production. The normal order is:

1. Create a pre-change backup set and record its `backup_id`.
2. Apply an additive Alembic migration with `alembic upgrade head`, then verify the database with `alembic current --check-heads` and the expected single head.
3. Roll out code that can read both old and new shapes.
4. Backfill through persisted, resumable maintenance work with progress and fencing.
5. Verify invariants and observability.
6. Remove old fields only in a later release after all workloads are compatible.

Production does not use an ad-hoc `alembic downgrade` as a rollback plan. A code rollback is allowed only when the current schema is backward-compatible with that image. A destructive schema change, provider change, or index implementation change requires a planned migration window or a new generation/release procedure.

### 7.4 Release, index rollback, and emergency restore

Application release rollback, index-generation rollback, and database/object restore are separate operations:

- Application rollback returns workloads to a compatible immutable image and does not change business facts.
- Index rollback is an `ops`-authorized atomic switch to an eligible, fully caught-up rollback candidate inside its fixed window.
- Database/object restore freezes retrieval, preview, download, and derived writes, restores facts in the fixed order, validates the object manifest, rebuilds derived stores, and reopens traffic only after consistency checks.

No operator may repair a production state by editing index documents, deleting a collection, changing a job status directly, or copying an object over a key without a recorded operation and checksum.

### 7.5 Shutdown and decommission

For planned shutdown, stop external schedulers, drain ingress, stop accepting new work, let workers finish or leave fenced recoverable attempts, and confirm no active leases are being renewed. Preserve PostgreSQL, object snapshots, deletion archives, audit records, and the final profile. Revoke credentials only after the final backup and manifest export are verified.

Decommissioning a deployment requires an approved retention decision. It must not be implemented as a namespace delete that silently removes the joint recovery source.

## 8. Go-live checklist

- [ ] Profile is versioned, fully resolved, and contains no secret values or placeholders.
- [ ] Namespace, database, object namespace, index prefixes, and administrator manifest are isolated to this deployment.
- [ ] PostgreSQL backup/PITR, object snapshot, manifest checksum, and restore owner are verified.
- [ ] Alembic head matches the release and the migration record is audited.
- [ ] Query and ingestion compute have separate placement/resource/GPU policies.
- [ ] Meilisearch or OpenSearch probe, Milvus model registry, and index generation gate pass.
- [ ] Production provider probes pass; judge and image-VLM credentials are distinct; no `none` provider is active.
- [ ] Ingress preserves CSRF and SSE headers, disables buffering, and allows the required timeout.
- [ ] Admin manifest has an active administrator and the removed-admin lifecycle has been tested.
- [ ] Authentication, upload, publication, query/SSE recovery, non-stream compatibility, A/B vote persistence, calibration window, outbox delivery, and job authorization checks pass.
- [ ] The non-stream `/chat` response returns the persisted `answer_mode` (including `no_context`) without inferring it from sources; A/B voting uses the persistent pair route and calibration state is read back from PostgreSQL.
- [ ] Dashboards, redaction, high-priority alerts, on-call ownership, and restore drill evidence are attached to the release.

## 9. Source of truth

Behavioral and data contracts remain defined by the backend and frontend design documents. This file defines how those contracts are deployed, isolated, gated, backed up, and released. When an implementation or an older runbook conflicts with those contracts, the design documents and this deployment profile are authoritative.
