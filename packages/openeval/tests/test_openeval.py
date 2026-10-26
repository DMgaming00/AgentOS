import csv
import io
import json

import pytest

from openeval import (
    Case,
    CaseRecord,
    Dataset,
    Observation,
    ToolCallRecord,
    aggregate,
    compare,
    dataset_hash,
    export_csv,
    export_json,
    export_markdown,
    grade_case,
    mcnemar_exact,
    paired_bootstrap,
)
from openeval.judge import JudgeVerdict

CASE = Case(
    case_id="case-001",
    task="Extract the p95 latency and output price of Helix Serve.",
    input_artifacts=["platform-helix.md"],
    expected_output={"p95_latency_ms": 300, "price_per_1m_output_tokens_usd": 0.95},
    evaluation_methods=[
        "schema_validation",
        "numeric_tolerance",
        "source_citation_check",
        "tool_correctness",
        "task_completion",
    ],
    evaluator_config={
        "numeric_tolerance": {
            "fields": {"p95_latency_ms": {"abs": 0.5}, "price_per_1m_output_tokens_usd": {"abs": 0.001}}
        },
        "tool_correctness": {"required_tools": ["document_search"], "max_calls": 4},
    },
    tags=["numerical_extraction"],
)
SOURCES = {
    "platform-helix.md#Helix Serve > Performance": "- p95 latency: 300 ms",
    "platform-helix.md#Helix Serve > Pricing": "- Price per 1M output tokens: $0.95 USD",
}
SEARCH = ToolCallRecord(name="document_search", arguments={"query": "helix"}, status="succeeded")


def good_obs(**kw):
    out = {
        "p95_latency_ms": 300,
        "price_per_1m_output_tokens_usd": 0.95,
        "citations": {
            "p95_latency_ms": "platform-helix.md#Helix Serve > Performance",
            "price_per_1m_output_tokens_usd": "platform-helix.md#Helix Serve > Pricing",
        },
    }
    return Observation(**{"output": out, "tool_calls": [SEARCH], "sources": SOURCES, **kw})


def test_correct_answer_passes_every_method():
    g = grade_case(CASE, good_obs())
    assert g.passed and all(r.passed for r in g.results)


def test_string_number_fails_schema_and_numeric_but_not_completion():
    obs = good_obs()
    obs.output["price_per_1m_output_tokens_usd"] = "$0.95"
    g = grade_case(CASE, obs).by_method()
    assert (
        not g["schema_validation"].passed
        and "is not of type 'number'" in g["schema_validation"].details["failures"][0]
    )
    assert not g["numeric_tolerance"].passed and g["task_completion"].passed
    assert not grade_case(CASE, obs).passed


def test_numeric_tolerance_bounds_and_null_expected():
    obs = good_obs()
    obs.output["p95_latency_ms"] = 300.4
    assert grade_case(CASE, obs).by_method()["numeric_tolerance"].passed
    obs.output["p95_latency_ms"] = 301
    assert not grade_case(CASE, obs).by_method()["numeric_tolerance"].passed
    obs.output["p95_latency_ms"] = True  # bools are not numbers
    assert not grade_case(CASE, obs).by_method()["numeric_tolerance"].passed
    missing = Case(
        case_id="m",
        task="t",
        expected_output={"cold_start_s": None},
        evaluation_methods=["numeric_tolerance"],
    )
    assert grade_case(missing, Observation(output={"cold_start_s": None})).passed
    assert not grade_case(missing, Observation(output={"cold_start_s": 9})).passed  # hallucinated value


def test_grounding_requires_retrieved_and_supporting_sources():
    obs = good_obs()
    obs.output["citations"]["p95_latency_ms"] = "platform-helix.md#Made Up"
    g = grade_case(CASE, obs).by_method()["source_citation_check"]
    assert not g.passed and "was not retrieved" in g.details["failures"][0]
    obs = good_obs(sources={k: "nothing relevant" for k in SOURCES})
    assert (
        "does not appear" in grade_case(CASE, obs).by_method()["source_citation_check"].details["failures"][0]
    )
    obs = good_obs()
    del obs.output["citations"]
    assert not grade_case(CASE, obs).by_method()["source_citation_check"].passed


