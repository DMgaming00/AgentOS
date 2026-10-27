"""Platform benchmarks for the 2.0 modules: memory, security, deployment, optimization.

Real worker processes, an isolated database and the deterministic offline providers. Every number is derived from records
the platform wrote (graded executions, ledgers, deployment and release events) or, where stated, from the real rule code
run on seeded synthetic samples. None of it measures a real model.

    cd benchmarks && uv run --group bench python platform_bench.py all --label platform
    cd benchmarks && uv run --group bench python platform_bench.py memory|security|deployment|optimization --label platform
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import math
import random
import time
import uuid
from pathlib import Path
from typing import Any

from harness import create_database, machine_info, reset, workers
from sqlalchemy import select

from agentos_core.autopilot.demo import EXTRACTION_AGENT, live_extraction_workflow
from agentos_core.autopilot.deployment import decide_request
from agentos_core.autopilot.diagnosis import diagnose
from agentos_core.autopilot.evaluation import create_evaluation_run
from agentos_core.autopilot.experiments import start_experiment
from agentos_core.autopilot.failures import analyze
from agentos_core.autopilot.optimizer import generate_candidates
from agentos_core.autopilot.rules import generate_rule_candidates
from agentos_core.autopilot.versions import canary_bucket
from agentos_core.db.models import (
    AgentConfigVersion,
    AgentDefinition,
    EvaluationRun,
    MemoryExperiment,
    ModelUsage,
    OptimizationExperiment,
    ReleasePlan,
    SecurityEvaluationRun,
    WorkflowRun,
    WorkflowStep,
)
from agentos_core.db.session import dispose_engine
from agentos_core.engine.tx import unit_of_work
from agentos_core.memory import experiments as mx
from agentos_core.release import controller as rc
from agentos_core.release.rules import Arm, RollbackRules, Sample, evaluate
from agentos_core.security import runner as security_runner
from agentos_core.testing import start_run
from agentos_core.testing_autopilot import clean_security_run, dataset, wait_row
from agentos_core.util import utcnow

OUT = Path(__file__).parent / "results"


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)


# ----------------------------------------------------------------------------- memory

CHURN = [(0, 0), (4, 2), (8, 3), (12, 5)]  # (facts updated, facts expired) between the two sessions


async def bench_memory(out: Path, seeds: list[int]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for updated, expired in CHURN:
        for seed in seeds:
            seeded = await reset()
            t0 = time.perf_counter()
            async with unit_of_work() as s:
                exp = await mx.start_experiment(
                    s,
                    workspace_id=seeded.workspace.id,
                    user_id=None,
                    seed=seed,
                    updated=updated,
                    expired=expired,
                )
                exp_id = exp.id
            with workers(2, 6):
                while True:
                    async with unit_of_work() as s:
                        e = await s.get(MemoryExperiment, exp_id)
                        if e is not None and e.status != "running":
                            break
                    await asyncio.sleep(0.5)
            assert e.status == "completed", e.error
            wall = time.perf_counter() - t0
            for strategy, r in e.results["strategies"].items():
                rows.append(
                    {
                        "updated_facts": updated,
                        "expired_facts": expired,
                        "seed": seed,
                        "strategy": strategy,
                        "questions": r["accuracy"]["denominator"],
                        "accuracy": r["accuracy"]["value"],
                        "stale_errors": r["stale_memory_errors"]["count"],
                        "stale_denominator": r["stale_memory_errors"]["denominator"],
                        "wrong_entity_reuse": r["incorrect_memory_reuse"]["count"],
                        "reuse_denominator": r["incorrect_memory_reuse"]["denominator"],
                        "precision": r["retrieval_precision"]["value"],
                        "precision_n": r["retrieval_precision"]["denominator"],
                        "answer_recall": r["answer_recall"]["value"],
                        "context_chars_mean": round(r["context"]["mean_chars"], 1),
                        "context_tokens_est": round(r["context"]["mean_tokens_est"], 1),
                        "latency_p50_ms": r["latency_ms"]["p50"],
                        "cost_usd_simulated": r["cost_usd_simulated"],
                        "experiment_wall_s": round(wall, 2),
                    }
                )
            print("memory", updated, expired, seed, f"{wall:.1f}s", flush=True)
    write_csv(out / "memory.csv", rows)
    return rows


# ----------------------------------------------------------------------------- security


async def bench_security(out: Path, repetitions: int) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seeded = await reset()
    with workers(2, 4):
        await asyncio.sleep(1.5)
        for rep in range(repetitions):
            t0 = time.perf_counter()
            async with unit_of_work() as s:
                run = await security_runner.start_security_run(
                    s, workspace_id=seeded.workspace.id, user_id=seeded.users["admin"].id
                )
                run_id = run.id
            while True:
                async with unit_of_work() as s:
                    r = await s.get(SecurityEvaluationRun, run_id)
                    if r is not None and r.status != "running":
                        break
                await asyncio.sleep(0.5)
            assert r.status == "completed", r.error
            m, c = r.results["metrics"], r.results["counts"]
            rows.append(
                {
                    "repetition": rep,
                    "wall_s": round(time.perf_counter() - t0, 2),
                    "policy_pass": c["policy"]["pass"],
                    "policy_total": c["policy"]["total"],
                    "injection_pass": c["injection"]["pass"],
                    "injection_total": c["injection"]["total"],
                    "benign_pass": c["benign"]["pass"],
                    "benign_total": c["benign"]["total"],
                    "exposure_pass": c["exposure"]["pass"],
                    "exposure_total": c["exposure"]["total"],
                    "attacks_exercised": m["attacks_exercised"]["numerator"],
                    "attacks_total": m["attacks_exercised"]["denominator"],
                    "unauthorized_action_attempts": m["unauthorized_action_attempts"],
                    "blocked_actions": m["blocked_actions"],
                    "successful_enforcement": m["successful_policy_enforcement"]["numerator"],
                    "enforcement_denominator": m["successful_policy_enforcement"]["denominator"],
                    "unsafe_side_effects": m["unsafe_side_effects"],
                    "false_positive_blocks": m["false_positive_blocks"]["numerator"],
                    "false_positive_denominator": m["false_positive_blocks"]["denominator"],
                    "canary_leaks": m["cross_workspace_access_attempts"]["canary_leaks"],
                    "secrets_in_traces": m["secret_exposure_in_traces"]["numerator"],
                    "secret_kinds_planted": m["secret_exposure_in_traces"]["denominator"],
                }
            )
            print("security", rep, f"{rows[-1]['wall_s']}s", flush=True)
    write_csv(out / "security.csv", rows)
    return rows


# ----------------------------------------------------------------------------- deployment


def routing_accuracy(out: Path, n: int = 50_000) -> list[dict[str, Any]]:
    rows = []
    agent = uuid.UUID(int=1)
    for seed in ("seed-a", "seed-b", "seed-c", None):
        rng = random.Random(f"routing-{seed}")
        ids = [uuid.UUID(int=rng.getrandbits(128), version=4) for _ in range(n)]
        buckets = [canary_bucket(r, agent, seed) for r in ids]
        assert buckets == [canary_bucket(r, agent, seed) for r in ids]  # deterministic
        for target in (1, 5, 10, 25, 50, 75, 99, 100):
            share = sum(b < target for b in buckets) / n
            half = 1.96 * math.sqrt(max(share * (1 - share), 1e-12) / n)
            nested = all((b < target) <= (b < min(100, target + 25)) for b in buckets[:2000])
            rows.append(
                {
                    "seed": seed or "(none)",
                    "target_percent": target,
                    "n": n,
                    "achieved_share": round(share, 5),
                    "abs_error_points": round(abs(share * 100 - target), 4),
                    "binomial_95_halfwidth_points": round(half * 100, 4),
                    "within_95_ci": abs(share - target / 100) <= half + 1e-9,
                    "nested_with_larger_percent": nested,
                }
            )
    write_csv(out / "routing_accuracy.csv", rows)
    return rows


def rollback_characteristics(out: Path, trials: int = 2000) -> list[dict[str, Any]]:
    """The real rule functions on seeded synthetic arms: false-rollback rate and detection power per effect size and sample size."""
    rows = []
    now = utcnow()
    rules = RollbackRules()
    p0 = 0.05
    rng = random.Random(20261026)

    def arm(n: int, p: float) -> Arm:
        return Arm([Sample(at=now, ok=rng.random() >= p, latency_ms=100.0, cost_usd=0.001) for _ in range(n)])

    for n in (10, 20, 40, 80):
        for p1 in (0.05, 0.10, 0.20, 0.40, 0.80):
            fr = eb = either = insufficient = 0
            for _ in range(trials):
                res = {r["rule"]: r for r in evaluate(rules, arm(n, p1), arm(n, p0), now=now)}
                a, b = (
                    res["failure_rate_increase"]["status"] == "triggered",
                    res["error_budget_exhausted"]["status"] == "triggered",
                )
                fr += a
                eb += b
                either += a or b
                insufficient += res["failure_rate_increase"]["status"] == "insufficient_evidence"
            rows.append(
                {
                    "samples_per_arm": n,
                    "baseline_failure_rate": p0,
                    "canary_failure_rate": p1,
                    "trials": trials,
                    "failure_rate_rule_trigger": round(fr / trials, 4),
                    "error_budget_rule_trigger": round(eb / trials, 4),
                    "either_rule_trigger": round(either / trials, 4),
                    "insufficient_evidence_share": round(insufficient / trials, 4),
                    "is_healthy_case": p1 == p0,
                }
            )
    write_csv(out / "rollback_characteristics.csv", rows)
    return rows


async def live_release_trials(out: Path, repetitions: int) -> list[dict[str, Any]]:
    """Real releases: gates, approval, seeded canary, live traffic through workers, automatic rollback, version consistency."""
    from agentos_core.autopilot.config import AgentConfig

    rows = []
    for rep in range(repetitions):
        seeded = await reset()
        ws, admin, dev = seeded.workspace.id, seeded.users["admin"].id, seeded.users["developer"].id
        agent = seeded.agents[EXTRACTION_AGENT]
        with workers(3, 6):
            await asyncio.sleep(1.5)
            # a healthy baseline: the numeric-format fix as v2, promoted through the normal pipeline
            agent, fix_exp = await _eligible(seeded)
            async with unit_of_work() as s:
                req = await _promote(s, ws, fix_exp.id, dev)
                await decide_request(
                    s, workspace_id=ws, request_id=req.id, approve=True, user_id=admin, reason="bench"
                )
            async with unit_of_work() as s:
                a = await s.get(AgentDefinition, agent.id)
                healthy_version = a.active_version_id
                snap = AgentConfig.model_validate(
                    (await s.get(AgentConfigVersion, healthy_version)).config_snapshot
                )
                cap = await _max_steps_candidate(s, ws, agent.id, dev, snap)
            async with unit_of_work() as s:
                exp = await start_experiment(
                    s,
                    workspace_id=ws,
                    agent_id=agent.id,
                    candidate_id=cap.id,
                    dataset_id=(await dataset(ws, "validation")).id,
                    trials=1,
                    user_id=dev,
                )
            exp = await wait_row(OptimizationExperiment, exp.id, {"completed", "failed"}, 150)
            assert exp.decision == "eligible", exp.eligibility
            await clean_security_run(ws)
            async with unit_of_work() as s:
                plan = await rc.create_release(
                    s,
                    workspace_id=ws,
                    agent_id=agent.id,
                    user_id=dev,
                    environment="production",
                    experiment_id=exp.id,
                    schedule=[50, 100],
                    seed=f"bench-{rep}",
                    auto_rollback=True,
                    min_stage_samples=5,
                )
                plan_id, request_id = plan.id, plan.request_id
            async with unit_of_work() as s:
                await decide_request(
                    s, workspace_id=ws, request_id=request_id, approve=True, user_id=admin, reason="bench"
                )
            run_ids: list[uuid.UUID] = []
            t_traffic = time.perf_counter()
            for _ in range(30):
                run_ids.append(await start_run(seeded, _live_def(agent.name), name="rel"))
            await _wait_runs(run_ids)
            # the rules need a minimum number of steps in BOTH arms; keep sending until the seeded routing delivered them
            while True:
                n_canary = sum(canary_bucket(r, agent.id, f"bench-{rep}") < 50 for r in run_ids)
                if min(n_canary, len(run_ids) - n_canary) >= 12 or len(run_ids) >= 150:
                    break
                more = [await start_run(seeded, _live_def(agent.name), name="rel") for _ in range(10)]
                await _wait_runs(more)
                run_ids += more
            t_ready = time.perf_counter()
            deadline = time.monotonic() + 90
            while True:  # evidence is now sufficient; the coordinator's tick decides
                p = await _plan(plan_id)
                if p.status != "active":
                    break
                if time.monotonic() > deadline:
                    raise TimeoutError(
                        f"no automatic rollback within 90 s (canary {n_canary}, baseline {len(run_ids) - n_canary} runs)"
                    )
                await asyncio.sleep(0.1)
            detect_s = time.perf_counter() - t_ready
            async with unit_of_work() as s:
                p = await s.get(ReleasePlan, plan_id)
                a = await s.get(AgentDefinition, agent.id)
                cand_version = p.candidate_version_id
                expected = {
                    rid: (
                        cand_version if canary_bucket(rid, agent.id, f"bench-{rep}") < 50 else healthy_version
                    )
                    for rid in run_ids
                }
                steps = {
                    x.run_id: x
                    for x in (
                        await s.execute(select(WorkflowStep).where(WorkflowStep.run_id.in_(run_ids)))
                    ).scalars()
                }
                mismatches = sum(1 for rid in run_ids if steps[rid].agent_version_id != expected[rid])
                canary_runs = [rid for rid in run_ids if expected[rid] == cand_version]
                failed_canary = sum(1 for rid in canary_runs if steps[rid].status == "failed")
                restored = (
                    a.active_version_id == healthy_version
                    and a.canary_version_id is None
                    and a.canary_percent == 0
                )
            after = [await start_run(seeded, _live_def(agent.name), name="post") for _ in range(10)]
            await _wait_runs(after)
            async with unit_of_work() as s:
                post_steps = (
                    (await s.execute(select(WorkflowStep).where(WorkflowStep.run_id.in_(after))))
                    .scalars()
                    .all()
                )
                post_ok = sum(
                    1 for x in post_steps if x.status == "succeeded" and x.agent_version_id == healthy_version
                )
            rows.append(
                {
                    "repetition": rep,
                    "status": p.status,
                    "automatic": p.status == "rolled_back",
                    "canary_runs": len(canary_runs),
                    "baseline_runs": len(run_ids) - len(canary_runs),
                    "canary_failures": failed_canary,
                    "detect_after_traffic_s": round(detect_s, 3),
                    "traffic_s": round(t_ready - t_traffic, 2),
                    "pin_mismatches": mismatches,
                    "restored_version_consistent": restored,
                    "post_rollback_runs_ok_on_restored": f"{post_ok}/{len(after)}",
                }
            )
            print("deployment", rep, rows[-1], flush=True)
    write_csv(out / "live_release_trials.csv", rows)
    return rows


def _live_def(agent_name: str) -> dict[str, Any]:
    d = live_extraction_workflow()
    d["nodes"][0]["agent"] = agent_name
    return d


async def _plan(plan_id: uuid.UUID) -> ReleasePlan:
    async with unit_of_work() as s:
        p = await s.get(ReleasePlan, plan_id)
        assert p is not None
        return p


async def _wait_runs(ids: list[uuid.UUID], timeout: float = 180) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        async with unit_of_work() as s:
            n = (
                await s.execute(
                    select(WorkflowRun.id).where(
                        WorkflowRun.id.in_(ids),
                        WorkflowRun.status.in_(["succeeded", "failed", "cancelled", "timed_out"]),
                    )
                )
            ).all()
        if len(n) == len(ids):
            return
        await asyncio.sleep(0.3)
    raise TimeoutError


async def _eligible(seeded: Any) -> tuple[AgentDefinition, OptimizationExperiment]:
    ws, dev = seeded.workspace.id, seeded.users["developer"].id
    agent = seeded.agents[EXTRACTION_AGENT]
    async with unit_of_work() as s:
        v1 = await s.get(AgentConfigVersion, agent.active_version_id)
        ev = await create_evaluation_run(
            s,
            agent=agent,
            version=v1,
            dataset=await dataset(ws, "development"),
            trials=1,
            created_by=dev,
            max_concurrency=6,
            per_case_max_cost_usd=0.25,
        )
    await wait_row(EvaluationRun, ev.id, {"completed"}, 120)
    async with unit_of_work() as s:
        (fix,) = await generate_rule_candidates(
            s, workspace_id=ws, agent_id=agent.id, diagnosis_id=None, user_id=dev
        )
    async with unit_of_work() as s:
        exp = await start_experiment(
            s,
            workspace_id=ws,
            agent_id=agent.id,
            candidate_id=fix.id,
            dataset_id=(await dataset(ws, "validation")).id,
            trials=1,
            user_id=dev,
        )
    exp = await wait_row(OptimizationExperiment, exp.id, {"completed", "failed"}, 150)
    assert exp.decision == "eligible"
    return agent, exp


async def _promote(s: Any, ws: uuid.UUID, exp_id: uuid.UUID, dev: uuid.UUID) -> Any:
    from agentos_core.autopilot.deployment import request_promotion

    return await request_promotion(
        s, workspace_id=ws, experiment_id=exp_id, user_id=dev, mode="full", reason="bench baseline"
    )


async def _max_steps_candidate(s: Any, ws: uuid.UUID, agent_id: uuid.UUID, dev: uuid.UUID, snap: Any) -> Any:
    from agentos_core.autopilot.optimizer import ProposedCandidate, ProposedChange, propose_manual_candidate

    _ = snap
    return await propose_manual_candidate(
        s,
        workspace_id=ws,
        agent_id=agent_id,
        user_id=dev,
        proposal=ProposedCandidate(
            title="Cap agent steps at 2",
            changes=[ProposedChange(field="max_steps", value=2, reason="reduce worst-case cost")],
        ),
    )


# ----------------------------------------------------------------------------- optimization


async def bench_optimization(out: Path, repetitions: int) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for rep in range(repetitions):
        seeded = await reset()
        ws, dev = seeded.workspace.id, seeded.users["developer"].id
        agent = seeded.agents[EXTRACTION_AGENT]
        row: dict[str, Any] = {"repetition": rep}
        with workers(2, 6):
            await asyncio.sleep(1.5)
            t = time.perf_counter()
            async with unit_of_work() as s:
                v1 = await s.get(AgentConfigVersion, agent.active_version_id)
                ev = await create_evaluation_run(
                    s,
                    agent=agent,
                    version=v1,
                    dataset=await dataset(ws, "development"),
                    trials=1,
                    created_by=dev,
                    max_concurrency=6,
                    per_case_max_cost_usd=0.25,
                )
            ev = await wait_row(EvaluationRun, ev.id, {"completed"}, 120)
            row["observe_dev_s"] = round(time.perf_counter() - t, 3)
            row["baseline_dev_success"] = ev.metrics["task_success_rate"]["value"]
            t = time.perf_counter()
            async with unit_of_work() as s:
                await analyze(s, ws, window_hours=2)
            row["detect_s"] = round(time.perf_counter() - t, 3)
            t = time.perf_counter()
            async with unit_of_work() as s:
                d = await diagnose(
                    s, workspace_id=ws, agent_id=agent.id, category="OUTPUT_SCHEMA_INVALID", user_id=dev
                )
            row["diagnose_s"] = round(time.perf_counter() - t, 3)
            t = time.perf_counter()
            async with unit_of_work() as s:
                (fix,) = await generate_rule_candidates(
                    s, workspace_id=ws, agent_id=agent.id, diagnosis_id=d.id, user_id=dev
                )
            row["generate_rules_s"] = round(time.perf_counter() - t, 3)
            t = time.perf_counter()
            async with unit_of_work() as s:
                model_cands = await generate_candidates(
                    s, workspace_id=ws, agent_id=agent.id, diagnosis_id=d.id, user_id=dev
                )
            row["generate_model_s"] = round(time.perf_counter() - t, 3)
            row["model_candidates"] = len(model_cands)
            row["model_candidates_rejected_by_policy"] = sum(
                c.status == "rejected_policy" for c in model_cands
            )
            val = (await dataset(ws, "validation")).id
            results: dict[str, Any] = {}
            for label, cand in (
                ("rule_fix", fix),
                ("fast_path", next(c for c in model_cands if c.title.startswith("Fast path"))),
            ):
                t = time.perf_counter()
                async with unit_of_work() as s:
                    e = await start_experiment(
                        s,
                        workspace_id=ws,
                        agent_id=agent.id,
                        candidate_id=cand.id,
                        dataset_id=val,
                        trials=1,
                        user_id=dev,
                    )
                e = await wait_row(OptimizationExperiment, e.id, {"completed", "failed"}, 150)
                results[label] = e
                row[f"experiment_{label}_s"] = round(time.perf_counter() - t, 3)
                r = e.results
                row[f"{label}_decision"] = e.decision
                row[f"{label}_baseline_success"] = r["baseline"]["metrics"]["task_success_rate"]["value"]
                row[f"{label}_candidate_success"] = r["candidate"]["metrics"]["task_success_rate"]["value"]
                row[f"{label}_regressions"] = len(r["comparison"]["regressions"])
                row[f"{label}_n_matched"] = r["comparison"]["n_matched"]
                row[f"{label}_ci_low"] = (r["comparison"]["ci95"] or [None, None])[0]
                row[f"{label}_baseline_cost_per_success"] = r["cost"]["baseline_per_success"]
                row[f"{label}_candidate_cost_per_success"] = r["cost"]["candidate_per_success"]
                row[f"{label}_candidate_total_cost"] = r["cost"]["candidate_total"]
                row[f"{label}_baseline_total_cost"] = r["cost"]["baseline_total"]
            async with unit_of_work() as s:
                opt_cost = (
                    await s.execute(
                        select(ModelUsage.purpose, ModelUsage.estimated_cost_usd).where(
                            ModelUsage.purpose.in_(["diagnosis", "optimizer"])
                        )
                    )
                ).all()
            row["optimizer_diagnosis_simulated_cost_usd"] = round(sum(float(c or 0) for _, c in opt_cost), 8)
            row["wall_failure_to_eligible_s"] = round(
                row["detect_s"] + row["diagnose_s"] + row["generate_rules_s"] + row["experiment_rule_fix_s"],
                3,
            )
        rows.append(row)
        print("optimization", rep, row["rule_fix_decision"], row["fast_path_decision"], flush=True)
    write_csv(out / "optimization.csv", rows)
    return rows


# ----------------------------------------------------------------------------- main


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("which", choices=["memory", "security", "deployment", "optimization", "all"])
    ap.add_argument("--label", default="platform")
    ap.add_argument("--quick", action="store_true")
    args = ap.parse_args()
    out = OUT / args.label
    out.mkdir(parents=True, exist_ok=True)
    await create_database()
    t0 = time.time()
    meta_path = out / "meta.json"
    meta = json.loads(meta_path.read_text()) if meta_path.exists() else {"sections": {}}
    todo = ["memory", "security", "deployment", "optimization"] if args.which == "all" else [args.which]
    for name in todo:
        s = time.time()
        if name == "memory":
            await bench_memory(out, [1, 2] if args.quick else [1, 2, 3])
        elif name == "security":
            await bench_security(out, 2 if args.quick else 5)
        elif name == "deployment":
            routing_accuracy(out, 10_000 if args.quick else 50_000)
            rollback_characteristics(out, 300 if args.quick else 2000)
            await live_release_trials(out, 2 if args.quick else 5)
        else:
            await bench_optimization(out, 2 if args.quick else 5)
        meta["sections"][name] = {"seconds": round(time.time() - s, 1), "quick": args.quick}
    meta.update(
        {
            "label": args.label,
            "machine": machine_info(),
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "providers": "mock (deterministic, simulated pricing); no model calls",
            "memory_churn_levels": CHURN,
        }
    )
    meta_path.write_text(json.dumps(meta, indent=2))
    await dispose_engine()
    print("done in", round(time.time() - t0, 1), "s ->", out)


if __name__ == "__main__":
    asyncio.run(main())
