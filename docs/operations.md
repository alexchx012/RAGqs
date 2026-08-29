# RAGqs Operations

> Normative operating manual for the V1 production system. It follows `docs/deployment.md`, `docs/项目设计/后端设计.md`, `docs/项目设计/前端接口需求.md`, and the frontend operations design. It does not preserve legacy implementation or local-development procedures.

## 1. Operating contract

RAGqs is operated as one isolated deployment for one company. PostgreSQL and private object storage are the joint business recovery source. Milvus, sparse search, hierarchy, graph, and cache data are derived and must be rebuilt rather than treated as authoritative.

The operating rules are:

- Use protected APIs, the approved operations CLI, or the platform release workflow. Do not edit business rows, index records, object keys, lease fields, or status values directly.
- Every recovery action is idempotent, uses the documented idempotency key or expected version, and leaves an audit record.
- A worker that loses its lease or fencing token stops immediately. It must not publish, notify, charge usage, or change a terminal state after losing authority.
- A browser disconnect, a provider timeout, or a worker restart is not a reason to create a duplicate generation, document job, notification, or A/B pair.
- No provider fallback is silent. User-visible notices, persisted failure classifications, usage records, and alerts must explain every allowed degradation.
- Production configuration is profile data. Provider, model, analyzer, hardware, residency, retention, and concurrency changes go through a release or index-generation procedure; they are not hot edits.
- A backup is not complete until PostgreSQL, object storage, and the object manifest share a verified `backup_id`.

The behavioral authority for jobs, generation, A/B, calibration, notifications, ACL, and retention is the backend design. This file adds the operational sequence and ownership needed to run those contracts.

## 2. Roles and access

| Actor | Operational capability |
| --- | --- |
| `ops` | Inspect operational projections, cancel/replay eligible jobs, open/close calibration windows, trigger or cancel graph builds, replay an outbox dead letter, and initiate an eligible index-generation rollback. |
| `admin` | Read operational projections and permission matrices. The operations queue is read-only for `admin`, but an `admin` may cancel or replay an eligible direct upload, replace, restore, or reindex job they initiated when the backend returns that action in `allowed_actions`. It cannot perform other listed operational mutations or manage `admin` accounts through runtime APIs. |
| Platform operator | Manages Kubernetes, ingress, node pools, secret delivery, managed PostgreSQL/object storage, backups, and release artifacts. This is an infrastructure identity, not an application role. Application mutations still use the protected API and audit path. |
| Incident commander | Coordinates an incident, decides traffic freezes and escalation, and records the final timeline. It does not bypass application authorization. |

The frontend operations queue is only a view of job facts. `allowed_actions` returned by the backend is the sole authority for whether cancel or replay is shown. Outbox dead-letter details and replay are not exposed in the frontend queue; they use the protected operations API/CLI.

## 3. Observability and service signals

### 3.1 Correlation fields

Every log, trace, metric exemplar, audit record, and support export that represents an operation must carry the fields that exist for that operation:

`request_id`, `trace_id`, `provider_call_id`, `generation_id`, `execution_id`, `job_id`, `attempt_id`, `replay_generation`, `fencing_token`, `publication_id`, `document_version_id`, `index_generation_id`, `index_revision`, `graph_build_id`, `source_revision`, `event_id`, and `backup_id`.

Do not emit prompts, candidate answers, document content, snippets, credentials, provider raw responses, grant fingerprints, or deletion-archive contents into logs, metrics, dashboards, or normal operations responses.

### 3.2 Required dashboards

The dashboard API returns counts, distributions, thresholds, and links, not document content. The frontend operations views consume the backend values; they do not calculate thresholds or infer hidden states.

| Dashboard | Required signals |
| --- | --- |
| Task health | Ingestion and evaluation backlog, running/retry/dead-letter counts, lease expiries, stale-reclaim count, cancel/replay outcomes, and age distributions. |
| Generation health | Running executions, execution attempts, deadline failures, provider reconciliation backlog, SSE subscription leases, stop reasons, and terminal latency. |
| Cost sentinel | Provider/local usage reconciliation, quota debit mismatches, judge/image-VLM lane saturation, and cost-catalog version. |
| Ingestion quality | OCR confidence distribution, tree/basic split, publication failures, contextual-retrieval degradation, and index staging cleanup failures. |
| Index health | Active/staging/retired generations, applied and rollback revisions, component lag, acceptance-gate results, orphan entries, and GC failures. |
| Notification health | Outbox pending/running/retry/dead-letter counts, delivery age, lease expiry, recipient materialization outcomes, and compaction eligibility. |
| Recovery and security | Latest backup and restore-drill status, object checksum failures, archive failures, readiness/probe failures, authentication/CSRF failures, and admin-manifest changes. |

