"""Chaos Lab pure logic: reproducible schedules, plan validation, scorecard definitions."""

import statistics

import pytest
from pydantic import ValidationError

from agentos_core.chaos.context import roll
from agentos_core.chaos.faults import CATALOG, PRESETS, ChaosPlan, FaultSpec, FaultType, Point
from agentos_core.chaos.scorecard import METRIC_DEFINITIONS, rate


def test_roll_is_a_pure_function_of_the_schedule_coordinates():
    a = roll(7, 3, "research", "provider_timeout", "before_model_call", 1, "0:call0")
    assert a == roll(7, 3, "research", "provider_timeout", "before_model_call", 1, "0:call0")
    others = [
        roll(8, 3, "research", "provider_timeout", "before_model_call", 1, "0:call0"),  # seed
        roll(7, 4, "research", "provider_timeout", "before_model_call", 1, "0:call0"),  # case
        roll(7, 3, "analyze", "provider_timeout", "before_model_call", 1, "0:call0"),  # step
        roll(
            7, 3, "research", "provider_timeout", "before_model_call", 2, "0:call0"
        ),  # attempt (a retry re-rolls)
        roll(7, 3, "research", "provider_timeout", "before_model_call", 1, "0:call1"),  # call
    ]
    assert all(0 <= x < 1 for x in [a, *others]) and len({a, *others}) == 6


def test_roll_is_uniform_enough_to_honor_probabilities():
    rolls = [roll(1, i, "n", "f", "p", 1, "0:") for i in range(4000)]
    assert abs(statistics.fmean(rolls) - 0.5) < 0.03
    assert abs(sum(r < 0.3 for r in rolls) / len(rolls) - 0.3) < 0.03


def test_every_fault_has_a_documented_expectation_and_valid_default_point():
    assert set(CATALOG) == set(FaultType)
    for t, meta in CATALOG.items():
        assert meta["default"] in meta["points"] and meta["what"] and meta["expect"], t
        FaultSpec(type=t)  # default point validates


@pytest.mark.parametrize(
    "bad",
    [
        {"type": "tool_timeout", "point": "after_tool"},
        {"type": "worker_crash", "point": "run_creation"},
        {"type": "provider_timeout", "probability": 1.5},
        {"type": "tool_failure", "max_per_step": 0},
        {"type": "not_a_fault"},
    ],
)
def test_invalid_fault_specs_are_rejected(bad):
    with pytest.raises(ValidationError):
        FaultSpec.model_validate(bad)


def test_plan_bounds_and_presets():
    with pytest.raises(ValidationError):
        ChaosPlan.model_validate({"faults": []})
    with pytest.raises(ValidationError):
        ChaosPlan.model_validate({"faults": [{"type": "tool_failure"}], "trials": 41})
    with pytest.raises(ValidationError):
        ChaosPlan.model_validate({"faults": [{"type": "tool_failure"}], "safety_budget_usd": 26})
    capped = ChaosPlan.model_validate(
        {"faults": [{"type": "tool_failure"}], "trials": 5, "max_affected_runs": 40}
    )
    assert capped.max_affected_runs == 5  # cannot affect more runs than exist
    for name, p in PRESETS.items():
        plan = ChaosPlan.model_validate(p["plan"])
        assert plan.faults and p["description"], name
    assert FaultSpec(type=FaultType.WORKER_CRASH).point == Point.BEFORE_MODEL_CALL


def test_metrics_define_their_denominators_and_there_is_no_composite_score():
    for name, d in METRIC_DEFINITIONS.items():
        assert (
            d["definition"]
            and d["denominator"]
            and d["kind"] in {"outcome", "descriptive", "latency", "safety"}
        ), name
    assert not any("score" in k and "composite" in k for k in METRIC_DEFINITIONS)
    assert rate(3, 4) == {"value": 0.75, "numerator": 3, "denominator": 4}
    assert rate(0, 0)["value"] is None  # an undefined rate is None, not 0


def test_affected_cases_are_chosen_from_the_seed_alone():
    p = ChaosPlan.model_validate(
        {"faults": [{"type": "tool_failure"}], "trials": 20, "max_affected_runs": 6, "seed": 5}
    )
    a = p.affected_cases()
    assert len(a) == 6 and a == p.affected_cases() and a <= set(range(20))
    assert a != ChaosPlan.model_validate({**p.model_dump(), "seed": 6}).affected_cases()
    # the same schedule regardless of every other plan field
    q = ChaosPlan.model_validate(
        {**p.model_dump(), "faults": [{"type": "provider_timeout"}], "duration_seconds": 30}
    )
    assert q.affected_cases() == a
