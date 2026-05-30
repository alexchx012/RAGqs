## Context

The repository already implements a FastAPI RAG service with LangGraph-based
orchestration, provider-switched model/vector/session/storage boundaries, document
ingestion, retrieval tracing, knowledge-space lifecycle APIs, evaluation harnesses,
operational health gates, and local deployment scripts. The current OpenSpec baseline
does not yet describe these implemented contracts.

This change captures the current system behavior as OpenSpec requirements. The sources
of truth are the README, operations/deployment/evaluation/extension docs, baseline and
module tests, and the current `app/` implementation.

## Goals / Non-Goals

**Goals:**

- Record the current implemented capabilities as testable OpenSpec baseline specs.
- Preserve current limitations, especially where fake providers, preflight checks, or
  smoke gates validate software paths rather than production quality or capacity.
- Keep the baseline split by operational capability so future proposals can modify only
  the affected contract.

**Non-Goals:**

- No application, test, configuration, script, static UI, database, or dependency changes.
- No new RAG behavior, provider, API route, security control, or deployment automation.
- No claim that real business answer quality, 80-user capacity, or multi-instance
  production data behavior has already been validated.

## Decisions

- Create new delta specs for each baseline capability because `openspec/specs/` is empty.
  Alternative considered: one large baseline spec. Separate specs make future changes
  easier to review and archive without rewriting unrelated capability contracts.
- Use `## ADDED Requirements` only. There are no existing OpenSpec requirements to modify
  or remove.
- Describe externally observable behavior and provider boundaries, not internal line-by-line
  implementation. The specs name APIs, settings, provider ids, stores, and current
  limitations where those are part of the contract.
- Keep implementation tasks to OpenSpec artifact creation and validation. The requested
  change is a baseline capture, so "implementation" means completing the OpenSpec artifacts,
  not editing runtime code.

## Risks / Trade-offs

- Baseline specs can drift if future code changes bypass OpenSpec.
  Mitigation: future changes should modify these specs before implementation and archive
  deltas after completion.
- Some requirements summarize broad existing behavior across many files.
  Mitigation: each requirement includes concrete scenarios tied to routes, settings,
  provider ids, scripts, or documented gates.
- The current implementation has intentional gaps around real-provider quality, production
  observability, and multi-instance validation.
  Mitigation: specs explicitly mark those as non-claims so future optimization work starts
  from the real baseline.