def test_tool_correctness_rules():
    def tc(calls, **cfg):
        case = CASE.model_copy(
            update={"evaluator_config": {**CASE.evaluator_config, "tool_correctness": cfg}}
        )
        return grade_case(case, good_obs().model_copy(update={"tool_calls": calls})).by_method()[
            "tool_correctness"
        ]

    assert not tc([], required_tools=["document_search"]).passed
    assert not tc([SEARCH, ToolCallRecord(name="artifact_writer", status="denied")]).passed
    assert not tc(
        [ToolCallRecord(name="calculator", status="failed", error_type="invalid_tool_arguments")]
    ).passed
    assert not tc([SEARCH] * 3, max_identical_calls=2).passed
    assert not tc([SEARCH, SEARCH], max_calls=1).passed
    assert not tc(
        [SEARCH, ToolCallRecord(name="shell", status="succeeded")], forbidden_tools=["shell"]
    ).passed
    assert tc([SEARCH, SEARCH], required_tools=["document_search"]).passed


def test_failed_execution_fails_completion_and_schema():
    g = grade_case(CASE, Observation(output=None, error={"type": "invalid_model_output"}))
    assert not g.passed
    assert "invalid_model_output" in g.by_method()["task_completion"].details["failures"][0]


def test_exact_match_normalization():
    case = Case(
        case_id="e",
        task="t",
        expected_output={"vendor": "Helix Serve"},
        evaluation_methods=["exact_match"],
        evaluator_config={"exact_match": {"normalize": "casefold"}},
    )
    assert grade_case(case, Observation(output={"vendor": "  helix   serve "})).passed
    assert not grade_case(case, Observation(output={"vendor": "Helix"})).passed


def test_llm_judge_is_advisory_and_needs_calibration():
    class Harsh:
        def judge(self, rubric, examples, case, obs):
            return JudgeVerdict(score=0.0, rationale="bad", uncertainty=0.4)

    case = CASE.model_copy(
        update={
            "evaluation_methods": [*CASE.evaluation_methods, "llm_judge"],
            "evaluator_config": {
                **CASE.evaluator_config,
                "llm_judge": {"rubric": "clarity", "examples": [{"a": 1}, {"b": 2}]},
            },
        }
    )
    g = grade_case(case, good_obs(), judge=Harsh())
    judge = g.by_method()["llm_judge"]
    assert judge.advisory and not judge.passed and judge.details["uncertainty"] == 0.4
    assert g.passed  # the judge cannot fail a case that deterministic checks pass
    uncalibrated = case.model_copy(
        update={"evaluator_config": {**case.evaluator_config, "llm_judge": {"rubric": "x"}}}
    )
    assert (
        "calibration"
        in grade_case(uncalibrated, good_obs(), judge=Harsh()).by_method()["llm_judge"].details["skipped"]
    )
    with pytest.raises(ValueError):
        Case(case_id="j", task="t", expected_output={}, evaluation_methods=["llm_judge"])


def test_dataset_validation_and_hash():
    ds = Dataset(name="numeric", version=1, split="validation", cases=[CASE])
    assert (
        ds.visibility == "hidden"
        and Dataset(name="n", version=1, split="development", cases=[CASE]).visibility == "visible"
    )
    reordered = Dataset(name="numeric", version=1, split="validation", cases=[CASE])
    assert dataset_hash(ds) == dataset_hash(reordered) == ds.content_hash()
    changed = Dataset(
        name="numeric",
        version=1,
        split="validation",
        cases=[CASE.model_copy(update={"expected_output": {"p95_latency_ms": 301}})],
    )
    assert dataset_hash(changed) != dataset_hash(ds)
    with pytest.raises(ValueError):
        Dataset(name="d", version=1, split="test", cases=[CASE, CASE])
    with pytest.raises(ValueError):
        Case(case_id="x", task="t", expected_output={}, evaluation_methods=["vibes"])
    with pytest.raises(ValueError):
        Case(
            case_id="x",
            task="t",
            expected_output={},
            evaluation_methods=["exact_match"],
            evaluator_config={"numeric_tolerance": {}},
        )