The frontend `GET /metrics/dashboard` and `GET /metrics/operations` endpoints are read-only projections. The `GET /ops/jobs` view `stale` is a derived filter, not a persisted job state.

### 3.3 Alert priorities

The following alerts page the on-call owner immediately:

- PostgreSQL or object storage unavailable, backup missing, manifest mismatch, or restore validation failure;
- any production capability probe failure for judge, image VLM, reranker, embedding, or the selected sparse provider;
- an outbox delivery entering `dead_letter`;
- a provider reconciliation item remaining `unknown` beyond its reconciliation deadline;
- repeated fencing conflicts, stale leases that cannot be reclaimed, or an old worker attempting a conditional commit;
- an active generation failing its consistency gate, an active index generation becoming unavailable, or a rollback candidate losing catch-up eligibility;
- deletion archive missing/corrupt, retention cleanup halted, or an administrator manifest removing the last active admin;
- authentication signing/CSRF failures indicating a secret or ingress mismatch.

Backlog growth, latency, and cost thresholds are supplied by the resolved deployment profile and backend dashboard. Operators must not replace a missing threshold with a local guess; a missing production threshold is a release defect.

## 4. Routine operation and schedules

No business scheduler runs inside the API process. External CronJobs/workflows create persisted work through protected APIs or invoke the approved CLI. Workers reconcile persisted state at startup and during their normal loop.

| Activity | Required behavior |
| --- | --- |
| Worker startup | Scan `pending`, due `retry_wait`, expired leases, and provider-reconciliation records before claiming new work. Do not rely on an in-memory queue. |
| Generation lease | Default 90-second execution lease, renewed every 30 seconds. Subscription leases are also 90 seconds with a 30-second SSE heartbeat and 60-second disconnect grace. |
| Outbox delivery | 60-second delivery lease, renewed every 20 seconds. Delivery attempts and fencing tokens are persisted. |
| Ingestion attempt | One job has at most four automatic attempts: initial plus three retries after 1 minute, 5 minutes, and 30 minutes with jitter. |
| Ingestion worker | The resident `ragqs-ingestion-worker` claims pending/due jobs, renews the five-minute attempt lease every 20 seconds while processing, stages indexing output, and commits publication through the fenced documents transaction. `RAG_INGESTION_*` settings control lease, heartbeat, and poll intervals. |
| Outbox retry | Up to eight automatic attempts with waits of 5 seconds, 30 seconds, 2 minutes, 10 minutes, 30 minutes, 2 hours, and 6 hours with jitter; the eighth failure is `dead_letter`. |
| Shadow evaluation | An external schedule calls `POST /admin/evaluations/shadow-runs` with an idempotency key. The HTTP handler only creates a queued run. |
| Graph maintenance | `ops` explicitly creates a graph run. There is no automatic trigger or automatic replay. |
| Chat maintenance | An external schedule invokes `ragqs-chat-maintenance` with `RAG_MAINTENANCE_KEY`; it reaps generation leases and executes queued chat generations. |
| Usage maintenance | An external schedule invokes `ragqs-usage-maintenance` with `RAG_MAINTENANCE_KEY`; each tick reconciles unknown provider calls, recovers expired local usage meters, and processes quota cancellation candidates. |
| Documents maintenance | An external schedule invokes `ragqs-documents-maintenance` with `RAG_MAINTENANCE_KEY`; it deletes private objects for withdrawn or invalidated submissions and records the cleanup timestamp. The operation is idempotent. |
| Backup maintenance | The resident `ragqs-backup-maintenance` worker claims due schedule windows, executes backups under the write gate, drives restores, and applies retention expiry from persisted state. Cadence, gate settle/drain timings and sweep batch size come from the `RAG_BACKUP_*` settings. |
| Retention and GC | Maintenance work is persisted and idempotent. Its cadence is a profile value; it must be frequent enough to honor lifecycle deadlines and must never remove an active reference. |
| Backup | Use the deployment backup profile, record `backup_id`, validate the object manifest, and alert on any incomplete component. |
| Restore drill | Run at the cadence declared in the customer backup profile; the reference profile proposes quarterly drills, with evidence attached to the release/operations record. |

