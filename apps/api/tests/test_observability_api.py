"""Telemetry, workers and the run inspector computed from real stored records; one run, one id, every view."""

import httpx2
import pytest
from sqlalchemy import select

from agentos.main import app
from agentos_core.autopilot.demo import EXTRACTION_AGENT
from agentos_core.autopilot.evaluation import create_evaluation_run
from agentos_core.autopilot.failures import analyze
from agentos_core.autopilot.rules import generate_rule_candidates
from agentos_core.db.models import AgentConfigVersion, EvaluationRun, WorkflowRun
from agentos_core.demo import research_report_workflow
from agentos_core.engine.tx import unit_of_work
from agentos_core.testing import pending_approvals, running_workers, start_run, wait_run_status
from agentos_core.testing_autopilot import dataset, wait_row

pytestmark = pytest.mark.integration


@pytest.fixture
async def client(seeded):
    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def login(client, email="viewer@agentos.dev", password="agentos-viewer"):
    r = await client.post("/api/v1/auth/login", json={"email": email, "password": password})
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest.mark.timeout(300)
async def test_telemetry_workers_and_inspector_from_real_runs(client, seeded):
    h = await login(client)
    admin = await login(client, "admin@agentos.dev", "agentos-admin")
    ws, dev = seeded.workspace.id, seeded.users["developer"].id
    agent = seeded.agents[EXTRACTION_AGENT]
    report = await start_run(seeded, research_report_workflow(), name="report")
    async with running_workers(2, concurrency=6):
        await wait_run_status(report, {"waiting_approval"}, timeout=60)
        (approval,) = await pending_approvals(report)
        ok = await client.post(
            f"/api/v1/approvals/{approval.id}/approve", headers=admin, json={"reason": "ok"}
        )
        assert ok.status_code == 200, ok.text
        await wait_run_status(report, {"succeeded"}, timeout=60)
        async with unit_of_work() as s:
            v1 = await s.get(AgentConfigVersion, agent.active_version_id)
            ev = await create_evaluation_run(
                s,
                agent=agent,
                version=v1,
                dataset=await dataset(ws, "development"),
                trials=1,
                created_by=dev,
                max_concurrency=6,
                per_case_max_cost_usd=0.25,
            )
        await wait_row(EvaluationRun, ev.id, {"completed"}, 90)
    async with unit_of_work() as s:
        await analyze(s, ws, window_hours=1)
    async with unit_of_work() as s:
        await generate_rule_candidates(s, workspace_id=ws, agent_id=agent.id, diagnosis_id=None, user_id=dev)

    t = (await client.get("/api/v1/metrics/telemetry?days=1", headers=h)).json()
    assert t["workflow_duration_s"]["n"] >= 1 and t["queue_latency_ms"]["n"] > 5
    assert sum(b["count"] for b in t["queue_latency_ms"]["histogram"]) == t["queue_latency_ms"]["n"]
    assert (
        t["database_latency"]["kind"] == "probe" and t["database_latency"]["select_1_ms"]["p50"] is not None
    )
    assert (
        "mock/mock-research" in t["model_latency_ms"]["by_model"] or t["model_latency_ms"]["overall"]["n"] > 0
    )
    assert "document_search" in t["tool_latency_ms"] and t["tool_latency_ms"]["document_search"]["n"] >= 1
    assert t["approval_wait_s"]["n"] == 1 and t["approval_wait_s"]["pending"] == 0
    assert t["tokens"]["calls"] > 0 and t["estimated_cost"]["label"].startswith("Estimated cost")
    assert t["rates"]["run_failure_rate"] is not None and 0 <= t["rates"]["retry_rate"] <= 1
    assert any(e["tool"] == "document_search" and e["calls"] >= 1 for e in t["dependency_graph"])
    assert any(
        row["agent"] == EXTRACTION_AGENT and row["error"] == "invalid_model_output"
        for row in t["failure_heatmap"]
    )
    assert t["evaluations"][0]["agent"] == EXTRACTION_AGENT and t["evaluations"][0]["success"] is not None

    w = (await client.get("/api/v1/workers", headers=h)).json()
    assert w["summary"]["total"] >= 1 and w["items"][0]["recent"]["attempts"] >= 1

    # the inspector for the approval run: every view is built from the same run id
    ins = (await client.get(f"/api/v1/runs/{report}/inspector", headers=h)).json()
    assert ins["run"]["id"] == str(report) and ins["run"]["environment"] == "production"
    kinds = {s["kind"] for s in ins["spans"]}
    assert {"run", "step", "attempt", "model_call", "tool_call", "approval"} <= kinds
    ids = {s["id"] for s in ins["spans"]}
    assert all(s["parent"] in ids for s in ins["spans"] if s["kind"] != "run")  # a connected tree, no orphans
    assert abs(ins["cost"]["total_usd"] - sum(x["usd"] for x in ins["cost"]["by_step"])) < 1e-9
    assert ins["integrity"]["gap_free"] and ins["integrity"]["monotonic"] and ins["integrity"]["events"] > 5
    assert (
        ins["versions"] and ins["versions"][0]["role_now"] == "active" and ins["versions"][0]["deployments"]
    )
    assert ins["links"]["flight_recorder"] == f"/flight/{report}"
    assert any(x["tool"] == "artifact_writer" and x["requires_approval"] for x in ins["tools"])

    # a failed evaluation run: evaluation linkage, failure signals and candidates all point at it
    async with unit_of_work() as s:
        failed = (
            await s.execute(
                select(WorkflowRun.id)
                .where(WorkflowRun.purpose == "evaluation", WorkflowRun.status == "failed")
                .limit(1)
            )
        ).scalar_one()
    fi = (await client.get(f"/api/v1/runs/{failed}/inspector", headers=h)).json()
    assert (
        fi["evaluation"]
        and fi["evaluation"][0]["passed"] is False
        and fi["evaluation"][0]["dataset"].startswith("numeric-extraction/")
    )
    assert fi["failure"]["signals"] and fi["failure"]["signals"][0]["category"] == "OUTPUT_SCHEMA_INVALID"
    assert any(c["generator"] == "rule_engine" for c in fi["candidates"])
    assert (await client.get(f"/api/v1/runs/{ins['run']['id']}/inspector")).status_code == 401
