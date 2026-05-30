## ADDED Requirements

### Requirement: Local Startup Contract
The system SHALL provide documented local startup through PowerShell, Docker Compose, and
manual FastAPI commands.

#### Scenario: Start script runs preflight and starts core services
- **WHEN** `start.ps1` is run
- **THEN** it SHALL validate configuration, coordinate the local Milvus Docker stack, start
  FastAPI, and leave Milvus running by default when FastAPI exits

#### Scenario: Manual startup remains supported
- **WHEN** a developer starts Docker Compose and Uvicorn manually
- **THEN** the API and static UI SHALL be available at the configured host and port with
  API docs exposed by FastAPI

### Requirement: Docker Milvus Stack
The system SHALL define a local vector database stack with core and optional UI services.

#### Scenario: Core profile starts Milvus dependencies
- **WHEN** the default Docker Compose stack is used
- **THEN** etcd, MinIO, and Milvus standalone SHALL be the core services required for local
  vector storage

#### Scenario: Optional UI profile starts Attu
- **WHEN** the `ui` Docker profile is selected
- **THEN** Attu SHALL be included without being part of the default core profile

### Requirement: Deployment Gates
The system SHALL document and provide lightweight gates for local, staged, and production-like
readiness.

#### Scenario: Health and smoke gates are available
- **WHEN** a staged deployment is prepared
- **THEN** operators SHALL be able to run startup config validation, running API health
  preflight, non-destructive Milvus integration smoke, Postgres smoke, fake-provider load,
  and evaluation commands as appropriate for the selected provider setup

#### Scenario: Production mode blocks unsafe deployment values
- **WHEN** `DEPLOYMENT_ENVIRONMENT=production` is used before release
- **THEN** startup validation SHALL fail for local-only unsafe values including debug mode,
  fake providers, memory stores, disabled auth, disabled runtime controls, and localhost or
  wildcard CORS origins

### Requirement: Multi-Instance Data Layer Boundary
The system SHALL identify Postgres-backed stores as the current shared data-layer option
for multi-instance deployments.

#### Scenario: Multi-instance trial uses shared stores
- **WHEN** an internal multi-worker or multi-instance trial is prepared
- **THEN** session store, retrieval audit store, indexing queue, indexing job store,
  document catalog, and checkpoint provider settings SHALL be switched to Postgres and
  verified with Postgres smoke before traffic

#### Scenario: Smoke gate is not production proof
- **WHEN** Postgres smoke or fake load checks pass
- **THEN** the system SHALL treat them as readiness signals for configuration and software
  paths, not proof of real business answer quality, real 80-user capacity, or validated
  multi-instance production data behavior
