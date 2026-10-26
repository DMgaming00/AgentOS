"""Deterministic evaluators. Each returns an EvaluatorResult; none of them call a model."""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, TypeGuard

import jsonschema

from openeval.schema import Case, Observation


@dataclass
class EvaluatorResult:
    method: str
    passed: bool
    score: float
    details: dict[str, Any] = field(default_factory=dict)
    advisory: bool = False  # advisory results never affect pass/fail

    def to_dict(self) -> dict[str, Any]:
        return {
            "method": self.method,
            "passed": self.passed,
            "score": self.score,
            "details": self.details,
            "advisory": self.advisory,
        }


def _result(method: str, failures: list[str], checked: int, **details: Any) -> EvaluatorResult:
    score = 1.0 if checked == 0 else (checked - len(failures)) / checked
    return EvaluatorResult(
        method, not failures, round(score, 4), {"failures": failures, "checked": checked, **details}
    )


def _json_type(value: Any) -> Any:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int | float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    return "object"


def derived_schema(expected: dict[str, Any]) -> dict[str, Any]:
    """Schema implied by the expected output: same keys, same JSON types (numbers must be numbers)."""
    return {
        "type": "object",
        "required": sorted(expected),
        "properties": {k: {"type": _json_type(v)} for k, v in expected.items()},
    }


def schema_validation(case: Case, obs: Observation, cfg: dict[str, Any]) -> EvaluatorResult:
    if obs.output is None:
        return _result("schema_validation", ["no structured output"], 1)
    schema = cfg.get("schema") or derived_schema(case.expected_output)
    validator = jsonschema.Draft202012Validator(schema)
    errors = sorted(validator.iter_errors(obs.output), key=lambda e: list(e.path))
    failures = [f"{'.'.join(map(str, e.path)) or '<root>'}: {e.message}" for e in errors[:20]]
    return _result("schema_validation", failures, max(1, len(schema.get("properties", {})) or 1))


def _norm(v: Any, mode: str) -> Any:
    if isinstance(v, str):
        v = " ".join(v.split())
        return v.casefold() if mode == "casefold" else v
    return v


def exact_match(case: Case, obs: Observation, cfg: dict[str, Any]) -> EvaluatorResult:
    fields = cfg.get("fields") or [
        k for k, v in case.expected_output.items() if not isinstance(v, int | float) or isinstance(v, bool)
    ]
    if obs.output is None:
        return _result("exact_match", ["no structured output"], max(1, len(fields)))
    mode = cfg.get("normalize", "whitespace")
    failures = [
        f"{f}: expected {case.expected_output.get(f)!r}, got {obs.output.get(f)!r}"
        for f in fields
        if _norm(obs.output.get(f), mode) != _norm(case.expected_output.get(f), mode)
    ]
    return _result("exact_match", failures, len(fields))


def _is_number(v: Any) -> TypeGuard[int | float]:
    return isinstance(v, int | float) and not isinstance(v, bool) and math.isfinite(v)


def numeric_tolerance(case: Case, obs: Observation, cfg: dict[str, Any]) -> EvaluatorResult:
    spec: dict[str, dict[str, float]] = cfg.get("fields") or {
        k: {} for k, v in case.expected_output.items() if _is_number(v) or v is None
    }
    if obs.output is None:
        return _result("numeric_tolerance", ["no structured output"], max(1, len(spec)))
    failures = []
    for f, tol in spec.items():
        expected, got = case.expected_output.get(f), obs.output.get(f)
        if expected is None:
            if got is not None:
                failures.append(f"{f}: expected null (not in sources), got {got!r}")
            continue
        if not _is_number(got):
            failures.append(f"{f}: expected a number, got {type(got).__name__} {got!r}")
            continue
        allowed = max(float(tol.get("abs", 1e-9)), float(tol.get("rel", 0.0)) * abs(expected))
        if abs(got - expected) > allowed:
            failures.append(f"{f}: |{got} - {expected}| > {allowed}")
    return _result("numeric_tolerance", failures, len(spec))


