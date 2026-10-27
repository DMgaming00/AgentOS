"""Agent Arena without a database: corpus integrity and leakage, answer keys, leaderboard statistics, policy flags."""

import importlib.util
import json
import re
import uuid
from pathlib import Path

import pytest

import openeval
from agentos_core.arena import leaderboard as lb
from agentos_core.arena.corpus import ARENA_DIR, FAMILIES, load_datasets, load_manifest
from agentos_core.providers.arena_policy import flags, significant

ROOT = Path(__file__).resolve().parents[3]
DOCS = ROOT / "examples" / "research-report" / "documents"


def builder():
    spec = importlib.util.spec_from_file_location(
        "build_arena_corpus", ROOT / "scripts" / "build_arena_corpus.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_committed_corpus_is_exactly_what_the_builder_produces():
    files, manifest = builder().build()
    for name, text in files.items():
        assert (ARENA_DIR / "datasets" / name).read_text() == text, name
    assert json.loads((ARENA_DIR / "MANIFEST.json").read_text()) == manifest


def test_manifest_hashes_match_openeval_and_sizes():
    manifest = load_manifest()
    assert manifest["total_cases"] >= 100
    seen = set()
    for entry, ds in load_datasets():  # raises on any hash mismatch
        assert ds.content_hash() == entry["hash"] and len(ds.cases) == entry["cases"]
        seen.add((entry["family"], entry["split"]))
    assert seen == {(f, s) for f in FAMILIES for s in ("development", "validation", "test")}


def test_no_leakage_between_splits():
    by_split: dict[str, set[str]] = {}
    ids: set[str] = set()
    for entry, ds in load_datasets():
        for c in ds.cases:
            assert c.case_id not in ids
            ids.add(c.case_id)
            key = json.dumps([entry["family"], c.input], sort_keys=True)
            for other_split, keys in by_split.items():
                if other_split != entry["split"]:
                    assert key not in keys, "identical case in two splits"
            by_split.setdefault(entry["split"], set()).add(key)
    assert load_manifest()["leakage"]["identical_case_across_splits"] == 0


def test_expected_answers_never_appear_in_what_the_agent_sees():
    # The agent receives case.task and case.input only. Neither may contain the expected answer for answerable cases.
    for _, ds in load_datasets():
        for c in ds.cases:
            seen = json.dumps({"task": c.task, "input": c.input})
            for key, value in c.expected_output.items():
                if isinstance(value, str):
                    assert value not in seen, (c.case_id, key)
                elif (
                    isinstance(value, float)
                    and not float(value).is_integer()
                    and not c.input.get("operation")
                ):
                    assert f"{value}" not in seen, (c.case_id, key)


def doc_values():
    values: dict[tuple[str, str], list[float]] = {}
    for platform, filename in {
        "Nimbus Inference": "platform-nimbus.md",
        "Helix Serve": "platform-helix.md",
        "Vector Forge": "platform-vectorforge.md",
    }.items():
        for line in (DOCS / filename).read_text().splitlines():
            if line.startswith("- ") and ":" in line:
                label, rest = line[2:].split(":", 1)
                m = re.search(r"-?\d[\d,]*(?:\.\d+)?", rest)
                if m:
                    values.setdefault((platform, label.strip()), []).append(
                        float(m.group(0).replace(",", ""))
                    )
    return values


def test_answer_keys_agree_with_the_source_documents():
    values = doc_values()
    checked = 0
    for entry, ds in load_datasets():
        for c in ds.cases:
            if entry["family"] == "extraction":
                for f in c.input["fields"]:
                    key = (f["subject"], f["label"])
                    expected = next(
                        v for k, v in c.expected_output.items() if k.endswith(f["key"]) or k == f["key"]
                    )
                    if key in values:
                        assert values[key] == [expected] or float(expected) in values[key]
                        assert len(set(values[key])) == 1, "ambiguous fact used"
                    else:
                        assert expected is None
                    checked += 1
            elif entry["family"] == "grounded_qa" and c.input["kind"] in ("lookup", "unanswerable"):
                key = (c.input["subject"], c.input["label"])
                if c.input["kind"] == "unanswerable":
                    assert c.expected_output["answer"] is None and key not in values
                else:
                    assert set(values[key]) == {c.expected_output["answer"]}
                checked += 1
            elif entry["family"] == "grounded_qa":  # comparison: recompute the winner from the documents
                label = c.input["label"]
                holders = {p: v[0] for (p, lab), v in values.items() if lab == label}
                assert all(len(set(v)) == 1 for (_, lab), v in values.items() if lab == label)
                lower = c.input["direction"] == "lower_is_better"
                best = (min if lower else max)(holders, key=holders.__getitem__)
                assert c.expected_output["answer"] == best
                checked += 1
    assert checked >= 60


def test_calculation_answer_keys_are_recomputed_independently():
    import statistics

    fns = {
        "sum": sum,
        "mean": statistics.fmean,
        "median": statistics.median,
        "min": min,
        "max": max,
        "stdev": statistics.stdev,
        "ratio": lambda v: v[0] / v[1],
        "percent_change": lambda v: (v[1] - v[0]) / v[0] * 100,
    }
    n = 0
    for entry, ds in load_datasets():
        if entry["family"] != "calculation":
            continue
        for c in ds.cases:
            if c.input["operation"] == "evaluate":
                expr = c.input["expression"].replace("^", "**")
                assert re.fullmatch(r"[0-9.+\-*/() ]+", expr)
                want = float(eval(expr, {"__builtins__": {}}, {}))
            else:
                want = float(fns[c.input["operation"]](c.input["values"]))
            assert c.expected_output["result"] == pytest.approx(want, rel=1e-8)
            n += 1
    assert n >= 25


def test_every_case_is_a_valid_openeval_case_with_a_deterministic_method():
    for _, ds in load_datasets():
        assert isinstance(ds, openeval.Dataset)
        for c in ds.cases:
            assert any(m != "llm_judge" for m in c.evaluation_methods)


# ----------------------------------------------------------------------------- leaderboard statistics


def contender(name, outcomes):
    return lb.ContenderScores(uuid.uuid4(), name, 1, uuid.uuid4(), "h" * 64, outcomes=outcomes)


def outcomes(rates, n=20):
    return {f"fam{i}": {f"c{j}": j < round(r * n) for j in range(n)} for i, r in enumerate(rates)}


def test_wilson_interval_known_values():
    lo, hi = lb.wilson(8, 10)
    assert (round(lo, 3), round(hi, 3)) == (0.49, 0.943)
    assert lb.wilson(0, 0) is None
    lo, hi = lb.wilson(0, 10)
    assert lo == 0.0 and hi < 0.31


def test_macro_is_an_unweighted_mean_of_families():
    out = {"a": {"1": True, "2": True}, "b": {str(i): i == 0 for i in range(8)}}
    assert lb.macro(out) == pytest.approx((1.0 + 0.125) / 2)


def test_ranking_orders_separates_and_ties_with_statistics():
    strong = contender("strong", outcomes([1.0, 0.95, 0.9], n=30))
    twin_a = contender("twin-a", outcomes([0.6, 0.6, 0.6], n=30))
    twin_b = contender("twin-b", outcomes([0.6, 0.6, 0.6], n=30))
    weak = contender("weak", outcomes([0.1, 0.1, 0.0], n=30))
    board = lb.build_leaderboard([weak, twin_b, strong, twin_a])
    ranks = {r["agent"]: r["rank"] for r in board["rows"]}
    assert ranks["strong"] == 1 and ranks["weak"] == 4
    assert ranks["twin-a"] == ranks["twin-b"] == 2  # indistinguishable contenders share a rank
    assert board["rows"][0]["agent"] == "strong"
    assert board == lb.build_leaderboard([weak, twin_b, strong, twin_a])  # deterministic


def test_small_differences_on_few_cases_are_not_separated():
    a = contender("a", outcomes([0.7], n=10))
    b = contender("b", outcomes([0.6], n=10))
    board = lb.build_leaderboard([a, b])
    assert [r["rank"] for r in board["rows"]] == [1, 1]
    assert board["adjacent_comparisons"][0]["separated"] is False


def test_selection_vs_test_flags_large_gaps():
    sel = lb.build_leaderboard([contender("x", outcomes([0.9], n=20))])
    test = lb.build_leaderboard([contender("x", outcomes([0.5], n=20))])
    test["rows"][0]["agent_version_id"] = sel["rows"][0]["agent_version_id"]
    cmp = lb.selection_vs_test(sel, test)
    assert cmp[0]["gap_flag"] is True and cmp[0]["gap"] == pytest.approx(0.4)


# ----------------------------------------------------------------------------- policy helpers


def test_prompt_flags_and_significant_digits():
    f = flags(
        "Report prices as JSON numbers with no currency symbols. Cite the section. Never guess: return null."
    )
    assert f == {"numbers": True, "cite": True, "abstain": True}
    assert flags("You answer questions.") == {"numbers": False, "cite": False, "abstain": False}
    assert significant(1234.5678) == 1230 and significant(0.0123456) == 0.0123 and significant(0) == 0
