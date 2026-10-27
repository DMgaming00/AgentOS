"""Agent Arena end to end: real evaluation runs on real workers, ranked with statistics, with the test split locked."""

import pytest
from sqlalchemy import func, select

from agentos_core.arena import leaderboard
from agentos_core.arena.corpus import FAMILIES, corpus_status
from agentos_core.arena.runner import ArenaError, arena_summary, create_arena, start_final_test
from agentos_core.db.models import AgentDefinition, ArenaEntry, ArenaRun, EvaluationRun
from agentos_core.engine.tx import unit_of_work
from agentos_core.testing import running_workers, wait_for

pytestmark = pytest.mark.integration


async def version_ids(arena_seeded, names):
    async with unit_of_work() as s:
        out = []
        for n in names:
            a = await s.get(AgentDefinition, arena_seeded.agents[n].id)
            out.append(a.active_version_id)
        return out


async def wait_stage(arena_id, stage, timeout=300):
    async def check():
        async with unit_of_work() as s:
            summary = await arena_summary(s, await s.get(ArenaRun, arena_id))
            assert summary["stage"] != "failed", summary
            return summary if summary["stage"] == stage else None

    return await wait_for(check, timeout=timeout, interval=1.0)


async def test_arena_runs_ranks_and_locks_the_test_split(arena_seeded):
    names = ["Arena · Bare", "Arena · Careful", "Arena · Ungrounded"]
    vids = await version_ids(arena_seeded, names)
    async with unit_of_work() as s:
        arena = await create_arena(
            s,
            workspace_id=arena_seeded.workspace.id,
            user_id=arena_seeded.users["admin"].id,
            name="test arena",
            agent_version_ids=vids,
        )
        arena_id = arena.id

    async with unit_of_work() as s:
        # Only development and validation are scheduled at creation: no test-split evaluation exists yet.
        splits = (
            await s.execute(
                select(ArenaEntry.split, func.count())
                .where(ArenaEntry.arena_run_id == arena_id)
                .group_by(ArenaEntry.split)
            )
        ).all()
        assert dict(splits) == {"development": 9, "validation": 9}
    with pytest.raises(ArenaError) as early:
        async with unit_of_work() as s:
            await start_final_test(s, arena_id, user_id=None)
    assert early.value.code == "selection_not_complete"

    async with running_workers(3, concurrency=4):
        await wait_stage(arena_id, "selection_complete")
        async with unit_of_work() as s:
            sel = leaderboard.build_leaderboard(await leaderboard.load_scores(s, arena_id, "validation"))
        rows = {r["agent"]: r for r in sel["rows"]}
        # Careful has the calculator, citations and abstention; Ungrounded cannot search at all.
        assert rows["Arena · Careful"]["overall"]["score"] > rows["Arena · Bare"]["overall"]["score"]
        assert rows["Arena · Careful"]["overall"]["score"] > rows["Arena · Ungrounded"]["overall"]["score"]
        assert rows["Arena · Careful"]["rank"] == 1
        for r in rows.values():
            assert set(r["families"]) == set(FAMILIES)
            assert r["incomplete_executions"] == 0

        async with unit_of_work() as s:
            await start_final_test(s, arena_id, user_id=arena_seeded.users["admin"].id)
        with pytest.raises(ArenaError) as again:  # the test split is scored once
            async with unit_of_work() as s:
                await start_final_test(s, arena_id, user_id=None)
        assert again.value.code == "test_already_run"
        await wait_stage(arena_id, "completed")

    async with unit_of_work() as s:
        test = leaderboard.build_leaderboard(await leaderboard.load_scores(s, arena_id, "test"))
        assert len(test["rows"]) == 3
        cmp = leaderboard.selection_vs_test(sel, test)
        assert {c["agent"] for c in cmp} == set(names)
        assert (
            await s.execute(
                select(func.count())
                .select_from(ArenaEntry)
                .where(ArenaEntry.arena_run_id == arena_id, ArenaEntry.split == "test")
            )
        ).scalar_one() == 9
        # every execution of every entry is an ordinary evaluation run
        assert (
            await s.execute(
                select(func.count())
                .select_from(EvaluationRun)
                .join(ArenaEntry, ArenaEntry.evaluation_run_id == EvaluationRun.id)
                .where(ArenaEntry.arena_run_id == arena_id, EvaluationRun.status != "completed")
            )
        ).scalar_one() == 0
        status = await corpus_status(s, arena_seeded.workspace.id)
        assert status["all_verified"]
    print(
        "\nVALIDATION",
        [
            (
                r["agent"],
                r["rank"],
                round(r["overall"]["score"], 3),
                {f: round(v["rate"], 2) for f, v in r["families"].items()},
            )
            for r in sel["rows"]
        ],
    )
    print("TEST", [(r["agent"], r["rank"], round(r["overall"]["score"], 3)) for r in test["rows"]])