def tool_correctness(case: Case, obs: Observation, cfg: dict[str, Any]) -> EvaluatorResult:
    """Required tools used successfully; no forbidden, denied or malformed calls; no call spam."""
    failures: list[str] = []
    calls = obs.tool_calls
    ok = Counter(c.name for c in calls if c.status == "succeeded")
    for t in cfg.get("required_tools", []):
        if not ok[t]:
            failures.append(f"required tool {t!r} was not called successfully")
    for c in calls:
        if c.name in cfg.get("forbidden_tools", []):
            failures.append(f"forbidden tool {c.name!r} was called")
        if c.status == "denied":
            failures.append(f"call to {c.name!r} was denied by policy")
        elif c.status == "failed" and c.error_type == "invalid_tool_arguments":
            failures.append(f"call to {c.name!r} had invalid arguments")
    if "max_calls" in cfg and len(calls) > int(cfg["max_calls"]):
        failures.append(f"{len(calls)} tool calls > max {cfg['max_calls']}")
    signature = Counter((c.name, repr(sorted(c.arguments.items()))) for c in calls)
    worst = max(signature.values(), default=0)
    if worst > int(cfg.get("max_identical_calls", 2)):
        failures.append(f"identical tool call repeated {worst} times")
    checks = len(cfg.get("required_tools", [])) + len(calls) + 2
    return _result("tool_correctness", failures, checks, tool_calls=len(calls))


_NUM = re.compile(r"-?\d[\d,]*(?:\.\d+)?")


def _numbers_in(text: str) -> list[float]:
    return [float(m.replace(",", "")) for m in _NUM.findall(text)]


def source_citation_check(case: Case, obs: Observation, cfg: dict[str, Any]) -> EvaluatorResult:
    """Each reported field cites a source the agent actually retrieved, and that source supports the value."""
    fields = cfg.get("fields") or [k for k, v in case.expected_output.items() if v is not None]
    citation_field = cfg.get("citation_field", "citations")
    if obs.output is None:
        return _result("source_citation_check", ["no structured output"], max(1, len(fields)))
    citations = obs.output.get(citation_field) or {}
    if not isinstance(citations, dict):
        return _result("source_citation_check", [f"{citation_field} must be an object"], max(1, len(fields)))
    failures = []
    for f in fields:
        cite = citations.get(f)
        if not cite:
            failures.append(f"{f}: no citation")
            continue
        text = obs.sources.get(cite)
        if text is None:
            failures.append(f"{f}: cited {cite!r} was not retrieved during this execution")
            continue
        value = obs.output.get(f)
        if _is_number(value):
            if not any(math.isclose(value, n, rel_tol=1e-6, abs_tol=1e-9) for n in _numbers_in(text)):
                failures.append(f"{f}: value {value} does not appear in cited source {cite!r}")
        elif isinstance(value, str) and value.casefold() not in text.casefold():
            failures.append(f"{f}: value {value!r} does not appear in cited source {cite!r}")
    return _result("source_citation_check", failures, len(fields))


def task_completion(case: Case, obs: Observation, cfg: dict[str, Any]) -> EvaluatorResult:
    required = cfg.get("required_fields") or list(case.expected_output)
    if obs.error is not None or obs.output is None:
        reason = (obs.error or {}).get("type", "no output")
        return _result("task_completion", [f"execution did not complete: {reason}"], max(1, len(required)))
    failures = [f"missing required field {f!r}" for f in required if f not in obs.output]
    return _result("task_completion", failures, len(required))


Evaluator = Callable[[Case, Observation, dict[str, Any]], EvaluatorResult]
EVALUATORS: dict[str, Evaluator] = {
    "schema_validation": schema_validation,
    "exact_match": exact_match,
    "numeric_tolerance": numeric_tolerance,
    "tool_correctness": tool_correctness,
    "source_citation_check": source_citation_check,
    "task_completion": task_completion,
}
