"""Dataset, case and observation schemas."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Split = Literal["development", "validation", "test"]
METHODS = (
    "schema_validation",
    "exact_match",
    "numeric_tolerance",
    "tool_correctness",
    "source_citation_check",
    "task_completion",
    "llm_judge",
)


class Case(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    case_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,119}$")
    task: str = Field(min_length=1, max_length=8000)
    input: dict[str, Any] = Field(default_factory=dict)
    input_artifacts: list[str] = Field(default_factory=list)
    expected_output: dict[str, Any]
    evaluation_methods: list[str] = Field(min_length=1)
    evaluator_config: dict[str, dict[str, Any]] = Field(default_factory=dict)
    tags: list[str] = Field(default_factory=list)

    @field_validator("evaluation_methods")
    @classmethod
    def _known(cls, v: list[str]) -> list[str]:
        unknown = [m for m in v if m not in METHODS]
        if unknown:
            raise ValueError(f"unknown evaluation methods: {unknown}")
        if len(set(v)) != len(v):
            raise ValueError("duplicate evaluation methods")
        return v

    @model_validator(mode="after")
    def _config_matches(self) -> Case:
        extra = set(self.evaluator_config) - set(self.evaluation_methods)
        if extra:
            raise ValueError(f"evaluator_config for methods not listed: {sorted(extra)}")
        if self.evaluation_methods == ["llm_judge"]:
            raise ValueError("a case needs at least one deterministic evaluation method")
        return self


class Dataset(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    name: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$")
    version: int = Field(ge=1)
    split: Split
    description: str = ""
    cases: list[Case] = Field(min_length=1, max_length=5000)

    @model_validator(mode="after")
    def _unique_ids(self) -> Dataset:
        ids = [c.case_id for c in self.cases]
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate case_id in dataset")
        return self

    @property
    def visibility(self) -> str:
        """Optimizers may see development cases; validation/test answers stay hidden."""
        return "visible" if self.split == "development" else "hidden"

    def content_hash(self) -> str:
        return dataset_hash(self)


def dataset_hash(ds: Dataset) -> str:
    """Hash of the dataset identity and cases (order-independent)."""
    body = {
        "name": ds.name,
        "version": ds.version,
        "split": ds.split,
        "cases": sorted((c.model_dump(mode="json") for c in ds.cases), key=lambda c: c["case_id"]),
    }
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class ToolCallRecord(BaseModel):
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    status: str  # succeeded | failed | denied | rejected | pending_approval | ...
    error_type: str | None = None


class Observation(BaseModel):
    """Everything the evaluators may look at for one execution of one case."""

    output: dict[str, Any] | None = None
    tool_calls: list[ToolCallRecord] = Field(default_factory=list)
    # citation key -> source text the agent actually retrieved during this execution
    sources: dict[str, str] = Field(default_factory=dict)
    error: dict[str, Any] | None = None
    latency_ms: float | None = None
    cost_usd: float | None = None
    retries: int = 0
