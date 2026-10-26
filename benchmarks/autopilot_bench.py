"""Autopilot benchmark: the full optimization cycle on an isolated database with real worker processes.

    uv run --group bench python benchmarks/autopilot_bench.py --label autopilot [--trials 3]

Measures (all from persisted records): baseline/candidate success on validation and held-out
test, per-case regressions, tool-call correctness, cost per successful task, p50/p95 latency,
time spent in each optimization phase, candidates generated/rejected, live failure rate before
and after rollback, and evaluation wall time vs worker concurrency. Uses the deterministic mock
provider, so quality numbers describe the scripted demo agent, not a real model.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import time
import uuid
from pathlib import Path
from typing import Any

from harness import create_database, machine_info, reset, workers

from agentos_core.autopilot.config import AgentConfig
from agentos_core.autopilot.demo import EXTRACTION_AGENT, live_extraction_workflow
from agentos_core.autopilot.deployment import decide_request, request_promotion, rollback
from agentos_core.autopilot.diagnosis import diagnose
from agentos_core.autopilot.evaluation import create_evaluation_run
from agentos_core.autopilot.experiments import start_experiment
from agentos_core.autopilot.failures import analyze
from agentos_core.autopilot.optimizer import (
    ProposedCandidate,
    ProposedChange,
    generate_candidates,
    propose_manual_candidate,
)
from agentos_core.db.models import (
    AgentConfigVersion,
    AgentDefinition,
    EvaluationDataset,
    EvaluationRun,
    ModelUsage,
    OptimizationExperiment,
    RolloutEvent,
    WorkflowRun,
)
from agentos_core.db.session import dispose_engine, get_sessionmaker
from agentos_core.engine.tx import unit_of_work
from agentos_core.testing import get_steps, start_run

OUT = Path(__file__).parent / "results"


async def poll(fn: Any, timeout: float = 600) -> Any:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        v = await fn()
        if v:
            return v
        await asyncio.sleep(0.25)
    raise TimeoutError


async def datasets(ws: uuid.UUID) -> dict[str, EvaluationDataset]:
    from sqlalchemy import select

    async with get_sessionmaker()() as s:
        return {
            d.split: d
            for d in (
                await s.execute(select(EvaluationDataset).where(EvaluationDataset.workspace_id == ws))
            ).scalars()
        }


async def wait_exp(exp_id: uuid.UUID) -> OptimizationExperiment:
    async def f() -> Any:
        async with get_sessionmaker()() as s:
            e = await s.get(OptimizationExperiment, exp_id)
            return e if e and e.status != "running" else None

    return await poll(f)


async def wait_eval(ev_id: uuid.UUID) -> EvaluationRun:
    async def f() -> Any:
        async with get_sessionmaker()() as s:
            e = await s.get(EvaluationRun, ev_id)
            return e if e and e.status == "completed" else None

    return await poll(f)


def arm_row(label: str, split: str, m: dict[str, Any], trials: int) -> dict[str, Any]:
    return {
        "arm": label,
        "split": split,
        "trials": trials,
        "executions": m["attempts"],
        "task_success": round(m["task_success_rate"]["value"] or 0, 4),
        "success_numerator": m["task_success_rate"]["numerator"],
        "success_denominator": m["task_success_rate"]["denominator"],
        "schema_compliance": m["schema_compliance"]["value"],
        "grounding": m["grounding_pass_rate"]["value"],
        "tool_correctness": m["tool_call_correctness"]["value"],
        "cost_per_success_usd": m["cost_per_successful_task_usd"],
        "total_cost_usd": m["total_cost_usd"],
        "latency_p50_ms": m["latency_ms"]["p50"],
        "latency_p95_ms": m["latency_ms"]["p95"],
    }


async def cycle(trials: int, concurrency: int) -> dict[str, Any]:
    seeded = await reset()
    ws = seeded.workspace.id
    admin, dev = seeded.users["admin"].id, seeded.users["developer"].id
    agent = seeded.agents[EXTRACTION_AGENT]
    ds = await datasets(ws)
    phases: dict[str, float] = {}
    arms: list[dict[str, Any]] = []
    with workers(2, concurrency):
        await asyncio.sleep(1.5)
        t = time.perf_counter()
        async with unit_of_work() as s:
            v1 = await s.get(AgentConfigVersion, agent.active_version_id)
            ev = await create_evaluation_run(
                s,
                agent=agent,
                version=v1,
                dataset=ds["development"],
                trials=1,
                created_by=dev,
                max_concurrency=concurrency,
                per_case_max_cost_usd=0.25,
            )
        await wait_eval(ev.id)
        phases["observe_dev_benchmark_s"] = time.perf_counter() - t
        t = time.perf_counter()
        async with unit_of_work() as s:
            await analyze(s, ws, window_hours=2)
        phases["detect_failures_s"] = time.perf_counter() - t
        t = time.perf_counter()
        async with unit_of_work() as s:
            d = await diagnose(
                s, workspace_id=ws, agent_id=agent.id, category="OUTPUT_SCHEMA_INVALID", user_id=dev
            )
        phases["diagnose_s"] = time.perf_counter() - t
        t = time.perf_counter()
        async with unit_of_work() as s:
            cands = await generate_candidates(
                s, workspace_id=ws, agent_id=agent.id, diagnosis_id=d.id, user_id=dev
            )
        phases["generate_candidates_s"] = time.perf_counter() - t
        results: dict[str, Any] = {
            "candidates": [
                {"title": c.title, "status": c.status, "violations": c.policy_violations} for c in cands
            ]
        }
        decisions = {}
        for c in [c for c in cands if c.candidate_version_id]:
            t = time.perf_counter()
            async with unit_of_work() as s:
                e = await start_experiment(
                    s,
                    workspace_id=ws,
                    agent_id=agent.id,
                    candidate_id=c.id,
                    dataset_id=ds["validation"].id,
                    trials=trials,
                    user_id=dev,
                )
            e = await wait_exp(e.id)
            phases[f"experiment_{c.title[:18]}_s"] = time.perf_counter() - t
            decisions[c.title] = e.decision
            r = e.results
            assert r is not None
            arms.append(
                arm_row("baseline v1", "validation", r["baseline"]["metrics"], trials)
                | {"candidate": c.title}
            )
            arms.append(
                arm_row(
                    f"candidate v{r['candidate']['version']}", "validation", r["candidate"]["metrics"], trials
                )
                | {
                    "candidate": c.title,
                    "decision": e.decision,
                    "regressions": len(r["comparison"]["regressions"]),
                    "regression_rate": len(r["comparison"]["regressions"]) / r["comparison"]["n_matched"],
                    "diff_mean": r["comparison"]["diff_mean"],
                    "ci_low": r["comparison"]["ci95"][0],
                    "ci_high": r["comparison"]["ci95"][1],
                }
            )
            if e.decision == "eligible":
                fix_exp, fix = e, c
        results["decisions"] = decisions
        # held-out test split: final, never used for selection
        for label, vid in (("baseline v1", v1.id), ("promoted v2", fix.candidate_version_id)):
            async with unit_of_work() as s:
                v = await s.get(AgentConfigVersion, vid)
                ev = await create_evaluation_run(
                    s,
                    agent=agent,
                    version=v,
                    dataset=ds["test"],
                    trials=trials,
                    created_by=dev,
                    max_concurrency=concurrency,
                    per_case_max_cost_usd=0.25,
                )
            done = await wait_eval(ev.id)
            arms.append(arm_row(label, "test", done.metrics or {}, trials))
        t = time.perf_counter()
        async with unit_of_work() as s:
            req = await request_promotion(s, workspace_id=ws, experiment_id=fix_exp.id, user_id=dev)
            await decide_request(
                s, workspace_id=ws, request_id=req.id, approve=True, user_id=admin, reason="bench"
            )
        phases["promote_s"] = time.perf_counter() - t

        # live traffic, regression, rollback
        async def live(n: int) -> list[uuid.UUID]:
            ids = [await start_run(seeded, live_extraction_workflow(), name="live") for _ in range(n)]

            async def f() -> Any:
                async with get_sessionmaker()() as s:
                    from sqlalchemy import func, select

                    left = (
                        await s.execute(
                            select(func.count())
                            .select_from(WorkflowRun)
                            .where(
                                WorkflowRun.id.in_(ids), WorkflowRun.status.not_in(["succeeded", "failed"])
                            )
                        )
                    ).scalar_one()
                    return left == 0

            await poll(f, 120)
            return ids

        async def failure_rate(ids: list[uuid.UUID]) -> float:
            st = [(await get_steps(i))["extract"].status for i in ids]
            return st.count("failed") / len(st)

        before = await live(10)
        async with unit_of_work() as s:
            cap = await propose_manual_candidate(
                s,
                workspace_id=ws,
                agent_id=agent.id,
                user_id=dev,
                proposal=ProposedCandidate(
                    title="Cap agent steps at 2",
                    changes=[ProposedChange(field="max_steps", value=2, reason="cost cap")],
                ),
            )
            e3 = await start_experiment(
                s,
                workspace_id=ws,
                agent_id=agent.id,
                candidate_id=cap.id,
                dataset_id=ds["validation"].id,
                trials=trials,
                user_id=dev,
            )
        e3 = await wait_exp(e3.id)
        async with unit_of_work() as s:
            req = await request_promotion(s, workspace_id=ws, experiment_id=e3.id, user_id=dev)
            await decide_request(
                s, workspace_id=ws, request_id=req.id, approve=True, user_id=admin, reason="bench"
            )
        t_deploy = time.perf_counter()
        during = await live(10)

        async def flagged() -> Any:
            from sqlalchemy import select

            async with get_sessionmaker()() as s:
                return (
                    (
                        await s.execute(
                            select(RolloutEvent).where(RolloutEvent.event_type == "regression_detected")
                        )
                    )
                    .scalars()
                    .first()
                )

        await poll(flagged, 60)
        phases["regression_detection_after_deploy_s"] = time.perf_counter() - t_deploy
        async with unit_of_work() as s:
            await rollback(s, workspace_id=ws, agent_id=agent.id, user_id=admin, reason="bench regression")
        after = await live(10)
        results["live"] = {
            "before_regression_failure_rate": await failure_rate(before),
            "regressed_failure_rate": await failure_rate(during),
            "after_rollback_failure_rate": await failure_rate(after),
            "runs_per_phase": 10,
            "regressed_candidate_offline_decision": e3.decision,
        }
    from sqlalchemy import func, select

    async with get_sessionmaker()() as s:
        spend = (
            await s.execute(
                select(func.coalesce(func.sum(ModelUsage.estimated_cost_usd), 0)).where(
                    ModelUsage.purpose.in_(["diagnosis", "optimizer"])
                )
            )
        ).scalar_one()
        agent_now = await s.get(AgentDefinition, agent.id)
        final = AgentConfig.model_validate(
            (await s.get(AgentConfigVersion, agent_now.active_version_id)).config_snapshot
        )
    results["autopilot_llm_spend_usd_simulated"] = float(spend)
    results["final_active_max_steps"] = final.max_steps
    return {"phases": phases, "arms": arms, "results": results}


async def concurrency_scan(trials: int) -> list[dict[str, Any]]:
    rows = []
    for conc in (1, 2, 4, 8):
        seeded = await reset()
        agent = seeded.agents[EXTRACTION_AGENT]
        ds = await datasets(seeded.workspace.id)
        with workers(2, max(conc, 1)):
            await asyncio.sleep(1.5)
            t = time.perf_counter()
            async with unit_of_work() as s:
                v1 = await s.get(AgentConfigVersion, agent.active_version_id)
                ev = await create_evaluation_run(
                    s,
                    agent=agent,
                    version=v1,
                    dataset=ds["validation"],
                    trials=trials,
                    created_by=None,
                    max_concurrency=conc,
                    per_case_max_cost_usd=0.25,
                )
            done = await wait_eval(ev.id)
            wall = time.perf_counter() - t
        rows.append(
            {
                "eval_max_concurrency": conc,
                "executions": done.metrics["attempts"],
                "wall_s": round(wall, 3),
                "executions_per_s": round(done.metrics["attempts"] / wall, 2),
            }
        )
        print("concurrency", rows[-1], flush=True)
    return rows


def write(path: Path, rows: list[dict[str, Any]]) -> None:
    keys: list[str] = []
    for r in rows:
        keys += [k for k in r if k not in keys]
    with path.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=keys)
        w.writeheader()
        w.writerows(rows)


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", default="autopilot")
    ap.add_argument("--trials", type=int, default=3)
    args = ap.parse_args()
    out = OUT / args.label
    out.mkdir(parents=True, exist_ok=True)
    await create_database()
    t0 = time.time()
    res = await cycle(args.trials, concurrency=6)
    print(json.dumps(res["results"], indent=1, default=str), flush=True)
    scan = await concurrency_scan(1)
    write(out / "arms.csv", res["arms"])
    write(out / "phases.csv", [{"phase": k, "seconds": round(v, 3)} for k, v in res["phases"].items()])
    write(out / "eval_concurrency.csv", scan)
    (out / "results.json").write_text(json.dumps(res["results"], indent=2, default=str))
    (out / "meta.json").write_text(
        json.dumps(
            {
                "label": args.label,
                "trials": args.trials,
                "machine": machine_info(),
                "duration_s": round(time.time() - t0, 1),
                "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "provider": "mock (deterministic, simulated pricing)",
            },
            indent=2,
        )
    )
    await dispose_engine()
    print(f"results in {out}")


if __name__ == "__main__":
    asyncio.run(main())
