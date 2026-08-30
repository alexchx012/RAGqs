"""Agents orchestration capability: generation-side self-evaluation.

The agents layer owns the query control-flow steps that are not already
supplied by a sibling capability: the think/deep self-evaluation and bounded
rewrite/re-retrieve loop declared in 《后端设计》§7.3 and 未实现能力表 I.
Effort-tier budgets, the BudgetMeter consumption port and tree-candidate
selection are implemented in ``app/chat/budget.py`` on top of the
usage-metering capability; the evaluation judge belongs to the evaluation
capability and is consumed through the port declared here.
"""

from .selfeval import (
    AcceptingSelfEvaluationPort,
    DeepRetrievalStrategyPlan,
    HeuristicSelfEvaluationPort,
    SelfEvaluationPort,
    SelfEvaluationResult,
)

__all__ = [
    "AcceptingSelfEvaluationPort",
    "DeepRetrievalStrategyPlan",
    "HeuristicSelfEvaluationPort",
    "SelfEvaluationPort",
    "SelfEvaluationResult",
]
