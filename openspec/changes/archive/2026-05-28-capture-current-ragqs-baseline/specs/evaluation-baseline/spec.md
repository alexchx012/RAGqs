## ADDED Requirements

### Requirement: Golden Dataset Contract
The system SHALL load UTF-8 JSONL golden evaluation examples with stable fields for RAG
regression checks.

#### Scenario: Golden examples include expected answer and source data
- **WHEN** a dataset row is loaded
- **THEN** it SHALL provide id, question, expected answer traits, expected sources,
  unsupported-question refusal expectation, and optional metadata such as `spaceId`

#### Scenario: Business dataset readiness is validated
- **WHEN** real-provider evaluation preflight is requested
- **THEN** the system SHALL reject duplicate ids, insufficient example count, unscoped
  real-provider examples, grounded examples without traits or sources, and refusal examples
  that define answer traits or sources

### Requirement: Evaluation Runner Modes
The system SHALL provide fake, in-process service, and live HTTP evaluation modes.

#### Scenario: Fake mode runs without external dependencies
- **WHEN** `scripts/run-evaluation.ps1` is run with default mode
- **THEN** the runner SHALL execute deterministic fake evaluation without DashScope, Milvus,
  or network access and SHALL mark the quality conclusion as not real quality validated

#### Scenario: Service and HTTP modes run real RAG paths
- **WHEN** evaluation runs in `service` or `http` mode
- **THEN** examples SHALL be executed through the in-process RAG service or a live `/chat`
  API client while propagating per-example knowledge-space metadata

### Requirement: Evaluation Metrics And Reports
The system SHALL aggregate evaluation results into JSON-compatible reports and enforce
configured metric thresholds.

#### Scenario: Metrics are computed
- **WHEN** evaluation results are compared with golden examples
- **THEN** the report SHALL include total examples, retrieval hit rate, answer trait
  coverage, answer faithfulness, citation accuracy, no-answer refusal rate, average
  latency, and per-example failures

#### Scenario: Reports can be written as artifacts
- **WHEN** `--report-path` or the PowerShell `-ReportPath` wrapper option is provided
- **THEN** the system SHALL write the JSON report to that path and still return failure
  when configured thresholds are not met

### Requirement: Real Evaluation Preflight
The system SHALL support a preflight-only mode that validates real-provider evaluation
readiness without running examples.

#### Scenario: Preflight rejects fake or incomplete real setups
- **WHEN** preflight is run for real-provider service or HTTP evaluation
- **THEN** it SHALL reject fake service providers, weak datasets, missing model-judge
  credentials, invalid HTTP targets, and incomplete LangSmith settings when tracing is
  enabled

#### Scenario: Preflight does not claim quality
- **WHEN** preflight completes
- **THEN** the report SHALL use `status: not_run` and `qualityConclusion: not_assessed`
  because examples were not executed
