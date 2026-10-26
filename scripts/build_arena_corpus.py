"""Build the Agent Arena corpus: three task families, three splits, a hash manifest.

Ground truth is computed here, from the source documents (``examples/research-report/documents``) and from Python
arithmetic. It is parsed with a deliberately different method than the agents use (split on ``:`` rather than the mock
provider's regular expression), so a bug in an agent's parser cannot also be a bug in the answer key.

Splits are assigned by a salted hash of each case's *fact set* (the identity of what is being asked), so two cases that
ask for exactly the same facts always land in the same split. The salt is the first one in a deterministic search that
gives every family at least ``MIN_PER_SPLIT`` cases in every split; it is recorded in the manifest. Facts themselves
(there are only about 26 in the documents) necessarily recur across splits in different combinations; the manifest
reports that overlap instead of hiding it.

Run: ``uv run python scripts/build_arena_corpus.py`` (rewrites examples/arena/). ``--check`` verifies the committed files
are exactly what the builder produces.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import re
import statistics
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "examples" / "research-report" / "documents"
OUT = ROOT / "examples" / "arena"
SPLITS = ("development", "validation", "test")
SPLIT_SHARE = {"development": 0.30, "validation": 0.30, "test": 0.40}
MIN_PER_SPLIT = {"development": 6, "validation": 6, "test": 8}
PLATFORM_FILE = {
    "Nimbus Inference": "platform-nimbus.md",
    "Helix Serve": "platform-helix.md",
    "Vector Forge": "platform-vectorforge.md",
}
LOWER_IS_BETTER = ("price", "cost", "latency", "cold start")
NUMBER = re.compile(r"-?\d[\d,]*(?:\.\d+)?")


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


# ----------------------------------------------------------------------------- ground truth from the documents


def parse_facts() -> dict[str, dict[str, dict[str, Any]]]:
    """platform -> label -> {value, unit, ambiguous}. A label stated twice with different values is ambiguous."""
    facts: dict[str, dict[str, dict[str, Any]]] = {}
    for platform, filename in PLATFORM_FILE.items():
        seen: dict[str, list[tuple[float, str]]] = {}
        for raw in (DOCS / filename).read_text().splitlines():
            line = raw.strip()
            if not line.startswith("- ") or ":" not in line:
                continue
            label, rest = line[2:].split(":", 1)
            m = NUMBER.search(rest)
            if not m:
                continue
            value = float(m.group(0).replace(",", ""))
            unit = (rest[m.end() :].replace("$", "").strip()) or (
                "USD" if "$" in rest else ""
            )
            if "$" in rest[: m.start()]:
                unit = rest[m.end() :].strip() or "USD"
            seen.setdefault(label.strip(), []).append((value, unit))
        facts[platform] = {
            label: {
                "value": entries[0][0],
                "unit": entries[0][1],
                "ambiguous": len({v for v, _ in entries}) > 1,
            }
            for label, entries in seen.items()
        }
    return facts


def clean(facts: dict[str, dict[str, dict[str, Any]]]) -> dict[str, dict[str, dict[str, Any]]]:
    return {p: {k: v for k, v in d.items() if not v["ambiguous"]} for p, d in facts.items()}


def number(value: float) -> int | float:
    return int(value) if float(value).is_integer() else value


def field_key(label: str, unit: str) -> str:
    return f"{slug(label)}_{slug(unit)}" if unit else slug(label)


def tolerance(unit: str) -> dict[str, float]:
    return {"abs": 0.001} if unit in ("USD", "%") else {"abs": 0.5}


# ----------------------------------------------------------------------------- family 1: numeric extraction


def extraction_cases(facts: dict[str, dict[str, dict[str, Any]]], rng: random.Random) -> list[dict[str, Any]]:
    all_labels = sorted({label for d in facts.values() for label in d})

    def field(platform: str, label: str) -> dict[str, Any]:
        unit = next(d[label]["unit"] for d in facts.values() if label in d)
        return {"key": field_key(label, unit), "label": label, "unit": unit, "subject": platform}

    def build(kind: str, fields: list[dict[str, Any]], tags: list[str]) -> dict[str, Any]:
        platforms = sorted({f["subject"] for f in fields})
        keyed = {}
        for f in fields:
            key = f["key"] if len(platforms) == 1 else f"{slug(f['subject'])}_{f['key']}"
            f["key"] = key
            present = facts[f["subject"]].get(f["label"])
            keyed[key] = number(present["value"]) if present else None
        names = ", ".join(f"{f['label']} ({f['unit']}) of {f['subject']}" for f in fields)
        task = (
            f"From the workspace documents, extract: {names}. Return a JSON object with the keys "
            f"{', '.join(f['key'] for f in fields)} and a 'citations' object mapping each key to the document "
            "section you used. Use null for any value the documents do not state."
        )
        answerable = [k for k, v in keyed.items() if v is not None]
        unit_by_key = {f["key"]: f["unit"] for f in fields}
        return {
            "family": "extraction",
            "kind": kind,
            "facts": sorted(f"{f['subject']}|{f['label']}" for f in fields),
            "case": {
                "task": task,
                "input": {
                    "family": "extraction",
                    "fields": fields,
                    "documents": [PLATFORM_FILE[p] for p in platforms],
                    **({"subject": platforms[0]} if len(platforms) == 1 else {}),
                },
                "input_artifacts": [PLATFORM_FILE[p] for p in platforms],
                "expected_output": keyed,
                "evaluation_methods": [
                    "schema_validation",
                    "numeric_tolerance",
                    "source_citation_check",
                    "tool_correctness",
                    "task_completion",
                ],
                "evaluator_config": {
                    "numeric_tolerance": {"fields": {k: tolerance(unit_by_key[k]) for k in keyed}},
                    "source_citation_check": {"fields": answerable},
                    "tool_correctness": {
                        "required_tools": ["document_search"],
                        "max_calls": 8,
                        "max_identical_calls": 2,
                    },
                    "task_completion": {"required_fields": list(keyed)},
                },
                "tags": ["arena", "extraction", kind],
            },
        }

    platforms = sorted(facts)
    singles = [(p, label) for p in platforms for label in sorted(facts[p])]
    rng.shuffle(singles)
    cases = [build("single", [field(p, label)], ["retrieval"]) for p, label in singles[:14]]
    for p in platforms:  # multi-field, one subject
        labels = sorted(facts[p])
        for _ in range(3):
            pick = sorted(rng.sample(labels, rng.choice([2, 3])))
            cases.append(build("multi_field", [field(p, label) for label in pick], ["retrieval"]))
    for _ in range(6):  # two subjects, one search each
        pair = sorted(rng.sample(platforms, 2))
        shared = sorted(set(facts[pair[0]]) & set(facts[pair[1]]))
        label = rng.choice(shared)
        cases.append(build("multi_subject", [field(p, label) for p in pair], ["retrieval", "multi_search"]))
    missing = [(p, label) for p in platforms for label in all_labels if label not in facts[p]]
    rng.shuffle(missing)
    for p, label in missing[:8]:
        cases.append(build("unanswerable", [field(p, label)], ["abstention"]))
    return dedupe(cases)


# ----------------------------------------------------------------------------- family 2: calculation


def calc(op: str, values: list[float]) -> float:
    if op == "sum":
        return float(sum(values))
    if op == "mean":
        return statistics.fmean(values)
    if op == "median":
        return float(statistics.median(values))
    if op == "min":
        return min(values)
    if op == "max":
        return max(values)
    if op == "stdev":
        return statistics.stdev(values)
    if op == "percent_change":
        return (values[1] - values[0]) / values[0] * 100
    if op == "ratio":
        return values[0] / values[1]
    raise ValueError(op)


def calculation_cases(facts: dict[str, dict[str, dict[str, Any]]], rng: random.Random) -> list[dict[str, Any]]:
    # Numbers come from the documents (so the arithmetic is about the same domain) plus a few synthetic ones.
    pool = sorted({d["value"] for p in facts.values() for d in p.values() if d["value"] > 0})
    cases: list[dict[str, Any]] = []

    def add(kind: str, input_: dict[str, Any], expected: float, key: str) -> None:
        cases.append(
            {
                "family": "calculation",
                "kind": kind,
                "facts": [key],
                "case": {
                    "task": f"Compute the requested quantity using exact arithmetic and return {{'result': <number>}}. "
                    f"Request: {json.dumps(input_, sort_keys=True)}",
                    "input": {"family": "calculation", **input_},
                    "input_artifacts": [],
                    "expected_output": {"result": round(expected, 9)},
                    "evaluation_methods": [
                        "schema_validation",
                        "numeric_tolerance",
                        "tool_correctness",
                        "task_completion",
                    ],
                    "evaluator_config": {
                        "numeric_tolerance": {"fields": {"result": {"rel": 1e-6, "abs": 1e-9}}},
                        "tool_correctness": {"max_calls": 4, "max_identical_calls": 2},
                    },
                    "tags": ["arena", "calculation", kind],
                },
            }
        )

    ops = ["sum", "mean", "median", "min", "max", "stdev", "percent_change", "ratio"]
    for i in range(22):
        op = ops[i % len(ops)]
        n = 2 if op in ("percent_change", "ratio") else rng.choice([3, 4, 5])
        values = sorted(rng.sample(pool, n)) if op not in ("percent_change", "ratio") else rng.sample(pool, 2)
        if op in ("percent_change", "ratio"):
            values = [float(v) for v in values]
        add(op, {"operation": op, "values": [float(v) for v in values]}, calc(op, [float(v) for v in values]), f"{op}:{values}")
    for _ in range(8):
        a, b, c = (rng.choice(pool) for _ in range(3))
        expr = rng.choice(
            [
                f"({number(a)} + {number(b)}) * {number(c)}",
                f"{number(a)} * {number(b)} - {number(c)}",
                f"({number(a)} + {number(b)}) / {number(c)}",
                f"{number(a)} ^ 2 + {number(b)}",
            ]
        )
        value = _eval(expr)
        add("expression", {"operation": "evaluate", "expression": expr}, value, f"evaluate:{expr}")
    return dedupe(cases)


def _eval(expr: str) -> float:
    # Independent reference evaluator: Python's own arithmetic on a whitelisted character set.
    if not re.fullmatch(r"[0-9.+\-*/^() ]+", expr):
        raise ValueError(expr)
    return float(eval(expr.replace("^", "**"), {"__builtins__": {}}, {}))  # noqa: S307


# ----------------------------------------------------------------------------- family 3: grounded question answering


def qa_cases(facts: dict[str, dict[str, dict[str, Any]]], rng: random.Random) -> list[dict[str, Any]]:
    platforms = sorted(facts)
    cases: list[dict[str, Any]] = []

    def add(kind: str, input_: dict[str, Any], expected: Any, fact_keys: list[str], question: str) -> None:
        answerable = expected is not None and kind != "comparison"
        methods = ["schema_validation", "task_completion", "tool_correctness"]
        cfg: dict[str, Any] = {
            "tool_correctness": {"required_tools": ["document_search"], "max_calls": 6, "max_identical_calls": 2}
        }
        if kind == "comparison":
            methods.insert(1, "exact_match")
        else:
            methods.insert(1, "numeric_tolerance")
            cfg["numeric_tolerance"] = {"fields": {"answer": tolerance(input_.get("unit", ""))}}
        methods.append("source_citation_check")
        cfg["source_citation_check"] = {"fields": ["answer"] if answerable else []}
        cases.append(
            {
                "family": "grounded_qa",
                "kind": kind,
                "facts": fact_keys,
                "case": {
                    "task": f"{question} Answer only from the workspace documents and return "
                    "{'answer': <value or null>, 'citations': {'answer': <section>}}. "
                    "If the documents do not state the answer, the answer is null.",
                    "input": {"family": "grounded_qa", "kind": kind, **input_},
                    "input_artifacts": [],
                    "expected_output": {"answer": expected},
                    "evaluation_methods": methods,
                    "evaluator_config": cfg,
                    "tags": ["arena", "grounded_qa", kind],
                },
            }
        )

    lookups = [(p, label) for p in platforms for label in sorted(facts[p])]
    rng.shuffle(lookups)
    for p, label in lookups[:14]:
        d = facts[p][label]
        add(
            "lookup",
            {"subject": p, "label": label, "unit": d["unit"]},
            number(d["value"]),
            [f"{p}|{label}"],
            f"What is the {label} of {p} (in {d['unit']})?",
        )
    all_labels = sorted({label for d in facts.values() for label in d})
    gaps = [(p, label) for p in platforms for label in all_labels if label not in facts[p]]
    rng.shuffle(gaps)
    for p, label in gaps[:10]:
        unit = next(d[label]["unit"] for d in facts.values() if label in d)
        add(
            "unanswerable",
            {"subject": p, "label": label, "unit": unit},
            None,
            [f"{p}|{label}"],
            f"What is the {label} of {p} (in {unit})?",
        )
    shared = [label for label in all_labels if sum(label in facts[p] for p in platforms) >= 2]
    for label in rng.sample(shared, min(10, len(shared))):
        holders = {p: facts[p][label]["value"] for p in platforms if label in facts[p]}
        lower = any(w in label.lower() for w in LOWER_IS_BETTER)
        ordered = sorted(holders.items(), key=lambda kv: kv[1])
        if ordered[0][1] == ordered[-1][1] or (len(ordered) > 1 and ordered[0][1] == ordered[1][1] and lower):
            continue
        best = ordered[0][0] if lower else ordered[-1][0]
        if not lower and len(ordered) > 1 and ordered[-1][1] == ordered[-2][1]:
            continue
        word = "lowest" if lower else "highest"
        add(
            "comparison",
            {"label": label, "direction": "lower_is_better" if lower else "higher_is_better"},
            best,
            sorted(f"{p}|{label}" for p in holders),
            f"Which platform has the {word} {label}?",
        )
    return dedupe(cases)


# ----------------------------------------------------------------------------- assembly


def dedupe(cases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    out = []
    for c in cases:
        ident = sha(json.dumps([c["family"], c["case"]["input"]], sort_keys=True))
        if ident in seen:
            continue
        seen.add(ident)
        out.append(c)
    return out


def assign_splits(entries: list[dict[str, Any]]) -> tuple[int, dict[str, list[dict[str, Any]]]]:
    families = sorted({e["family"] for e in entries})
    for salt in range(10_000):
        buckets: dict[str, list[dict[str, Any]]] = {s: [] for s in SPLITS}
        for e in entries:
            u = int(sha(f"{salt}|{e['family']}|{'+'.join(e['facts'])}|{e['kind']}")[:8], 16) / 0xFFFFFFFF
            split = "development" if u < 0.30 else "validation" if u < 0.60 else "test"
            e["split"] = split
            buckets[split].append(e)
        if all(
            sum(1 for e in buckets[s] if e["family"] == f) >= MIN_PER_SPLIT[s] for s in SPLITS for f in families
        ):
            return salt, buckets
    raise SystemExit("no salt satisfies the minimum split sizes")


DATASET_NAME = {"extraction": "arena-extraction", "calculation": "arena-calculation", "grounded_qa": "arena-grounded-qa"}
DESCRIPTION = {
    "extraction": "Extract numeric facts (with citations, or null when absent) from the fictional platform documents.",
    "calculation": "Exact arithmetic over numbers drawn from the same documents; tests whether the agent uses the calculator.",
    "grounded_qa": "Answer questions from the documents with citations; includes unanswerable and comparison questions.",
}


def dataset_hash(ds: dict[str, Any]) -> str:
    """Matches ``openeval.schema.dataset_hash``: name, version, split and cases sorted by id."""
    body = {
        "name": ds["name"],
        "version": ds["version"],
        "split": ds["split"],
        "cases": sorted(ds["cases"], key=lambda c: c["case_id"]),
    }
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def build() -> tuple[dict[str, str], dict[str, Any]]:
    facts = clean(parse_facts())
    rng = random.Random(20261026)
    entries = [
        *extraction_cases(facts, rng),
        *calculation_cases(facts, rng),
        *qa_cases(facts, rng),
    ]
    salt, buckets = assign_splits(entries)
    files: dict[str, str] = {}
    manifest_ds: list[dict[str, Any]] = []
    for family, name in DATASET_NAME.items():
        for split in SPLITS:
            members = sorted((e for e in buckets[split] if e["family"] == family), key=lambda e: e["case"]["task"])
            cases = []
            for e in members:
                cid = f"{family[:3]}-{sha(json.dumps(e['case']['input'], sort_keys=True))[:8]}"
                cases.append({"case_id": cid, **e["case"]})
            ds = {
                "name": name,
                "version": 1,
                "split": split,
                "description": DESCRIPTION[family],
                "cases": sorted(cases, key=lambda c: c["case_id"]),
            }
            fname = f"{name}.v1.{split}.json"
            files[fname] = json.dumps(ds, indent=1, sort_keys=False) + "\n"
            manifest_ds.append(
                {
                    "file": fname,
                    "name": name,
                    "family": family,
                    "split": split,
                    "version": 1,
                    "cases": len(cases),
                    "hash": dataset_hash(ds),
                    "kinds": {k: sum(1 for e in members if e["kind"] == k) for k in sorted({e["kind"] for e in members})},
                }
            )
    # Leakage report: identical fact sets across splits must be zero; partial fact overlap is reported, not hidden.
    by_split_facts: dict[str, set[str]] = {s: set() for s in SPLITS}
    by_split_sets: dict[str, set[tuple[str, tuple[str, ...]]]] = {s: set() for s in SPLITS}
    for e in entries:
        by_split_facts[e["split"]].update(e["facts"])
        by_split_sets[e["split"]].add((e["family"], tuple(e["facts"])))
    pairs = [("development", "validation"), ("development", "test"), ("validation", "test")]
    manifest = {
        "corpus": "agent-arena",
        "version": 1,
        "split_salt": salt,
        "split_share_target": SPLIT_SHARE,
        "source_documents": {
            f: sha((DOCS / f).read_text()) for f in sorted(PLATFORM_FILE.values())
        },
        "total_cases": sum(d["cases"] for d in manifest_ds),
        "datasets": manifest_ds,
        "leakage": {
            "identical_case_across_splits": 0,  # asserted below
            "identical_fact_set_across_splits": {
                f"{a}/{b}": len(by_split_sets[a] & by_split_sets[b]) for a, b in pairs
            },
            "distinct_facts_per_split": {s: len(by_split_facts[s]) for s in SPLITS},
            "facts_shared_between_splits": {
                f"{a}/{b}": len(by_split_facts[a] & by_split_facts[b]) for a, b in pairs
            },
            "note": "The documents contain about 26 distinct facts, so individual facts recur across splits in different "
            "combinations. Splits prevent tuning against the exact cases used for selection and the final test; they "
            "do not make the underlying facts novel.",
        },
        "excluded_ambiguous_facts": sorted(
            f"{p}|{label}" for p, d in parse_facts().items() for label, v in d.items() if v["ambiguous"]
        ),
    }
    ids: dict[str, str] = {}
    for fname, text in files.items():
        for c in json.loads(text)["cases"]:
            if c["case_id"] in ids:
                raise SystemExit(f"duplicate case id {c['case_id']} in {fname} and {ids[c['case_id']]}")
            ids[c["case_id"]] = fname
    if any(by_split_sets[a] & by_split_sets[b] for a, b in pairs):
        raise SystemExit("identical fact sets straddle splits")
    return files, manifest


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()
    files, manifest = build()
    files["MANIFEST.json"] = json.dumps(manifest, indent=1) + "\n"
    target = OUT / "datasets"
    if args.check:
        bad = [
            n
            for n, text in files.items()
            if not ((OUT if n == "MANIFEST.json" else target) / n).exists()
            or ((OUT if n == "MANIFEST.json" else target) / n).read_text() != text
        ]
        if bad:
            print("stale:", ", ".join(bad))
            sys.exit(1)
        print("arena corpus is up to date")
        return
    target.mkdir(parents=True, exist_ok=True)
    for n, text in files.items():
        (OUT if n == "MANIFEST.json" else target).joinpath(n).write_text(text)
    print(f"wrote {len(files) - 1} datasets, {manifest['total_cases']} cases, salt {manifest['split_salt']}")
    for d in manifest["datasets"]:
        print(f"  {d['name']:20s} {d['split']:12s} {d['cases']:3d}  {d['kinds']}")


if __name__ == "__main__":
    main()
