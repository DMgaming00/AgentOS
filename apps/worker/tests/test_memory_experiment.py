"""Memory strategy experiment end to end: real store writes, real retrieval, graded workflow runs, measured outcomes."""

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from agentos_core.db.models import MemoryAuditEvent, MemoryExperiment, MemoryRecord
from agentos_core.engine.tx import unit_of_work
from agentos_core.memory import experiments as mx
from agentos_core.testing import running_workers, wait_for

pytestmark = pytest.mark.integration


async def run_experiment(seeded, seed=7):
    async with unit_of_work() as s:
        exp = await mx.start_experiment(
            s, workspace_id=seeded.workspace.id, user_id=seeded.users["admin"].id, seed=seed
        )
        exp_id = exp.id

    async def done():
        async with unit_of_work() as s:
            e = await s.get(MemoryExperiment, exp_id)
            return e if e.status != "running" else None

    async with running_workers(2, concurrency=4):
        return await wait_for(done, timeout=240, interval=1.0)


async def test_strategies_are_measured_not_assumed(seeded):
    exp = await run_experiment(seeded)
    assert exp.status == "completed", exp.error
    r = exp.results["strategies"]
    assert set(r) == set(mx.STRATEGIES)
    acc = {k: v["accuracy"]["value"] for k, v in r.items()}
    assert all(v["accuracy"]["denominator"] == exp.results["tasks"] for v in r.values())

    # Without memory only "unknown" answers can be right; short-term context adds the in-conversation facts.
    assert acc["none"] < acc["short_term"] < acc["persistent_full"]
    assert r["none"]["by_kind"]["current"]["rate"] == 0 and r["short_term"]["by_kind"]["inline"]["rate"] == 1
    # Stale memory: summaries that were not refreshed return superseded and expired values; current stores do not.
    assert (
        r["summarized_stale"]["stale_memory_errors"]["count"] == 11
    )  # 8 updated + 3 expired facts in this scenario
    for strategy in ("persistent_full", "summarized_refreshed", "retrieval_top3", "none", "short_term"):
        assert r[strategy]["stale_memory_errors"]["count"] == 0, strategy
    assert acc["summarized_refreshed"] > acc["summarized_stale"]
    # Memory does not always help: every strategy that stores the sibling reuses it for the look-alike entities.
    assert r["persistent_full"]["incorrect_memory_reuse"]["count"] > 0
    assert (
        r["none"]["incorrect_memory_reuse"]["count"]
        == r["short_term"]["incorrect_memory_reuse"]["count"]
        == 0
    )
    # Retrieval reaches the same accuracy as dumping everything with far less context and higher precision.
    assert r["retrieval_top3"]["context"]["mean_chars"] < r["persistent_full"]["context"]["mean_chars"] / 3
    assert (
        r["retrieval_top3"]["retrieval_precision"]["value"]
        > r["persistent_full"]["retrieval_precision"]["value"]
    )
    assert r["retrieval_top3"]["answer_recall"]["value"] == 1.0
    # Paired statistics are present and the sealed experiment cannot change.
    comp = {(c["candidate"], c["baseline"]): c for c in exp.results["comparisons"]}
    assert comp[("persistent_full", "short_term")]["significant"] is True
    assert comp[("retrieval_top3", "persistent_full")]["significant"] is False
    with pytest.raises(DBAPIError):
        async with unit_of_work() as s:
            await s.execute(text("UPDATE memory_experiments SET error='x'"))

    async with unit_of_work() as s:
        ns = exp.config["namespaces"]["main"]
        recs = (await s.execute(select(MemoryRecord).where(MemoryRecord.namespace == ns))).scalars().all()
        assert len(recs) == exp.config["facts"]
        assert {rec.deleted_reason for rec in recs} == {None, "expired"}
        assert sum(1 for rec in recs if rec.version == 2 and rec.deleted_reason is None) == 8  # the updates
        searches = (
            (await s.execute(select(MemoryAuditEvent).where(MemoryAuditEvent.action == "search")))
            .scalars()
            .all()
        )
        assert len(searches) == exp.results["tasks"] and all(a.actor_type == "agent" for a in searches)


async def test_same_seed_gives_identical_outcomes(seeded):
    a = await run_experiment(seeded)
    b = await run_experiment(seeded)
    for strategy in mx.STRATEGIES:
        for key in ("accuracy", "errors", "by_kind", "retrieval_precision", "answer_recall", "context"):
            assert a.results["strategies"][strategy][key] == b.results["strategies"][strategy][key], (
                strategy,
                key,
            )
