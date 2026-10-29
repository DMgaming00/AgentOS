"""Agent Arena with real Claude Haiku contenders (needs live mode, a key and a spend cap; see docs/ENABLE_REAL_CLAUDE.md).

    cd scripts && uv run python real_haiku_arena.py

Creates Haiku versions of the sample contenders (same instructions, tools and step limits as the offline ones), runs the
Arena on the 111-case corpus (development + validation, then the one-shot test split) and writes the leaderboards to
benchmarks/results/real_haiku/arena.json. One trial per case; real models are nondeterministic, so ties and small gaps
are expected. Spend is capped by AGENTOS_MAX_REAL_API_SPEND_USD.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from autopilot_demo import Client, say, wait

from agentos_core.arena.contenders import CONTENDERS

MODEL = "claude-haiku-5-5"
USE = ("Arena · Bare", "Arena · Formatter", "Arena · Calculator", "Arena · Careful", "Arena · Ungrounded")
OUT = Path(__file__).resolve().parent.parent / "benchmarks" / "results" / "real_haiku" / "arena.json"


def main() -> None:
    admin = Client("admin@agentos.dev", "agentos-admin")
    stamp = time.strftime("%m%d-%H%M")
    versions: list[str] = []
    for spec in CONTENDERS:
        if spec["name"] not in USE:
            continue
        agent = admin.post(
            "/api/v1/agents",
            {
                "name": spec["name"].replace("Arena · ", "Arena · Haiku · ") + f" {stamp}",
                "description": f"Real-model Arena contender (Claude Haiku). {spec['description']}",
                "system_instructions": spec["system"],
                "provider": "anthropic",
                "model": MODEL,
                "model_configuration": {},
                "allowed_tool_ids": spec["tools"],
                "max_steps": spec["max_steps"],
                "max_cost_usd": "0.5",
                "timeout_seconds": 180,
            },
        )
        versions.append(agent["active_version_id"])
        say(f"created {agent['name']}")

    arena = admin.post(
        "/api/v1/arena/runs",
        {"name": f"Real Haiku arena {stamp}", "agent_version_ids": versions, "allow_live": True},
    )
    say(f"arena {arena['id'][:8]} started (selection splits)")
    wait(
        lambda: (a := admin.get(f"/api/v1/arena/runs/{arena['id']}"))["stage"] == "selection_complete" and a,
        "selection splits",
        timeout=3000,
        interval=10,
    )
    say("selection complete; scoring the test split once")
    admin.post(f"/api/v1/arena/runs/{arena['id']}/final-test")
    final: dict[str, Any] = wait(
        lambda: (a := admin.get(f"/api/v1/arena/runs/{arena['id']}"))["stage"] == "completed" and a,
        "test split",
        timeout=3000,
        interval=10,
    )
    OUT.write_text(json.dumps({"model": MODEL, "arena": final}, indent=2, default=str))
    say(f"wrote {OUT}")


if __name__ == "__main__":
    main()