## 5. Standard incident procedure

1. Assign an incident commander and record start time, deployment ID, environment, release ID, and current `backup_id`.
2. Classify the incident as availability, correctness/data, security, cost, provider, or derived-index failure.
3. Preserve correlation IDs and the last known audit events. Do not copy sensitive payloads into the incident channel.
4. Apply the smallest reversible mitigation: stop an external scheduler, remove traffic from an unready workload, open a provider circuit, pause derived writes, or freeze retrieval/preview/download during recovery.
5. Use the relevant runbook below. Every state transition must go through its protected API/worker protocol.
6. Verify both the business fact source and the derived/read path before reopening traffic.
7. Record root cause, affected release/profile/generation, data-integrity conclusion, user-visible notices, and follow-up tests.

When correctness is uncertain, preserve PostgreSQL and object storage, stop derived writes, and prefer an explicit failure over an answer or publication that cannot be proven authoritative.

## 6. Runbooks

### 6.1 Readiness or API outage

**Trigger:** readiness failures, elevated HTTP errors, failed SSE establishment, or missing dashboard export.

1. Confirm the ingress route, TLS certificate, service endpoints, pod readiness, and release digest.
2. Check PostgreSQL connectivity and migration head, object-storage private access, Milvus, sparse-provider probe, secret-manager resolution, and telemetry export.
3. Compare the failure time with a rollout, profile change, secret rotation, migration, or index-generation switch.
4. Remove only failing replicas from service. Do not force readiness by disabling dependency checks.
5. Roll back the image only if its schema is compatible. A database restore or index rollback is a separate runbook.

**Verify:** readiness is green on more than one replica, a synthetic authenticated request succeeds, telemetry is correlated, and no hidden degraded mode is masking a missing fact source.

### 6.2 Ingestion backlog or failed job

**Trigger:** `active`/`replayable` queue growth, `tasks_health` threshold, repeated `failed`/`dead_letter`, or a user reports a missing publication.

1. Query `GET /ops/jobs?view=all|active|replayable|stale` when operating as `ops`, or use the authorized job/business view for an initiator's own job. Record `job_id`, state, attempt, replay generation, next attempt time, failure classification, and allowed actions.
2. Check whether the issue is provider capacity, object access, parser/OCR, index staging, authorization scope, or a stale lease. Do not interpret a response window as deletion of older jobs.
3. For `pending`, `running`, and `retry_wait`, use only `POST /ingestion-jobs/{job_id}/cancel` when cancellation is required.
4. For `failed`, `cancelled`, or `dead_letter`, use only `POST /ingestion-jobs/{job_id}/replay` when all ACL, version, source checksum, lifecycle, and `purge_after_at` preconditions pass. Supply a new idempotency key for the new replay request and reuse it if the response is unknown.
5. Never reset `attempt_number`, revive a discarded publication, or create a second job by inserting a row.

**Verify:** the new attempt has a new staged `publication_id`, the old history remains immutable, usage is attributed to the correct attempt/replay generation, and the document becomes queryable only after the active publication transaction succeeds.

### 6.3 Stale ingestion lease or worker crash

**Trigger:** expired `running` attempt, repeated fencing conflict, worker termination, or staged artifacts with no active owner.

1. Locate the job, attempt, lease owner, lease expiry, and fencing token using the operations projection.
2. Confirm the old worker is terminated or isolated before the reclaimer advances the job. The reclaimer uses PostgreSQL time and conditional updates.
3. Let the persisted recovery transaction mark the attempt `expired` and either schedule the next retry or enter `dead_letter`.
4. Inspect and later clean staged objects/index entries by `(attempt_id, backend_kind, resource_id)`. They remain invisible throughout.
5. If the old worker still emits commits, isolate its workload and treat it as a fencing incident; do not accept a manual “last writer wins” repair.

**Verify:** exactly one current lease exists, the old fencing token cannot update any fact, and the next attempt has the expected replay/configuration snapshot.

