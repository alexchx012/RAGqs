"""Golden dataset quality checks for business RAG evaluation."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.evaluation.models import GoldenExample


class GoldenDatasetQualityIssue(BaseModel):
    """One actionable dataset quality issue."""

    field: str
    message: str
    severity: str = "error"


class GoldenDatasetQualityReport(BaseModel):
    """Machine-readable quality report for a business golden dataset."""

    model_config = ConfigDict(populate_by_name=True)

    ready: bool
    dataset_examples: int = Field(alias="datasetExamples")
    grounded_examples: int = Field(alias="groundedExamples")
    refusal_examples: int = Field(alias="refusalExamples")
    errors: list[GoldenDatasetQualityIssue] = Field(default_factory=list)
    warnings: list[GoldenDatasetQualityIssue] = Field(default_factory=list)


def validate_business_golden_dataset(
    examples: Sequence[GoldenExample],
    *,
    min_examples: int = 4,
    min_grounded_examples: int = 1,
    min_refusal_examples: int = 1,
    require_space_id: bool = True,
) -> GoldenDatasetQualityReport:
    """Validate that a golden dataset is strong enough for business RAG evaluation."""

    errors: list[GoldenDatasetQualityIssue] = []
    warnings: list[GoldenDatasetQualityIssue] = []
    seen_ids: set[str] = set()
    grounded_examples = [example for example in examples if not example.expects_refusal]
    refusal_examples = [example for example in examples if example.expects_refusal]

    if len(examples) < min_examples:
        errors.append(
            GoldenDatasetQualityIssue(
                field="DATASET",
                message=f"include at least {min_examples} golden examples",
            )
        )
    if len(grounded_examples) < min_grounded_examples:
        errors.append(
            GoldenDatasetQualityIssue(
                field="DATASET",
                message=f"include at least {min_grounded_examples} grounded answer example",
            )
        )
    if len(refusal_examples) < min_refusal_examples:
        errors.append(
            GoldenDatasetQualityIssue(
                field="DATASET",
                message=(
                    f"include at least {min_refusal_examples} unsupported-question "
                    "refusal example"
                ),
            )
        )

    for index, example in enumerate(examples):
        label = _example_label(example, index)
        example_id = example.id.strip()
        if not example_id:
            errors.append(
                GoldenDatasetQualityIssue(
                    field=f"DATASET[{index}].id",
                    message="example id must be set",
                )
            )
        elif example_id in seen_ids:
            errors.append(
                GoldenDatasetQualityIssue(
                    field=f"DATASET[{example_id}].id",
                    message="example ids must be unique",
                )
            )
        seen_ids.add(example_id)

        if not example.question.strip():
            errors.append(
                GoldenDatasetQualityIssue(
                    field=f"DATASET[{label}].question",
                    message="question must be set",
                )
            )

        if require_space_id and not _metadata_space_id(example.metadata):
            errors.append(
                GoldenDatasetQualityIssue(
                    field=f"DATASET[{label}].metadata.spaceId",
                    message="business evaluation examples must target a knowledge space",
                )
            )

        if example.expects_refusal:
            _validate_refusal_example(example, label, errors)
        else:
            _validate_grounded_example(example, label, errors)

    return GoldenDatasetQualityReport(
        ready=not errors,
        datasetExamples=len(examples),
        groundedExamples=len(grounded_examples),
        refusalExamples=len(refusal_examples),
        errors=errors,
        warnings=warnings,
    )


def _validate_grounded_example(
    example: GoldenExample,
    label: str,
    errors: list[GoldenDatasetQualityIssue],
) -> None:
    if not _non_empty_values(example.expected_answer_traits):
        errors.append(
            GoldenDatasetQualityIssue(
                field=f"DATASET[{label}].expectedAnswerTraits",
                message="grounded examples must define expected answer traits",
            )
        )
    if not _non_empty_values(example.expected_sources):
        errors.append(
            GoldenDatasetQualityIssue(
                field=f"DATASET[{label}].expectedSources",
                message="grounded examples must define expected sources",
            )
        )


def _validate_refusal_example(
    example: GoldenExample,
    label: str,
    errors: list[GoldenDatasetQualityIssue],
) -> None:
    if _non_empty_values(example.expected_answer_traits):
        errors.append(
            GoldenDatasetQualityIssue(
                field=f"DATASET[{label}].expectedAnswerTraits",
                message="refusal examples must not define answer traits",
            )
        )
    if _non_empty_values(example.expected_sources):
        errors.append(
            GoldenDatasetQualityIssue(
                field=f"DATASET[{label}].expectedSources",
                message="refusal examples must not define expected sources",
            )
        )


def _example_label(example: GoldenExample, index: int) -> str:
    return example.id.strip() or str(index)


def _metadata_space_id(metadata: dict[str, Any]) -> str:
    value = metadata.get("spaceId", metadata.get("space_id", ""))
    return str(value).strip()


def _non_empty_values(values: Sequence[str]) -> list[str]:
    return [value.strip() for value in values if value.strip()]
