"""Evaluation primitives for the RAG foundation."""

from app.evaluation.dataset_quality import (
    GoldenDatasetQualityIssue,
    GoldenDatasetQualityReport,
    validate_business_golden_dataset,
)
from app.evaluation.datasets import load_golden_dataset
from app.evaluation.fake import run_fake_evaluation
from app.evaluation.http import HttpRagEvaluationClient, run_http_evaluation
from app.evaluation.judges import (
    FaithfulnessJudge,
    ModelFaithfulnessJudge,
    StaticFaithfulnessJudge,
)
from app.evaluation.metrics import evaluate_results
from app.evaluation.models import (
    AgentRunResult,
    EvaluationMetrics,
    EvaluationReport,
    FaithfulnessVerdict,
    GoldenExample,
)
from app.evaluation.readiness import (
    EvaluationReadinessIssue,
    EvaluationReadinessReport,
    validate_real_evaluation_readiness,
)
from app.evaluation.service import TracedRagService, run_service_evaluation

__all__ = [
    "AgentRunResult",
    "EvaluationMetrics",
    "EvaluationReadinessIssue",
    "EvaluationReadinessReport",
    "EvaluationReport",
    "FaithfulnessJudge",
    "FaithfulnessVerdict",
    "GoldenDatasetQualityIssue",
    "GoldenDatasetQualityReport",
    "GoldenExample",
    "HttpRagEvaluationClient",
    "ModelFaithfulnessJudge",
    "StaticFaithfulnessJudge",
    "TracedRagService",
    "evaluate_results",
    "load_golden_dataset",
    "run_fake_evaluation",
    "run_http_evaluation",
    "run_service_evaluation",
    "validate_business_golden_dataset",
    "validate_real_evaluation_readiness",
]
