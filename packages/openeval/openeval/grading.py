from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from openeval.evaluators import EVALUATORS, EvaluatorResult
from openeval.judge import Judge, llm_judge
from openeval.schema import Case, Observation


@dataclass
class CaseGrade:
    case_id: str
    passed: bool
    results: list[EvaluatorResult]

    def by_method(self) -> dict[str, EvaluatorResult]:
        return {r.method: r for r in self.results}

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "passed": self.passed,
            "results": {r.method: r.to_dict() for r in self.results},
        }


def grade_case(case: Case, obs: Observation, *, judge: Judge | None = None) -> CaseGrade:
    """A case passes only if every deterministic (non-advisory) evaluator passes."""
    results: list[EvaluatorResult] = []
    for method in case.evaluation_methods:
        cfg = case.evaluator_config.get(method, {})
        if method == "llm_judge":
            results.append(llm_judge(case, obs, cfg, judge))
        else:
            results.append(EVALUATORS[method](case, obs, cfg))
    deterministic = [r for r in results if not r.advisory]
    return CaseGrade(case.case_id, bool(deterministic) and all(r.passed for r in deterministic), results)
