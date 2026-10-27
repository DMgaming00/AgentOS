"""FinOps API: analytics from real runs, budget management by role, anomalies, suggestions, labeling."""

import httpx2
import pytest

from agentos.main import app
from agentos_core.testing import running_workers, start_run, wait_run_status

pytestmark = pytest.mark.integration
FLOW = {
    "nodes": [
        {
            "key": "a",
            "type": "agent_task",
            "agent": "Research Agent",
            "tools": ["document_search"],
            "input": {"platform": "Nimbus Inference"},
        }
    ]
}


@pytest.fixture
async def client(seeded):
    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def login(client, email, password):
    r = await client.post("/api/v1/auth/login", json={"email": email, "password": password})
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


async def test_summary_from_real_runs_budgets_roles_anomalies_and_suggestions(client, seeded):
    admin = await login(client, "admin@agentos.dev", "agentos-admin")
    dev = await login(client, "developer@agentos.dev", "agentos-dev")
    viewer = await login(client, "viewer@agentos.dev", "agentos-viewer")

    empty = (await client.get("/api/v1/finops/summary", headers=viewer)).json()
    assert (
        empty["totals"]["calls"] == 0
        and empty["totals"]["estimated_cost_usd"] == 0
        and empty["label"].startswith("Estimated cost")
    )

    runs = [await start_run(seeded, FLOW) for _ in range(3)]
    async with running_workers(2):
        for r in runs:
            await wait_run_status(r, {"succeeded"}, timeout=60)
    s = (await client.get("/api/v1/finops/summary?days=7", headers=viewer)).json()
    assert (
        s["totals"]["calls"] >= 6
        and s["totals"]["estimated_cost_usd"] > 0
        and s["totals"]["simulated_share"] == 1.0
    )
    agent = next(a for a in s["by_agent"] if a["agent"] == "Research Agent")
    assert agent["successful_steps"] == 3 and agent["cost_per_successful_step_usd"] == pytest.approx(
        agent["estimated_cost_usd"] / 3
    )
    assert sum(w["succeeded_runs"] for w in s["by_workflow"]) == 3 and s["pricing"]["label"].startswith(
        "Estimated cost — simulated pricing"
    )
    assert any(t["tool"] == "document_search" and t["calls"] >= 3 for t in s["tools"])
    assert len(s["daily"]) == 7 and s["by_purpose"]["run"] > 0
    spent = s["totals"]["estimated_cost_usd"]

    body = {"scope": "workspace", "period": "daily", "limit_usd": "5.00", "mode": "enforce"}
    assert (
        await client.post("/api/v1/finops/budgets", headers=dev, json=body)
    ).status_code == 403  # spending controls are admin-only
    created = await client.post("/api/v1/finops/budgets", headers=admin, json=body)
    assert created.status_code == 201 and created.json()["limit_usd"] == 5.0
    assert (await client.post("/api/v1/finops/budgets", headers=admin, json=body)).status_code == 409
    assert (
        await client.post("/api/v1/finops/budgets", headers=admin, json={**body, "scope": "agent"})
    ).status_code == 422
    bid = created.json()["id"]
    util = (await client.get("/api/v1/finops/budgets", headers=viewer)).json()["items"][0]
    assert util["spent_usd"] == pytest.approx(spent) and util["state"] == "ok"
    patched = await client.patch(
        f"/api/v1/finops/budgets/{bid}", headers=admin, json={"limit_usd": "0.000001"}
    )
    assert patched.json()["state"] == "exceeded"
    assert (
        await client.patch(f"/api/v1/finops/budgets/{bid}", headers=admin, json={"limit_usd": "0.00000001"})
    ).status_code == 422  # below the 6-decimal precision
    assert (
        await client.patch(f"/api/v1/finops/budgets/{bid}", headers=dev, json={"enabled": False})
    ).status_code == 403

    # anomalies: nothing unusual in three healthy runs; detection is idempotent
    assert (await client.post("/api/v1/finops/anomalies/detect", headers=viewer)).status_code == 403
    det = (await client.post("/api/v1/finops/anomalies/detect", headers=dev)).json()
    again = (await client.post("/api/v1/finops/anomalies/detect", headers=dev)).json()
    assert det["found"] == again["found"] and again["new"] == 0
    listing = (await client.get("/api/v1/finops/anomalies", headers=viewer)).json()
    assert listing["thresholds"]["spend_ratio"] == 2.0

    sug = (await client.get("/api/v1/finops/suggestions", headers=viewer)).json()
    assert sug["label"].startswith("Estimated cost") and all(
        i["savings"]["basis"] in ("measured", "projected", "none") for i in sug["items"]
    )

    assert (await client.delete(f"/api/v1/finops/budgets/{bid}", headers=admin)).status_code == 200
    assert (await client.get("/api/v1/finops/budgets", headers=viewer)).json()["items"] == []