def test_metrics_denominators_and_cost_per_success():
    recs = [
        CaseRecord("a", 1, True, {"schema_validation": True}, cost_usd=0.02, latency_ms=100),
        CaseRecord("b", 1, False, {"schema_validation": False}, cost_usd=0.02, latency_ms=300),
        CaseRecord("c", 1, False, {}, errored=True, error_category="provider_timeout", retries=2),
    ]
    m = aggregate(recs)
    assert m["task_success_rate"] == {"value": 1 / 3, "numerator": 1, "denominator": 3}
    assert m["schema_compliance"]["denominator"] == 2  # only cases where it was evaluated
    assert m["cost_per_successful_task_usd"] == pytest.approx(0.04)
    assert m["latency_ms"]["p95"] == 300 and m["latency_ms"]["measured"] == 2
    assert m["failure_rate"]["numerator"] == 1 and m["retry_rate"]["numerator"] == 1
    assert m["failure_breakdown"] == {"evaluation_failed": 1, "provider_timeout": 1}
    none = aggregate([CaseRecord("a", 1, False, cost_usd=0.1)])
    assert none["cost_per_successful_task_usd"] is None  # undefined, not $0
    assert aggregate([])["task_success_rate"]["value"] is None


def test_bootstrap_and_mcnemar():
    lo_hi = paired_bootstrap([1.0] * 12)
    assert lo_hi == (1.0, 1.0, 1.0)
    mean, lo, hi = paired_bootstrap([1, 0, 0, 1, -1, 0, 1, 0, 0, 1, 0, 1])
    assert lo <= mean <= hi and paired_bootstrap([1, 0, -1], seed=7) == paired_bootstrap([1, 0, -1], seed=7)
    assert mcnemar_exact(0, 0) is None
    assert mcnemar_exact(0, 6) == pytest.approx(2 / 64)
    assert mcnemar_exact(3, 3) == 1.0


def records(pattern):
    return [CaseRecord(f"c{i}", 1, bool(p)) for i, p in enumerate(pattern)]


def test_compare_requires_sample_size_and_ci_for_significance():
    base, cand = records([0] * 12), records([1] * 12)
    c = compare(base, cand, min_n=10)
    assert c.significant and c.ci[0] > 0 and c.improvements and not c.regressions
    small = compare(records([0] * 4), records([1] * 4), min_n=10)
    assert not small.significant and "uncertainty is too high" in small.interpretation
    noisy = compare(records([1, 0] * 6), records([0, 1] * 6))
    assert not noisy.significant and len(noisy.regressions) == 6 and "includes 0" in noisy.interpretation
    worse = compare(records([1] * 12), records([0] * 12))
    assert worse.significant and worse.ci[1] < 0 and "lower" in worse.interpretation
    unmatched = compare(records([1, 1]), [CaseRecord("zzz", 1, True)])
    assert unmatched.n_matched == 0 and len(unmatched.unmatched) == 3


def test_trials_are_averaged_per_case():
    base = [CaseRecord("a", 1, True), CaseRecord("a", 2, False)]
    cand = [CaseRecord("a", 1, True), CaseRecord("a", 2, True)]
    c = compare(base, cand, min_n=1)
    assert c.per_case == [{"case_id": "a", "baseline": 0.5, "candidate": 1.0, "diff": 0.5}]


def test_exports_include_methodology_and_dataset():
    report = {
        "dataset": {"name": "numeric", "split": "validation", "version": 1, "hash": "abc"},
        "conditions": {
            "agent_name": "R",
            "agent_version": 2,
            "provider": "mock",
            "model": "mock-x",
            "simulated": True,
            "trials": 1,
            "config_hash": "f" * 64,
        },
        "metrics": aggregate(records([1, 0])),
        "results": [
            {"case_key": "c0", "attempt": 1, "passed": True, "scores": {"exact_match": {"passed": True}}},
            {
                "case_key": "c1",
                "attempt": 1,
                "passed": False,
                "scores": {"exact_match": {"passed": False}},
                "error": {"type": "x"},
            },
        ],
    }
    j = json.loads(export_json(report))
    assert j["methodology"] and j["dataset"]["hash"] == "abc"
    rows = list(csv.reader(io.StringIO(export_csv(report))))
    assert "dataset=numeric" in rows[0][0] and "methodology=" in rows[0][0] and rows[2][0] == "c0"
    md = export_markdown(report)
    assert "SIMULATED" in md and "## Methodology" in md and "50.0% (1/2)" in md
