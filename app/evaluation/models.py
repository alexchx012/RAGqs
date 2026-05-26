"""Typed models for local RAG evaluation."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class GoldenExample(BaseModel):
    """One row in a golden evaluation dataset."""

    model_config = ConfigDict(populate_by_name=True)

    id: str
    question: str
    expected_answer_traits: list[str] = Field(
        default_factory=list,
        alias="expectedAnswerTraits",
    )
    expected_sources: list[str] = Field(default_factory=list, alias="expectedSources")
    expects_refusal: bool = Field(default=False, alias="expectsRefusal")
    metadata: dict[str, Any] = Field(default_factory=dict)


class AgentRunResult(BaseModel):
    """Provider-neutral result captured from an agent run."""

    model_config = ConfigDict(populate_by_name=True)

    example_id: str = Field(alias="exampleId")
    answer: str = ""
    retrieved_sources: list[dict[str, Any]] = Field(
        default_factory=list,
        alias="retrievedSources",
    )
    cited_sources: list[dict[str, Any]] = Field(default_factory=list, alias="citedSources")
    latency_ms: float = Field(default=0.0, alias="latencyMs")
    refused: bool = False
    faithfulness: FaithfulnessVerdict | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class FaithfulnessVerdict(BaseModel):
    """Groundedness judgment for one answer."""

    score: float
    passed: bool
    rationale: str = ""


class EvaluationMetrics(BaseModel):
    """Aggregate metrics for a dataset run."""

    model_config = ConfigDict(populate_by_name=True)

    total_examples: int = Field(alias="totalExamples")
    retrieval_hit_rate: float = Field(alias="retrievalHitRate")
    answer_trait_coverage: float = Field(alias="answerTraitCoverage")
    answer_faithfulness: float = Field(alias="answerFaithfulness")
    citation_accuracy: float = Field(alias="citationAccuracy")
    no_answer_refusal_rate: float = Field(alias="noAnswerRefusalRate")
    average_latency_ms: float = Field(alias="averageLatencyMs")


class EvaluationReport(BaseModel):
    """Full evaluation output with aggregate metrics and failures."""

    model_config = ConfigDict(populate_by_name=True)

    status: str = "completed"
    quality_conclusion: str = Field(
        default="not_real_quality_validated",
        alias="qualityConclusion",
    )
    limitations: list[str] = Field(
        default_factory=lambda: [
            "fake/local evaluation is a software-interface check only",
            "real business answer quality has not been validated",
        ]
    )
    metrics: EvaluationMetrics
    failures: list[dict[str, Any]] = Field(default_factory=list)