### 6.4 Provider degradation, circuit breaker, or rate limit

**Trigger:** provider 429/5xx/timeout rate, an open `provider + operation` circuit, `retrieval_degraded`/`rerank_degraded` notices, or a capability probe failure.

1. Identify provider, operation, model revision, `provider_call_id`, deadline, retry count, and lane (query, ingestion, judge, or image VLM).
2. Respect the fixed bounded retry policy: synchronous provider operation up to three attempts, asynchronous operation up to five, with 250 ms/1 s/4 s/16 s jittered waits and one absolute deadline.
3. Do not add an SDK or proxy retry layer that is invisible to usage accounting.
4. Allow only the documented fallback: vector to sparse, sparse to vector, reranker to candidate order, or no answer when both retrieval paths fail. Every fallback emits its notice and failure/usage record.
5. A production `none` provider is a configuration failure, not a mitigation. Restore the approved provider or stop the affected capability.

**Verify:** the circuit state, failure reason, retry budget, and expected recovery time are visible; no duplicate usage or answer was created; and the profile/probe is corrected before closing the incident.

### 6.5 Unknown provider result during generation

**Trigger:** a generation execution has a provider call in `dispatching` or `unknown` after a connection failure, timeout, or worker restart.

1. Chat provider reconciliation is **not implemented**: there is no `provider_reconciling` execution status and no worker protocol to enter one. Never report or record a reconciled outcome for a chat generation; that state does not exist.
2. If the provider result is unknown, let the existing execution protocol run its course: the attempt either completes on the persisted execution or transitions to `retry_wait`/`failed` with the recorded error classification. Do not mark an unknown outcome as completed.
3. Optional manual forensics: query the provider's supported request-status API, provider logs, or billing record using the immutable `provider_call_id` and idempotency key. Findings inform incident review only; they must not mutate the generation's terminal state.
4. If the result remains unknown at the deadline, the execution fails with the recorded classification and the usage ledger keeps the original outbound attempt exactly once. Do not create a new user-visible generation automatically.

**Verify:** the generation has at most one terminal event, the user can use the normal failed-generation retry flow if permitted, and the usage ledger contains the original outbound attempt exactly once.

### 6.6 Generation execution or SSE recovery

**Trigger:** client reports a frozen answer, SSE disconnect, duplicate event, stale `running` generation, or an execution lease expiry.

1. Read the message/generation state and the persisted `generation_execution`; do not infer state from the browser.
2. A connection loss alone does not stop execution. The client reconnects through `GET /generations/{generation_id}/events` with `Last-Event-ID`; the server replays persisted events before live events.
3. Check the 30-second heartbeat, 90-second subscription/execution leases, ingress timeout, and 60-second disconnect deadline.
4. If a lease expired, let the recovery transaction create the next execution within the same generation and budget. It must not create another user message, assistant message, or generation.
5. If a session was revoked, stop the associated generation through the authorization-revocation state machine. Natural access/refresh expiry alone does not stop an already running generation.
6. If the generation deadline or execution recovery budget is exhausted, allow one `failed` terminal event. If the user explicitly stops it, use `POST /generations/{generation_id}/stop`.

**Verify:** event IDs are monotonic and non-reused, exactly one `done`/`error`/`stopped` terminal event exists, stable answers are unchanged, and an old worker cannot append after fencing loss.

### 6.7 Outbox delivery dead letter

**Trigger:** a high-priority `dead_letter` alert or a delivery that remains in `retry_wait` beyond its operational threshold.

1. Read `GET /ops/outbox-deliveries/{event_id}?consumer_name=in_app_notification` as `ops`. Record status, version, replay generation, attempts, and the last error code. The endpoint does not return payload or recipient content.
2. Confirm the source event is still `storage_state=full`, the consumer is supported, and the incident is not a transient database/secret/permission issue.
3. Correct the dependency or consumer first. Do not replay a dead letter to hide a deterministic schema or payload error.
4. Submit `POST /ops/outbox-deliveries/{event_id}/replay` with `consumer_name=in_app_notification`, the current `expected_version`, and an idempotency key.
5. Poll the protected read endpoint until delivered or a new dead letter is recorded. Do not expose a frontend queue item or create a second outbox event.

