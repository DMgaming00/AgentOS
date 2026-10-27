"""Evidence-based cost suggestions. A suggestion states what was observed (measured) and, only where it can be computed
from data, a labeled saving:

* ``measured``  -- an amount that was actually spent on something avoidable (an upper bound on what could be saved);
* ``projected`` -- a calculation from the price table or a stated assumption, with the formula shown;
* ``none``      -- no cost claim is made (the benefit is latency, safety or context size).

No suggestion promises a saving it cannot show, and none changes anything: they are inputs for a human or for an
Autopilot experiment.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from agentos_core.finops.analytics import Inputs
from agentos_core.pricing import PricingTable, get_pricing
from openeval.metrics import nearest_rank

MILLION = Decimal(1_000_000)


@dataclass(frozen=True)
class ApprovedModel:
    provider: str
    model: str


def _s(
    id_: str, title: str, evidence: dict[str, Any], action: str, basis: str, usd: float | None, note: str
) -> dict[str, Any]:
    return {
        "id": id_,
        "title": title,
        "evidence": evidence,
        "action": action,
        "savings": {"basis": basis, "usd": usd, "note": note},
    }


def suggest(
    inp: Inputs,
    summary: dict[str, Any],
    *,
    approved_models: list[ApprovedModel] | None = None,
    pricing: PricingTable | None = None,
) -> list[dict[str, Any]]:
    table = pricing or get_pricing()
    out: list[dict[str, Any]] = []
    totals = summary["totals"]

    # 1. redundant tool calls within a run
    groups: dict[tuple[uuid.UUID, str, str], int] = defaultdict(int)
    for t in inp.tools:
        if t.status == "succeeded":
            groups[(t.run_id, t.tool, t.args_hash)] += 1
    redundant = sum(n - 1 for n in groups.values() if n > 1)
    if redundant >= 5:
        out.append(
            _s(
                "reduce_redundant_tool_calls",
                "Reduce redundant tool calls",
                {
                    "redundant_calls": redundant,
                    "distinct_repeated_calls": sum(1 for n in groups.values() if n > 1),
                },
                "Add duplicate-call protection to the agent's tool strategy (Autopilot rule `redundant_tool_calls`) and evaluate it.",
                "none",
                None,
                "Tool calls carry no model cost here; the benefit is fewer calls and shorter context.",
            )
        )

    # 2. retries that ended in failure
    retry = totals["retry_cost_usd"] or 0.0
    if retry > 0 and (totals["retry_cost_share"] or 0) >= 0.10:
        out.append(
            _s(
                "bound_retries",
                "Bound retries on persistent failures",
                {"retry_cost_usd": retry, "share_of_spend": totals["retry_cost_share"]},
                "Lower `retry_max_attempts` for the agents below and check whether the failures were transient.",
                "measured",
                retry,
                f"${retry:.4f} was spent on attempts that ended in failure; at most this amount could have been avoided.",
            )
        )

    # 3. cache read-only tool results repeated across runs
    across: dict[tuple[str, str], int] = defaultdict(int)
    for t in inp.tools:
        if t.status == "succeeded" and t.read_only:
            across[(t.tool, t.args_hash)] += 1
    repeated = {k: n for k, n in across.items() if n >= 10}
    if repeated:
        top = max(repeated.items(), key=lambda kv: kv[1])
        out.append(
            _s(
                "cache_read_only_tools",
                "Cache read-only tool results",
                {
                    "repeated_calls": sum(repeated.values()),
                    "most_repeated": {"tool": top[0][0], "times": top[1]},
                },
                "Cache results of read-only tools keyed by arguments, with a short expiry (stale data is the risk).",
                "none",
                None,
                "Saves latency and load; no cost claim.",
            )
        )

    # 4. cheaper approved model (projected from the price table)
    for m in summary["by_model"]:
        if m["calls"] < 50:
            continue
        cur = table.lookup(m["provider"], m["model"])
        if cur is None:
            continue
        best: tuple[Decimal, ApprovedModel] | None = None
        for a in approved_models or []:
            if (a.provider, a.model) == (m["provider"], m["model"]):
                continue
            p = table.lookup(a.provider, a.model)
            if p is None:
                continue
            saved = (
                Decimal(m["input_tokens"]) * (cur.input - p.input)
                + Decimal(m["output_tokens"]) * (cur.output - p.output)
            ) / MILLION
            if saved > 0 and (best is None or saved > best[0]):
                best = (saved, a)
        if best is not None and float(best[0]) >= 0.2 * (m["estimated_cost_usd"] or 0) > 0:
            out.append(
                _s(
                    "cheaper_approved_model",
                    f"Evaluate {best[1].provider}/{best[1].model} instead of {m['provider']}/{m['model']}",
                    {
                        "calls": m["calls"],
                        "input_tokens": m["input_tokens"],
                        "output_tokens": m["output_tokens"],
                        "price_table": table.version,
                    },
                    "Run a paired Autopilot experiment with the approved cheaper model; adopt it only if quality holds.",
                    "projected",
                    float(best[0]),
                    "projection = input_tokens x (current - alternative input price) + output_tokens x (current - alternative output price); quality is unverified.",
                )
            )

    # 5. oversized context
    for m in summary["by_model"]:
        if (
            m["calls"] >= 20
            and m["output_tokens"]
            and m["input_tokens"] / m["calls"] >= 2000
            and m["input_tokens"] / max(1, m["output_tokens"]) >= 20
        ):
            cur = table.lookup(m["provider"], m["model"])
            projected = (
                float(Decimal(m["input_tokens"]) * Decimal("0.25") * cur.input / MILLION) if cur else None
            )
            out.append(
                _s(
                    "reduce_context",
                    f"Trim context sent to {m['provider']}/{m['model']}",
                    {
                        "mean_input_tokens_per_call": round(m["input_tokens"] / m["calls"]),
                        "input_to_output_ratio": round(m["input_tokens"] / m["output_tokens"], 1),
                    },
                    "Retrieve fewer or shorter excerpts, or summarize long tool results before they re-enter the context.",
                    "projected",
                    projected,
                    "projection = 25% fewer input tokens at the current input price (an assumption, not a measurement).",
                )
            )

    # 6. queueing (concurrency / worker capacity)
    waits = [float(a.queue_wait_ms) for a in inp.attempts if a.queue_wait_ms is not None]
    if len(waits) >= 20:
        p95 = nearest_rank(waits, 0.95)
        if p95 is not None and p95 >= 5000:
            out.append(
                _s(
                    "increase_capacity",
                    "Steps are waiting for a worker",
                    {"queue_wait_p95_ms": p95, "samples": len(waits)},
                    "Add worker processes or raise per-worker concurrency; check run priorities if only some runs wait.",
                    "none",
                    None,
                    "Latency, not cost.",
                )
            )
    return out
