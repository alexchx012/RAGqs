"""Readiness checks for real-provider RAG evaluation runs."""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.config import Settings, config
from app.evaluation.dataset_quality import validate_business_golden_dataset
from app.evaluation.models import GoldenExample
from app.operations.config_validation import validate_settings
from app.providers.selection import ProviderSelection


class EvaluationReadinessIssue(BaseModel):
    """One actionable issue that blocks or weakens a real evaluation run."""

    field: str
    message: str
    severity: str = "error"


class EvaluationReadinessReport(BaseModel):
    """Machine-readable readiness report for a real-provider evaluation run."""

    model_config = ConfigDict(populate_by_name=True)

    ready: bool
    mode: str
    dataset_examples: int = Field(alias="datasetExamples")
    errors: list[EvaluationReadinessIssue] = Field(default_factory=list)
    warnings: list[EvaluationReadinessIssue] = Field(default_factory=list)


def validate_real_evaluation_readiness(
    examples: Sequence[GoldenExample],
    *,
    settings: Settings | None = None,
    mode: str,
    faithfulness_judge: str,
    base_url: str = "http://127.0.0.1:8000",
    env: Mapping[str, str] | None = None,
    min_examples: int = 2,
) -> EvaluationReadinessReport:
    """Validate prerequisites before running service/http evaluation with real providers."""

    settings = settings or config
    env = env if env is not None else os.environ
    normalized_mode = _normalize_id(mode)
    normalized_judge = _normalize_id(faithfulness_judge)
    errors: list[EvaluationReadinessIssue] = []
    warnings: list[EvaluationReadinessIssue] = []

    if normalized_mode == "fake":
        errors.append(
            EvaluationReadinessIssue(
                field="EVALUATION_MODE",
                message="use service or http mode for real-provider evaluation",
            )
        )
    elif normalized_mode not in {"service", "http"}:
        errors.append(
            EvaluationReadinessIssue(
                field="EVALUATION_MODE",
                message=f"unsupported mode: {normalized_mode}",
            )
        )

    _validate_dataset(examples, errors, min_examples=min_examples)

    selection = ProviderSelection.from_settings(settings)
    if normalized_mode == "service":
        _validate_service_providers(selection, errors)
        _extend_startup_config_errors(settings, errors)
    elif normalized_mode == "http":
        _validate_http_target(base_url, errors)

    if normalized_judge not in {"none", "static", "model"}:
        errors.append(
            EvaluationReadinessIssue(
                field="FAITHFULNESS_JUDGE",
                message=f"unsupported judge: {normalized_judge}",
            )
        )
    elif normalized_judge == "model":
        _validate_model_judge_provider(selection, settings, errors)

    _validate_langsmith(env, errors, warnings)

    return EvaluationReadinessReport(
        ready=not errors,
        mode=normalized_mode,
        datasetExamples=len(examples),
        errors=errors,
        warnings=warnings,
    )


def _validate_dataset(
    examples: Sequence[GoldenExample],
    errors: list[EvaluationReadinessIssue],
    *,
    min_examples: int,
) -> None:
    report = validate_business_golden_dataset(
        examples,
        min_examples=min_examples,
        min_grounded_examples=1,
        min_refusal_examples=1,
        require_space_id=True,
    )
    for issue in report.errors:
        errors.append(EvaluationReadinessIssue(field=issue.field, message=issue.message))


def _validate_service_providers(
    selection: ProviderSelection,
    errors: list[EvaluationReadinessIssue],
) -> None:
    fake_provider_fields = {
        "CHAT_PROVIDER": selection.chat_provider,
        "EMBEDDING_PROVIDER": selection.embedding_provider,
        "VECTOR_STORE_PROVIDER": selection.vector_store_provider,
        "INGESTION_PROVIDER": selection.ingestion_provider,
    }
    for field, provider in fake_provider_fields.items():
        if provider == "fake":
            errors.append(
                EvaluationReadinessIssue(
                    field=field,
                    message="fake provider is not allowed for real-provider service evaluation",
                )
            )


def _extend_startup_config_errors(
    settings: Settings,
    errors: list[EvaluationReadinessIssue],
) -> None:
    startup_report = validate_settings(settings)
    for issue in startup_report.errors:
        errors.append(EvaluationReadinessIssue(field=issue.field, message=issue.message))


def _validate_http_target(base_url: str, errors: list[EvaluationReadinessIssue]) -> None:
    normalized = base_url.strip().lower()
    if not normalized:
        errors.append(
            EvaluationReadinessIssue(
                field="BASE_URL",
                message="must be set for http evaluation mode",
            )
        )
    elif not (normalized.startswith("http://") or normalized.startswith("https://")):
        errors.append(
            EvaluationReadinessIssue(
                field="BASE_URL",
                message="must start with http:// or https://",
            )
        )


def _validate_model_judge_provider(
    selection: ProviderSelection,
    settings: Settings,
    errors: list[EvaluationReadinessIssue],
) -> None:
    if selection.chat_provider == "fake":
        errors.append(
            EvaluationReadinessIssue(
                field="CHAT_PROVIDER",
                message="model faithfulness judge requires a real chat provider",
            )
        )
        return

    for issue in validate_settings(settings).errors:
        if issue.field.startswith("DASHSCOPE") or issue.field.startswith("OPENAI_COMPATIBLE"):
            errors.append(EvaluationReadinessIssue(field=issue.field, message=issue.message))


def _validate_langsmith(
    env: Mapping[str, str],
    errors: list[EvaluationReadinessIssue],
    warnings: list[EvaluationReadinessIssue],
) -> None:
    if _truthy(env.get("LANGSMITH_TRACING", "")):
        if not env.get("LANGSMITH_API_KEY", "").strip():
            errors.append(
                EvaluationReadinessIssue(
                    field="LANGSMITH_API_KEY",
                    message="must be set when LANGSMITH_TRACING=true",
                )
            )
        if not env.get("LANGSMITH_PROJECT", "").strip():
            errors.append(
                EvaluationReadinessIssue(
                    field="LANGSMITH_PROJECT",
                    message="must be set when LANGSMITH_TRACING=true",
                )
            )
    else:
        warnings.append(
            EvaluationReadinessIssue(
                field="LANGSMITH_TRACING",
                message="enable LangSmith tracing for real-provider diagnostics",
                severity="warning",
            )
        )


def _truthy(value: Any) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _normalize_id(value: Any) -> str:
    return str(value).strip().lower().replace("-", "_")