**Verify:** the original event, recipient snapshot, and old attempts remain immutable; the replay uses a new replay generation but the same event and business unique key; audit contains acceptance, rejection, and completion.

### 6.8 Index generation build, publish, rollback, or GC

**Trigger:** staging generation failure, revision lag, retrieval acceptance failure, active-generation outage, or GC alert.

1. Record active generation, candidate generation, `base_revision`, `applied_revision`, component manifests, embedding dimension/metric/model, sparse provider/analyzer, and release gate versions.
2. Build from the frozen snapshot, then apply every `index_change` in strict revision order. A missing or conflicting revision stops the build; it is never skipped.
3. Run the complete gate: active document/publication coverage, no pending-delete entries, identifier consistency, vector compatibility, sparse frozen Chinese suite, hierarchy/graph checks, no orphan/duplicate chunks, and no unprocessed revision.
4. Switch `active_generation_id` atomically only after the gate passes. Keep the former generation as the sole rollback candidate for seven days.
5. For rollback, `ops` verifies the candidate is within its fixed window, fully caught up to the current revision, and passes the same gate. Then use the protected rollback operation; do not activate an old collection manually.
6. GC each component idempotently after the window and reference leases expire. A component failure delays cleanup but never changes the active pointer.

**Verify:** queries use one generation for their full lifetime, old and new entries cannot mix, and PostgreSQL remains the source of active publication truth.

### 6.9 Public graph build

**Trigger:** `graph_availability=stale|disabled`, an explicit maintenance request, run failure, or source revision change.

1. `ops` reads `GET /ops/graph-builds/current` and records current `source_revision`, latest run, estimated calls, and allowed actions.
2. Submit `POST /ops/graph-builds` with the expected source revision and idempotency key. The run is public-library only and freezes its source publication set, provider/model/prompt, and deterministic call plan.
3. If a non-terminal run exists, do not create another. If the public source changes, the staged output is discarded and the run fails with `graph_source_changed`.
4. Cancel only a queued/running run through its protected cancel endpoint with the expected version. A failed run requires a new run; it is never automatically replayed.

**Verify:** only a generation built against the current source revision can become active, stale graph data is not used for query routing, usage is attributed to the graph run, and one `graph_build_completed` outbox event exists for the terminal transition.

### 6.10 Shadow evaluation and judge lane

**Trigger:** scheduled evaluation, a release gate, a policy comparison, or a judge capability/rate alert.

1. The external scheduler calls `POST /admin/evaluations/shadow-runs` with an idempotency key. It receives `202` and a persisted `run_id`; it does not wait for the batch in the HTTP handler.
2. `ops`/`admin` reads `GET /admin/evaluations/shadow-runs/{run_id}` for state, progress, attempt, failure classification, report reference, and locked configuration versions. The response contains no question, answer, or document content.
3. Verify the run has its own session prefix, snapshot, active generation, evaluation policy version, model/prompt versions, and judge release.
4. Judge V1 is `bailian` + `qwen3.7-plus` + `non_thinking`. Its credential and rate lane are separate from the image-VLM lane. A failed capability probe blocks production startup or new non-production runs; it never falls back to a chat model.
5. A failed run is investigated and rerun through a new idempotent operation according to the policy. It does not mutate online query configuration.

**Verify:** the report is immutable and comparable only to runs with the same policy/release versions; online traffic and quotas are unaffected.

### 6.11 Calibration window and A/B operations

**Trigger:** a cold-start/sentinel eligibility signal, an operations request to open/close a window, expired pairs, or an A/B vote reconciliation issue.

1. Read `GET /calibration/window`. The only persistent window is in PostgreSQL and transitions `open -> closing -> closed`; at most one window is open.
2. `ops` opens or closes through `POST /calibration/window` with an idempotency key and only the action/window kind. The client cannot override sample rate, thresholds, minimum query count, or policy version.
3. At open, snapshot the policy version and sample rate. At close, honor the close deadline and expire open pairs at the earlier of pair TTL and window deadline.
4. A/B candidates, pair status, publication identity, and votes are persisted independently. Normal feedback is separate from A/B voting. The only vote route is `POST /messages/{id}/ab-vote` with the pair identity and idempotency key.
5. The first ten votes cannot automatically change the default. A/B configuration identity is not revealed to voters. A user preference `ab_opt_out=true` skips new sampling but does not alter existing pairs.
6. Treat `ab_pair_expired`, duplicate idempotency, and already-voted responses as authoritative server outcomes. Do not repair a pair by changing candidate order or inserting a vote.

