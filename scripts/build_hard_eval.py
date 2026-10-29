"""Build the "hard-ops" evaluation set: 150 deterministic cases over 12 fictional platforms.

Compared with the Arena corpus (3 documents, ~25 facts, mostly single lookups) these cases need several lookups, date-based
conflict resolution, arithmetic, filtering across platforms, a fact buried in a long handbook, and abstention on near
misses. Ground truth comes from the in-memory fact model that also renders the documents; ``verify()`` then re-parses the
rendered documents with different code (line/regex based, no shared helpers) and checks every stored value and every
changelog event, so a rendering bug cannot silently become an answer-key bug.

    uv run python scripts/build_hard_eval.py            # rewrites examples/hard-eval/
    uv run python scripts/build_hard_eval.py --check    # fails if the committed files differ

Fictional data, all figures invented. Splits are stratified by question kind with a fixed seed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "examples" / "hard-eval"
SEED = 20261029
NAMES = [
    "Aurora Serve", "Brio Compute", "Cedar Infer", "Dune Runtime", "Ember Cloud", "Fjord Models",
    "Granite Host", "Halcyon AI", "Iris Engine", "Juniper Serve", "Kestrel Infer", "Lumen Cloud",
]  # fmt: skip
METRICS = {  # key: (label, unit)
    "price_in": ("Price per 1M input tokens", "USD"),
    "price_out": ("Price per 1M output tokens", "USD"),
    "p50": ("Median latency (p50)", "ms"),
    "p95": ("p95 latency", "ms"),
    "uptime": ("Uptime SLA", "%"),
    "cold_start": ("Cold start time", "s"),
    "context": ("Max context window", "tokens"),
    "regions": ("Number of regions", "regions"),
}
PHANTOM = [  # similar-sounding metrics that are NOT stated (a near miss exists for the first two)
    ("Data retention period", "days"), ("Egress bandwidth", "Gbps"), ("Free tier requests per month", "requests"),
]  # fmt: skip
SPLITS = {"development": 0.27, "validation": 0.33, "test": 0.40}
KINDS = {
    "lookup": 8,
    "compute_cost": 36,
    "multi_hop": 22,
    "conflict_latest": 14,
    "ratio": 8,
    "unanswerable": 12,
    "buried": 6,
    "aggregate_count": 8,
    "aggregate_sum": 6,
    "cheapest_blend": 30,
}
VERSION = 2
PER_1K = NAMES[
    9:
]  # these platforms state prices per 1K tokens: answering per-1M questions needs a unit conversion


def fmt_val(key: str, v: float) -> str:
    if key in ("price_in", "price_out"):
        return f"{v:.2f}"
    if key == "uptime":
        return f"{v:g}"
    return f"{v:,}" if key == "context" else f"{v:g}"


def make_world(rng: random.Random) -> dict[str, Any]:
    plats: dict[str, dict[str, float]] = {}
    for n in NAMES:
        p50 = rng.randrange(90, 400, 5)
        plats[n] = {
            "price_in": round(rng.uniform(0.08, 0.60), 2),
            "price_out": round(rng.uniform(0.30, 2.40), 2),
            "p50": p50,
            "p95": p50 + rng.randrange(60, 520, 5),
            "uptime": rng.choice([99.5, 99.9, 99.95, 99.99]),
            "cold_start": rng.randrange(2, 31),
            "context": rng.choice([8192, 32768, 65536, 128000, 200000]),
            "regions": rng.randrange(2, 15),
        }
    # changelog: some platforms change a price after the platform document was written (the document keeps the old value)
    events: list[dict[str, Any]] = []
    for n in rng.sample(NAMES, 9):
        key = rng.choice(["price_in", "price_out"])
        old = plats[n][key]
        steps = rng.choice([1, 2])
        for i in range(steps):
            new = round(max(0.05, old * rng.choice([0.7, 0.8, 0.9, 1.15, 1.3])), 2)
            if new == old:
                new = round(old + 0.03, 2)
            events.append(
                {
                    "platform": n,
                    "key": key,
                    "old": old,
                    "new": new,
                    "date": f"2026-0{2 + 2 * i}-{rng.randrange(10, 28)}",
                }
            )
            old = new
    oncall = {n: rng.randrange(10, 61, 5) for n in NAMES}
    return {
        "platforms": plats,
        "events": sorted(events, key=lambda e: (e["platform"], e["date"])),
        "oncall": oncall,
    }


def current(world: dict[str, Any], plat: str, key: str) -> float:
    ev = [e for e in world["events"] if e["platform"] == plat and e["key"] == key]
    return sorted(ev, key=lambda e: e["date"])[-1]["new"] if ev else world["platforms"][plat][key]


# ------------------------------------------------------------------------------------------------ documents


def row_label_value(n: str, k: str, v: float) -> tuple[str, str, str]:
    """(label, value text, unit) as written in the document; PER_1K platforms state prices per 1K tokens."""
    if n in PER_1K and k in ("price_in", "price_out"):
        return METRICS[k][0].replace("1M", "1K"), f"{v / 1000:.5f}", "USD"
    return METRICS[k][0], fmt_val(k, v), METRICS[k][1]


def render_platform(n: str, facts: dict[str, float], style: int, rng: random.Random) -> str:
    head = f"# {n}\n\n> Fictional sample document for AgentOS evaluation. All figures are invented. Figures are as of 2025-11-01 unless the changelog says otherwise.\n\n"
    if style == 0:  # bullets
        body = "## Specifications\n\n" + "\n".join(
            "- {}: {} {}".format(*row_label_value(n, k, facts[k])) for k in METRICS
        )
    elif style == 1:  # table
        body = "## Specifications\n\n| Metric | Value | Unit |\n|---|---|---|\n" + "\n".join(
            "| {} | {} | {} |".format(*row_label_value(n, k, facts[k])) for k in METRICS
        )
    else:  # prose
        body = "## Specifications\n\n" + " ".join(
            "{}{} is {} {}.".format("The " if k != "p95" else "", *row_label_value(n, k, facts[k]))
            for k in METRICS
        )
    near = ""
    if n in NAMES[::3]:
        near = f"\n\n## Operations\n\n- Log retention (application logs only): {rng.randrange(7, 60)} days\n- Peak ingress bandwidth: {rng.randrange(10, 80)} Gbps\n"
    return head + body + near + "\n"


FILLER = [
    "Capacity planning reviews happen quarterly and focus on GPU supply, queue depth and regional demand.",
    "Incident reviews are blameless and published to customers within five business days of resolution.",
    "Release trains ship weekly; hotfixes follow a separate path with an additional approval.",
    "Customer support tiers are staffed in three time zones; premium plans receive a named engineer.",
    "Model onboarding follows a checklist covering licensing, quantization quality and cold-start behavior.",
    "Observability covers request traces, token accounting and per-tenant error budgets.",
]


def render_handbook(world: dict[str, Any], rng: random.Random) -> str:
    parts = [
        "# Operations handbook\n\n> Fictional sample document for AgentOS evaluation. All figures are invented.\n"
    ]
    for i, n in enumerate(NAMES):
        parts.append(f"\n## {n} operations\n")
        for _ in range(3):
            parts.append(rng.choice(FILLER) + " " + rng.choice(FILLER) + "\n")
        if i % 1 == 0:
            parts.append(
                f"On-call escalation window for {n}: {world['oncall'][n]} minutes before a page escalates to the next tier.\n"
            )
        parts.append(rng.choice(FILLER) + "\n")
    return "".join(parts)


def render_changelog(world: dict[str, Any]) -> str:
    out = [
        "# Pricing changelog\n\n> Fictional sample document for AgentOS evaluation. The most recent entry for a platform is its current price.\n"
    ]
    for e in sorted(world["events"], key=lambda e: (e["date"], e["platform"])):
        label = METRICS[e["key"]][0]
        out.append(
            f"\n## {e['date']} - {e['platform']}\n\n- {label} changed from ${e['old']:.2f} to ${e['new']:.2f} USD.\n"
        )
    return "".join(out)


def build_docs(world: dict[str, Any], rng: random.Random) -> dict[str, str]:
    docs = {}
    for i, n in enumerate(NAMES):
        docs[f"hard-{n.split()[0].lower()}.md"] = render_platform(n, world["platforms"][n], i % 3, rng)
    docs["hard-changelog.md"] = render_changelog(world)
    docs["hard-handbook.md"] = render_handbook(world, rng)
    return docs


# ---------------------------------------------------------------------------------------------------- cases


def case(kind: str, task: str, expected: Any, *, numeric: bool, tol: float, tag: str) -> dict[str, Any]:
    cid = "hrd-" + hashlib.sha256(f"{kind}|{task}".encode()).hexdigest()[:8]
    fmt = ' Return only a JSON object {"answer": <value, or null if the documents do not state it>}.'
    cfg: dict[str, Any] = {
        "tool_correctness": {
            "required_tools": ["document_search"],
            "max_calls": 30,
            "max_identical_calls": 3,
        },
        "schema_validation": {
            "schema": {
                "type": "object",
                "required": ["answer"],
                "properties": {"answer": {"type": ["number", "string", "null"]}},
            }
        },
    }
    methods = ["schema_validation", "task_completion", "tool_correctness"]
    if numeric:
        methods.insert(1, "numeric_tolerance")
        cfg["numeric_tolerance"] = {"fields": {"answer": {"abs": tol}}}
    else:
        methods.insert(1, "exact_match")
        cfg["exact_match"] = {"fields": ["answer"], "normalize": "casefold"}
    return {
        "case_id": cid,
        "task": task + fmt,
        "input": {"family": "hard_ops", "kind": kind},
        "input_artifacts": [],
        "expected_output": {"answer": expected},
        "evaluation_methods": methods,
        "evaluator_config": cfg,
        "tags": ["hard_ops", kind, tag],
    }


def build_cases(world: dict[str, Any], rng: random.Random) -> list[dict[str, Any]]:
    guard = [0]

    def tick() -> bool:
        guard[0] += 1
        if guard[0] > 200_000:
            raise SystemExit("case generation did not converge (too few distinct questions for a kind)")
        return True

    P = world["platforms"]
    out: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(c: dict[str, Any]) -> None:
        if c["case_id"] not in seen:
            seen.add(c["case_id"])
            out.append(c)

    plain = [k for k in METRICS if k not in ("price_in", "price_out")]
    # lookup: stable (never changed) metrics
    while tick() and sum(c["tags"][1] == "lookup" for c in out) < KINDS["lookup"]:
        n, k = rng.choice(NAMES), rng.choice(plain)
        label, unit = METRICS[k]
        add(
            case(
                "lookup",
                f"What is the {label} of {n} (in {unit})? Answer from the workspace documents.",
                P[n][k],
                numeric=True,
                tol=0.01,
                tag="single",
            )
        )
    # compute_cost: uses the CURRENT prices
    while tick() and sum(c["tags"][1] == "compute_cost" for c in out) < KINDS["compute_cost"]:
        n = rng.choice(NAMES)
        a, b = rng.choice([1, 2, 3, 5, 8, 10]), rng.choice([1, 2, 4, 6, 10])
        cost = round(current(world, n, "price_in") * a + current(world, n, "price_out") * b, 4)
        add(
            case(
                "compute_cost",
                f"A workload sends {a} million input tokens and {b} million output tokens to {n}. Using the current prices in the documents, what is the total token cost in USD?",
                cost,
                numeric=True,
                tol=0.005,
                tag="arithmetic",
            )
        )
    # multi_hop: lowest p95 among platforms meeting an uptime bar
    tries = 0
    while sum(c["tags"][1] == "multi_hop" for c in out) < KINDS["multi_hop"] and tries < 4000:
        tries += 1
        group = rng.sample(NAMES, 4)
        bar = rng.choice([99.9, 99.95, 99.99])
        ok = [g for g in group if P[g]["uptime"] >= bar]
        if len(ok) < 2:
            continue
        ok.sort(key=lambda g: P[g]["p95"])
        if P[ok[0]]["p95"] == P[ok[1]]["p95"]:
            continue
        add(
            case(
                "multi_hop",
                f"Among {', '.join(group)}: which platform has the lowest p95 latency among those with an uptime SLA of at least {bar}%? Answer with the platform name only.",
                ok[0],
                numeric=False,
                tol=0,
                tag="filter",
            )
        )
    # conflict_latest: platforms with changelog events (current value, then "as of <date>" questions that need date logic)
    changed = sorted({(e["platform"], e["key"]) for e in world["events"]})
    for n, k in changed:
        if sum(c["tags"][1] == "conflict_latest" for c in out) >= KINDS["conflict_latest"] // 2:
            break
        label, _ = METRICS[k]
        add(
            case(
                "conflict_latest",
                f"What is the current {label} of {n} in USD? Prices change over time: use the most recent information in the documents.",
                current(world, n, k),
                numeric=True,
                tol=0.001,
                tag="current",
            )
        )
    while tick() and sum(c["tags"][1] == "conflict_latest" for c in out) < KINDS["conflict_latest"]:
        n, k = rng.choice(changed)
        label, _ = METRICS[k]
        day = f"2026-{rng.randrange(1, 12):02d}-{rng.randrange(1, 28):02d}"
        ev = [e for e in world["events"] if e["platform"] == n and e["key"] == k and e["date"] <= day]
        value = sorted(ev, key=lambda e: e["date"])[-1]["new"] if ev else world["platforms"][n][k]
        add(
            case(
                "conflict_latest",
                f"What was the {label} of {n} in USD on {day}? The platform document states launch prices (as of 2025-11-01); the pricing changelog lists later changes.",
                value,
                numeric=True,
                tol=0.001,
                tag="as-of-date",
            )
        )
    # aggregates over ALL platforms: exhaustive lookups, easy to get subtly wrong
    while tick() and sum(c["tags"][1] == "aggregate_count" for c in out) < KINDS["aggregate_count"]:
        up, rg = rng.choice([99.5, 99.9, 99.95, 99.99]), rng.choice([4, 6, 8, 10, 12])
        n_ok = sum(P[n]["uptime"] >= up and P[n]["regions"] >= rg for n in NAMES)
        if not 1 <= n_ok <= 8:
            continue
        add(
            case(
                "aggregate_count",
                f"How many of the {len(NAMES)} platforms in the documents have an uptime SLA of at least {up}% AND at least {rg} regions? Answer with a number.",
                n_ok,
                numeric=True,
                tol=0.01,
                tag="count-all",
            )
        )
    while tick() and sum(c["tags"][1] == "aggregate_sum" for c in out) < KINDS["aggregate_sum"]:
        up = rng.choice([99.9, 99.95, 99.99])
        key = rng.choice(["p50", "p95", "cold_start", "regions"])
        sel = [n for n in NAMES if P[n]["uptime"] >= up]
        if not 2 <= len(sel) <= 8:
            continue
        label, unit = METRICS[key]
        add(
            case(
                "aggregate_sum",
                f"What is the total of the {label} values ({unit}) across all platforms whose uptime SLA is at least {up}%?",
                sum(P[n][key] for n in sel),
                numeric=True,
                tol=0.01,
                tag="sum-filtered",
            )
        )
    while tick() and sum(c["tags"][1] == "cheapest_blend" for c in out) < KINDS["cheapest_blend"]:
        group = rng.sample(NAMES, 6)
        a, b = rng.choice([2, 5, 10, 20]), rng.choice([1, 3, 6, 12])
        costs = sorted(
            (round(current(world, g, "price_in") * a + current(world, g, "price_out") * b, 4), g)
            for g in group
        )
        if costs[1][0] - costs[0][0] < 0.05:
            continue
        add(
            case(
                "cheapest_blend",
                f"For a workload of {a} million input tokens and {b} million output tokens, which of {', '.join(group)} has the lowest total token cost using the CURRENT prices in the documents (note that some documents state prices per 1K tokens and the pricing changelog lists later changes)? Answer with the platform name only.",
                costs[0][1],
                numeric=False,
                tol=0,
                tag="blend-argmin",
            )
        )
    # ratio
    while tick() and sum(c["tags"][1] == "ratio" for c in out) < KINDS["ratio"]:
        a, b = rng.sample(NAMES, 2)
        add(
            case(
                "ratio",
                f"How many times higher is the p95 latency of {a} than that of {b}? Round to 2 decimal places.",
                round(P[a]["p95"] / P[b]["p95"], 2),
                numeric=True,
                tol=0.011,
                tag="ratio",
            )
        )
    # unanswerable with near misses
    while tick() and sum(c["tags"][1] == "unanswerable" for c in out) < KINDS["unanswerable"]:
        n = rng.choice(NAMES)
        label, unit = rng.choice(PHANTOM)
        add(
            case(
                "unanswerable",
                f"What is the {label} of {n} (in {unit})? Answer from the workspace documents.",
                None,
                numeric=True,
                tol=0.5,
                tag="near-miss" if n in NAMES[::3] else "absent",
            )
        )
    # buried
    while tick() and sum(c["tags"][1] == "buried" for c in out) < KINDS["buried"]:
        n = rng.choice(NAMES)
        add(
            case(
                "buried",
                f"In the operations handbook, how many minutes is the on-call escalation window for {n}?",
                world["oncall"][n],
                numeric=True,
                tol=0.5,
                tag="long-document",
            )
        )
    return out


def split(cases: list[dict[str, Any]], rng: random.Random) -> dict[str, list[dict[str, Any]]]:
    by_kind: dict[str, list[dict[str, Any]]] = {}
    for c in cases:
        by_kind.setdefault(c["tags"][1], []).append(c)
    res: dict[str, list[dict[str, Any]]] = {s: [] for s in SPLITS}
    for _kind, items in sorted(by_kind.items()):
        rng.shuffle(items)
        n = len(items)
        a = round(n * SPLITS["development"])
        b = round(n * (SPLITS["development"] + SPLITS["validation"]))
        res["development"] += items[:a]
        res["validation"] += items[a:b]
        res["test"] += items[b:]
    return res


# ------------------------------------------------------------------------------------------------ verify


def verify(world: dict[str, Any], docs: dict[str, str]) -> list[str]:
    """Re-derive facts from the rendered text with different code than the renderer; return discrepancies."""
    problems = []
    num = re.compile(r"-?\d[\d,]*(?:\.\d+)?")
    for n in NAMES:
        text = docs[f"hard-{n.split()[0].lower()}.md"]
        for k in METRICS:
            label = row_label_value(n, k, world["platforms"][n][k])[0]
            hits = [ln for ln in re.split(r"\n|(?<=\.) (?=[A-Z])", text) if label in ln]
            if len(hits) != 1:
                problems.append(f"{n}/{k}: label appears in {len(hits)} places")
                continue
            tail = hits[0].split(label, 1)[1]
            m = num.search(tail)
            got = float(m.group(0).replace(",", "")) if m else None
            if got is not None and n in PER_1K and k in ("price_in", "price_out"):
                got = round(got * 1000, 4)
            if got != float(world["platforms"][n][k]):
                problems.append(f"{n}/{k}: document says {got}, model says {world['platforms'][n][k]}")
    log = docs["hard-changelog.md"]
    for e in world["events"]:
        pat = re.compile(
            rf"## {e['date']} - {re.escape(e['platform'])}\n\n- {re.escape(METRICS[e['key']][0])} changed from \${e['old']:.2f} to \${e['new']:.2f} USD\."
        )
        if not pat.search(log):
            problems.append(f"changelog missing {e}")
    hb = docs["hard-handbook.md"]
    for n, mins in world["oncall"].items():
        if f"On-call escalation window for {n}: {mins} minutes" not in hb:
            problems.append(f"handbook missing {n}")
    return problems


def dumps(obj: Any) -> str:
    return json.dumps(obj, indent=2, sort_keys=False, ensure_ascii=False) + "\n"


def build() -> dict[str, str]:
    rng = random.Random(SEED)
    world = make_world(rng)
    docs = build_docs(world, random.Random(SEED + 1))
    problems = verify(world, docs)
    if problems:
        sys.exit("verification failed:\n" + "\n".join(problems[:20]))
    cases = build_cases(world, random.Random(SEED + 2))
    assert len(cases) == sum(KINDS.values()), len(cases)
    splits = split(cases, random.Random(SEED + 3))
    files: dict[str, str] = {f"documents/{n}": t for n, t in docs.items()}
    manifest: dict[str, Any] = {
        "name": "hard-ops",
        "version": VERSION,
        "seed": SEED,
        "cases": len(cases),
        "datasets": {},
        "documents": {},
    }
    for n, t in docs.items():
        manifest["documents"][n] = hashlib.sha256(t.encode()).hexdigest()
    for s, items in splits.items():
        ds = {
            "name": "hard-ops",
            "version": VERSION,
            "split": s,
            "description": "Hard multi-step questions over 12 fictional platform documents (conflicts, arithmetic, filtering, buried facts, near-miss abstention).",
            "cases": items,
        }
        text = dumps(ds)
        files[f"datasets/hard-ops.v{VERSION}.{s}.json"] = text
        manifest["datasets"][s] = {
            "cases": len(items),
            "sha256": hashlib.sha256(text.encode()).hexdigest(),
            "kinds": {k: sum(c["tags"][1] == k for c in items) for k in KINDS},
        }
    files["MANIFEST.json"] = dumps(manifest)
    return files


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()
    files = build()
    if args.check:
        bad = [f for f, t in files.items() if not (OUT / f).exists() or (OUT / f).read_text() != t]
        if bad:
            sys.exit("out of date: " + ", ".join(bad))
        print("hard-eval files are up to date")
        return
    for f, t in files.items():
        p = OUT / f
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(t)
    m = json.loads(files["MANIFEST.json"])
    print({s: d["cases"] for s, d in m["datasets"].items()}, "cases:", m["cases"])


if __name__ == "__main__":
    main()
