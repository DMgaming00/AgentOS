"""Generate the numeric-extraction OpenEval suite from the bundled (fictional) platform documents.

Expected values are copied from examples/research-report/documents/platform-*.md. Helix Serve
does not publish a cold-start time, so its expected value is null (an agent must not invent one).
Helix's uptime SLA conflicts between two sections and is deliberately excluded.

    uv run python scripts/build_autopilot_datasets.py   # writes examples/autopilot/datasets/*.json
"""

from __future__ import annotations

import json
from pathlib import Path

from openeval import Case, Dataset

OUT = Path(__file__).resolve().parents[1] / "examples" / "autopilot" / "datasets"
FIELDS = {
    "input_price": ("Price per 1M input tokens", "USD", "price_per_1m_input_tokens_usd"),
    "output_price": ("Price per 1M output tokens", "USD", "price_per_1m_output_tokens_usd"),
    "gpu_hour": ("Dedicated GPU hour", "USD", "dedicated_gpu_hour_usd"),
    "p50": ("Median latency (p50)", "ms", "p50_latency_ms"),
    "p95": ("p95 latency", "ms", "p95_latency_ms"),
    "throughput": ("Throughput per GPU", "tokens/s", "throughput_tokens_per_s"),
    "cold_start": ("Cold start time", "s", "cold_start_s"),
    "context": ("Max context window", "tokens", "max_context_tokens"),
}
VALUES = {
    "Nimbus Inference": {
        "input_price": 0.30,
        "output_price": 1.20,
        "gpu_hour": 2.90,
        "p50": 180,
        "p95": 420,
        "throughput": 2400,
        "cold_start": 9,
        "context": 128000,
    },
    "Helix Serve": {
        "input_price": 0.22,
        "output_price": 0.95,
        "gpu_hour": 3.40,
        "p50": 140,
        "p95": 300,
        "throughput": 3100,
        "cold_start": None,
        "context": 200000,
    },
    "Vector Forge": {
        "input_price": 0.15,
        "output_price": 0.60,
        "gpu_hour": 1.90,
        "p50": 260,
        "p95": 900,
        "throughput": 3600,
        "cold_start": 25,
        "context": 64000,
    },
}
DOCS = {
    "Nimbus Inference": "platform-nimbus.md",
    "Helix Serve": "platform-helix.md",
    "Vector Forge": "platform-vectorforge.md",
}
PAIRS = [
    ("p95", "output_price"),
    ("p50", "throughput"),
    ("input_price", "context"),
    ("gpu_hour", "p50"),
    ("cold_start", "context"),
    ("output_price", "input_price"),
    ("p95", "context"),
    ("throughput", "gpu_hour"),
    ("p50", "p95"),
    ("cold_start", "throughput"),
    ("input_price", "p95"),
]


def make_case(i: int, subject: str, pair: tuple[str, str]) -> Case:
    fields = [
        {"key": FIELDS[f][2], "label": FIELDS[f][0], "unit": FIELDS[f][1], "subject": subject} for f in pair
    ]
    expected = {FIELDS[f][2]: VALUES[subject][f] for f in pair}
    tolerances = {FIELDS[f][2]: {"abs": 0.001 if FIELDS[f][1] == "USD" else 0.5} for f in pair}
    task = (
        f"From the workspace documents, extract for {subject}: "
        + "; ".join(f"{fd['label']} ({fd['unit']})" for fd in fields)
        + ". Return a JSON object with the keys "
        + ", ".join(expected)
        + " and a 'citations' object mapping each key to the document section you used. Use null for any "
        "value the documents do not state."
    )
    tags = ["numerical_extraction", "retrieval"]
    if any(FIELDS[f][1] == "USD" for f in pair):
        tags.append("currency")
    if any(v is None for v in expected.values()):
        tags.append("missing_evidence")
    return Case(
        case_id=f"numx-{i:03d}",
        task=task,
        input={"subject": subject, "fields": fields, "documents": [DOCS[subject]]},
        input_artifacts=[DOCS[subject]],
        expected_output=expected,
        evaluation_methods=[
            "schema_validation",
            "numeric_tolerance",
            "source_citation_check",
            "tool_correctness",
            "task_completion",
        ],
        evaluator_config={
            "numeric_tolerance": {"fields": tolerances},
            "tool_correctness": {
                "required_tools": ["document_search"],
                "max_calls": 6,
                "max_identical_calls": 2,
            },
        },
        tags=tags,
    )


def main() -> None:
    cases = [
        make_case(i, subject, pair)
        for i, (subject, pair) in enumerate(((s, p) for p in PAIRS for s in VALUES), start=1)
    ]
    # Deterministic, disjoint splits: 1 in 4 to development, the rest alternate validation/test.
    splits: dict[str, list[Case]] = {"development": [], "validation": [], "test": []}
    for i, case in enumerate(cases):
        splits["development" if i % 4 == 0 else ("validation" if i % 2 else "test")].append(case)
    OUT.mkdir(parents=True, exist_ok=True)
    for split, items in splits.items():
        ds = Dataset(
            name="numeric-extraction",
            version=1,
            split=split,  # type: ignore[arg-type]
            description="Extract numeric facts with units and citations from fictional platform docs.",
            cases=items,
        )
        path = OUT / f"numeric-extraction.v1.{split}.json"
        path.write_text(json.dumps(ds.model_dump(mode="json"), indent=2) + "\n")
        print(f"{path.name}: {len(items)} cases, hash {ds.content_hash()[:12]}")


if __name__ == "__main__":
    main()
