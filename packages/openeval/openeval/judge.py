"""Optional LLM-as-judge for subjective criteria. Always advisory.

A judge must return a score, a rubric-anchored rationale and its own uncertainty.
Results are recorded but never change a case's pass/fail, which is decided by
deterministic evaluators only.
"""

from __future__ import annotations

from typing import Any, Protocol

from pydantic import BaseModel, Field

from openeval.evaluators import EvaluatorResult
from openeval.schema import Case, Observation


class JudgeVerdict(BaseModel):
    score: float = Field(ge=0, le=1)
    rationale: str = Field(max_length=4000)
    uncertainty: float = Field(ge=0, le=1, description="judge-reported uncertainty; 1 = no confidence")


class Judge(Protocol):
    def judge(
        self, rubric: str, examples: list[dict[str, Any]], case: Case, obs: Observation
    ) -> JudgeVerdict: ...


def llm_judge(case: Case, obs: Observation, cfg: dict[str, Any], judge: Judge | None) -> EvaluatorResult:
    if judge is None:
        return EvaluatorResult("llm_judge", True, 0.0, {"skipped": "no judge configured"}, advisory=True)
    if len(cfg.get("examples", [])) < 2:
        return EvaluatorResult(
            "llm_judge", True, 0.0, {"skipped": "rubric needs at least 2 calibration examples"}, advisory=True
        )
    verdict = judge.judge(cfg["rubric"], cfg["examples"], case, obs)
    return EvaluatorResult(
        "llm_judge",
        verdict.score >= float(cfg.get("threshold", 0.5)),
        verdict.score,
        {"rationale": verdict.rationale, "uncertainty": verdict.uncertainty},
        advisory=True,
    )
