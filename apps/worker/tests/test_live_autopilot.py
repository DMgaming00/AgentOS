"""OPT-IN smoke test: Diagnosis Agent and Optimizer against a real provider (costs real money).

(Also requires the four execution flags: see docs/ENABLE_REAL_CLAUDE.md; offline mode blocks these tests by design.)

AGENTOS_LIVE_TESTS=1 ANTHROPIC_API_KEY=... uv run pytest apps/worker/tests/test_live_autopilot.py -m live
AGENTOS_LIVE_TESTS=1 OPENAI_API_KEY=... AGENTOS_AUTOPILOT_OPENAI_MODEL=<model> AUTOPILOT_LIVE_PROVIDER=openai uv run pytest ...

The evaluated agent stays on the mock provider; only diagnosis and candidate generation call the
real model. The assertions check the safety contract, not answer quality: the output is stored either
as a validated hypothesis or as ``invalid`` (never trusted), and every candidate is either policy-valid or
rejected with recorded violations. Spend is capped by the workspace policy's per-cycle limit.
"""

import os

import pytest
from sqlalchemy import select

from agentos_core.autopilot.demo import EXTRACTION_AGENT
from agentos_core.autopilot.diagnosis import STANDARD_LIMITATION, allowed_ids, diagnose, evidence_pack
from agentos_core.autopilot.evaluation import create_evaluation_run
from agentos_core.autopilot.failures import analyze
from agentos_core.autopilot.optimizer import generate_candidates
from agentos_core.db.models import AgentConfigVersion, EvaluationDataset, EvaluationRun, ModelUsage
from agentos_core.engine.tx import unit_of_work
from agentos_core.testing import running_workers, wait_for

PROVIDER = os.environ.get("AUTOPILOT_LIVE_PROVIDER", "anthropic")
pytestmark = [
    pytest.mark.live,
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("AGENTOS_LIVE_TESTS") != "1",
        reason="live provider tests are opt-in (AGENTOS_LIVE_TESTS=1)",
    ),
    pytest.mark.skipif(
        not os.environ.get("ANTHROPIC_API_KEY" if PROVIDER == "anthropic" else "OPENAI_API_KEY"),
        reason=f"no API key for {PROVIDER}",
    ),
]


@pytest.mark.timeout(300)
async def test_live_diagnosis_and_candidates_respect_the_contract(seeded):
    ws, dev = seeded.workspace.id, seeded.users["developer"].id
    agent = seeded.agents[EXTRACTION_AGENT]
    async with running_workers(1, concurrency=4):
        async with unit_of_work() as s:
            dataset = (
                await s.execute(
                    select(EvaluationDataset).where(
                        EvaluationDataset.workspace_id == ws, EvaluationDataset.split == "development"
                    )
                )
            ).scalar_one()
            v1 = await s.get(AgentConfigVersion, agent.active_version_id)
            ev = await create_evaluation_run(
                s,
                agent=agent,
                version=v1,
                dataset=dataset,
                trials=1,
                created_by=dev,
                max_concurrency=4,
                per_case_max_cost_usd=0.25,
            )

        async def done():
            async with unit_of_work() as s:
                e = await s.get(EvaluationRun, ev.id)
                return e.status == "completed"

        await wait_for(done, timeout=90)
    async with unit_of_work() as s:
        await analyze(s, ws, window_hours=2)
        pack = await evidence_pack(s, ws, agent.id, "OUTPUT_SCHEMA_INVALID")
        d = await diagnose(
            s,
            workspace_id=ws,
            agent_id=agent.id,
            category="OUTPUT_SCHEMA_INVALID",
            user_id=dev,
            provider=PROVIDER,
        )
    assert d.status in ("hypothesis", "invalid")
    assert STANDARD_LIMITATION in d.limitations
    if d.status == "hypothesis":
        assert set(d.evidence_refs) <= allowed_ids(pack)
    async with unit_of_work() as s:
        cands = await generate_candidates(
            s, workspace_id=ws, agent_id=agent.id, diagnosis_id=d.id, user_id=dev, provider=PROVIDER
        )
        usage = (await s.execute(select(ModelUsage).where(ModelUsage.autopilot_ref == d.id))).scalars().all()
    for c in cands:
        assert (c.status == "proposed" and c.candidate_version_id and not c.policy_violations) or (
            c.status == "rejected_policy" and c.policy_violations and c.candidate_version_id is None
        )
    assert usage and all(u.provider == PROVIDER and u.input_tokens for u in usage)
    print(
        f"live diagnosis={d.status} hypothesis={d.hypothesis!r} candidates={[(c.title, c.status) for c in cands]}"
    )
