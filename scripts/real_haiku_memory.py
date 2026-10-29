"""Memory Lab with a real Claude Haiku reader (needs live mode; see docs/ENABLE_REAL_CLAUDE.md).

    cd scripts && uv run python real_haiku_memory.py

Runs the six-strategy experiment on 3 seeds (default churn: 8 facts updated, 3 expired), each question answered once by
the real model, and writes the per-strategy metrics to benchmarks/results/real_haiku/memory.json.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from autopilot_demo import Client, say, wait

OUT = (
    Path(__file__).resolve().parent.parent
    / "benchmarks"
    / "results"
    / "real_haiku"
    / os.environ.get("OUT_NAME", "memory.json")
)
SEEDS = tuple(int(x) for x in os.environ.get("SEEDS", "1,2,3").split(","))
UPDATED, EXPIRED = os.environ.get("CHURN", "8,3").split(",")


def main() -> None:
    admin = Client("admin@agentos.dev", "agentos-admin")
    out: list[dict[str, Any]] = []
    for seed in SEEDS:
        exp = admin.post(
            f"/api/v1/memory/experiments?reader=live&seed={seed}&updated={UPDATED}&expired={EXPIRED}"
        )
        say(f"seed {seed}: experiment {exp['id'][:8]} running")
        done = wait(
            lambda exp=exp: (
                (e := admin.get(f"/api/v1/memory/experiments/{exp['id']}"))["status"] != "running" and e
            ),
            f"memory experiment seed {seed}",
            timeout=1500,
            interval=5,
        )
        say(f"  {done['status']}")
        out.append(
            {
                "seed": seed,
                "id": exp["id"],
                "status": done["status"],
                "results": done.get("results"),
                "config": done.get("config"),
            }
        )
    OUT.write_text(json.dumps({"reader": "claude-haiku-5-5", "runs": out}, indent=2, default=str))
    say(f"wrote {OUT}")


if __name__ == "__main__":
    main()