**Verify:** the window/pair facts, candidate publications, vote result, and policy snapshot survive refresh and worker recovery; aggregates contain counts only and remain within the originating ACL domain.

### 6.12 Retention, deletion, and archive cleanup

**Trigger:** a retention schedule, pending deletion, archive checksum failure, or a cleanup backlog.

1. For document versions, use the value snapshotted at the time the version became `superseded`, `failed`, or `cancelled` (reference 30 days). Never recompute existing `purge_after_at` after a profile change.
2. For user deletion, verify the absolute private `USER_DELETION_ARCHIVE_DIR`, atomic archive name `{user_id}-{deletion_id}.zip`, manifest, file checksums, and completion timestamp before destructive cleanup.
3. If an archive is missing or corrupt, halt new destructive cleanup targets and keep affected accounts in `pending_delete`. Do not make the archive available through a business API or static path.
4. Track each deletion target by `(deletion_id, backend_kind, resource_id)` and retry components independently. Shared documents remain owned by their knowledge space; account deletion does not delete them.
5. Notification retention defaults to 90 days and delivered outbox retention to 30 days. Compact only full events whose deliveries are all delivered and whose `compact_after_at` has passed. Dead letters block ordinary compaction.

**Verify:** the archive and lifecycle audit are durable, no active document or ACL fact was removed prematurely, and the resulting notification receipts/outbox summaries preserve the required audit behavior.

### 6.13 Backup and restore

**Trigger:** scheduled backup, backup alert, data corruption, region outage, or restore drill.

1. Freeze retrieval, preview, download, and derived writes. Stop external schedulers and announce the maintenance state.
2. Select a backup set whose PostgreSQL position, object snapshot, and object manifest share one verified `backup_id`.
3. Restore PostgreSQL first and verify migration head, deployment identity, ACL, document/version facts, jobs, publications, sessions, usage, quotas, outbox, notifications, and audit records.
4. Restore object storage and verify every required key, size, and checksum against the manifest. Inconsistent objects remain invisible and enter repair handling.
5. Rebuild Milvus, sparse, hierarchy, graph, and cache in that order. Build a new staging index generation and run the complete consistency gate.
6. Reopen traffic only after the active generation, publication visibility, provider probes, and telemetry are healthy. Record achieved RPO/RTO and all gaps.

**Verify:** a restore drill has evidence for each component, the release record contains the `backup_id`, and no operator used a derived index to fill a missing fact-source row.

### 6.14 Authentication, secrets, and administrator change

**Trigger:** secret rotation, refresh reuse alert, CSRF failure spike, administrator manifest change, or session revocation request.

1. Resolve the new secret through the secret manager and run the relevant capability/readiness probes before removing the old reference.
2. Preserve the fixed auth values: access TTL 900 seconds, refresh family absolute TTL 604800 seconds, and refresh reuse grace 5 seconds. The old `AUTH_SESSION_TTL_SECONDS` name is invalid.
3. Refresh tokens remain host-only secure HTTP-only cookies; CSRF cookie/header/Origin checks remain enabled. Do not ask users to place tokens in local storage.
4. A session revocation stops its active subscriptions and running generations through the persisted state machine. Natural token expiry alone does not stop an already running generation.
5. Apply administrator additions/removals through the declarative manifest. Runtime admin CRUD is forbidden; removing an admin triggers the documented revoke/archive lifecycle and must not leave zero active admins.

**Verify:** old credentials are no longer accepted, no secret appears in telemetry, affected sessions/generations have the expected terminal behavior, and the manifest/audit trail is complete.

### 6.15 Release and rollback

**Trigger:** a planned release, migration, provider/model change, index release, or rollback request.

