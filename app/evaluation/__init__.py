"""Evaluation primitives for the RAG foundation."""

from app.evaluation.datasets import load_golden_dataset
from app.evaluation.metrics import evaluate_results
from app.evaluation.judges import (
    FaithfulnessJudge,
    ModelFaithfulnessJudge,
    StaticFaithfulnessJudge,
)
from app.evaluation.http import HttpRagEvaluationClient, run_http_evaluation
from app.evaluation.models import (
    AgentRunResult,
    EvaluationMetrics,
    EvaluationReport,
    FaithfulnessVerdict,
    GoldenExample,
)
from app.evaluation.fake import run_fake_evaluation
from app.evaluation.service import TracedRagService, run_service_evaluation

__all__ = [
    "AgentRunResult",
    "EvaluationMetrics",
    "EvaluationReport",
    "FaithfulnessJudge",
    "FaithfulnessVerdict",
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
]