1. Confirm release image digest, profile version, migration plan, acceptance suites, backup `backup_id`, and on-call owner.
2. Apply additive Alembic changes first, verify the head, then roll out compatible API/query and worker versions. Do not roll out workers that can write a schema the API cannot read.
3. For provider/model/analyzer/reranker changes, create a new release record and, where applicable, a new index generation. A failed gate leaves the current active generation and query configuration unchanged.
4. For application rollback, use only an image compatible with the current schema. For index rollback, use the eligible generation procedure. For data recovery, use the restore runbook. Do not conflate the three.
5. After rollout, run authentication, upload/publication, query/SSE reconnect, non-stream `/chat`, job cancel/replay, metrics, notification, calibration, A/B, and provider-probe smoke checks. Assert that non-stream `/chat` returns the persisted `answer_mode` value, including `no_context`, and never derives it from `sources`; assert that `POST /messages/{id}/ab-vote` and `GET/POST /calibration/window` round-trip their PostgreSQL facts.

**Verify:** all workloads report the same release/profile, no old worker retains a valid fencing token, dashboards are healthy, and the release record has test and migration evidence.

## 7. Operational prohibitions

The following actions are forbidden in production:

- direct SQL updates to job, attempt, generation, lease, fencing, publication, outbox, notification, ACL, quota, or lifecycle state;
- manual deletion or replacement of a Milvus collection, sparse index, object key, graph generation, or cache entry as a substitute for a persisted operation;
- enabling `IMAGE_VLM_PROVIDER=none` or `RERANKER_PROVIDER=none` in production;
- adding an unobserved retry layer, bypassing provider deadlines, or replaying an unknown provider request blindly;
- starting a graph build without the current `source_revision`, replaying a failed graph run automatically, or using stale graph data for routing;
- opening a calibration window or changing evaluation thresholds from the frontend;
- exposing outbox payloads, recipient identities, document content, prompts, candidate answers, or deletion archives in operations dashboards;
- restoring only one side of the PostgreSQL/object-storage fact source and reopening derived reads;
- deleting backups, archives, audit records, or immutable attempts to make an alert disappear.

## 8. Audit record for every operational action

The operator record must include:

```text
incident_or_change_id:
deployment_id:
environment:
operator_identity:
role:
action:
target_id:
expected_version_or_idempotency_key:
release_id:
profile_version:
active_generation_id:
backup_id:
before_state:
after_state:
reason:
validation_evidence:
user_visible_impact:
rollback_or_follow_up:
started_at_utc:
completed_at_utc:
```

For a rejected action, record the server error code and preserve the original state. For an accepted action whose response is lost, reuse the same idempotency key and read the persisted result rather than issuing a new operation.

## 9. Quick protected interface reference

| Operation | Interface | Role |
| --- | --- | --- |
| Job inspection | `GET /ops/jobs?view=all|active|replayable|stale` | `ops`; `admin` has a read-only operations projection |
| Job cancel/replay | `POST /ingestion-jobs/{job_id}/cancel`, `POST /ingestion-jobs/{job_id}/replay` | Backend ACL and `allowed_actions`; an `admin` may operate only an eligible self-initiated direct upload, replace, restore, or reindex job |
| Generation stop/recovery | `POST /generations/{generation_id}/stop`, `GET /generations/{generation_id}/events` | Authenticated owner and persisted worker state |
| Outbox inspection/replay | `GET/POST /ops/outbox-deliveries/...` | `ops` only |
| Graph build | `GET /ops/graph-builds/current`, `POST /ops/graph-builds`, protected cancel | `ops` only |
| Shadow evaluation | `POST /admin/evaluations/shadow-runs`, `GET .../{run_id}` | Run creation `ops`; read `ops`/`admin` |
| Calibration | `GET/POST /calibration/window` | Read per contract; mutations `ops` only |
| A/B vote | `POST /messages/{id}/ab-vote` | Current authorized voter; non-owner or cross-space votes return `403 ab_vote_forbidden`; idempotent |
| Metrics | `GET /metrics/dashboard`, `GET /metrics/operations` | Role-filtered read projection |

These interfaces are not permission shortcuts. The server recomputes current ACL, lifecycle, version, lease, and idempotency conditions for every operation.

## 10. Completion criteria

An incident, maintenance action, or release is complete only when:

- the authoritative facts and derived/read views agree;
- all leases and fencing tokens have one clear owner or a persisted terminal/retry state;
- no hidden retry, orphan staging artifact, unacknowledged dead letter, or unresolved provider result remains;
- dashboards and alerts show the expected steady state;
- the operation, user-visible impact, backup/generation identifiers, and evidence are audited.
