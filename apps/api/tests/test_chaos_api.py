"""Chaos Lab API: catalog, scenarios, experiment lifecycle, authorization, idempotency, comparison."""

import uuid

import httpx2
import pytest

from agentos.main import app
from agentos_core.db.models import User, Workspace, WorkspaceMember
from agentos_core.engine.tx import unit_of_work
from agentos_core.security_passwords import hash_password
from agentos_core.testing import running_workers, save_workflow, wait_for

pytestmark = pytest.mark.integration
FAST = {"max_attempts": 3, "backoff_base_seconds": 0.1, "backoff_max_seconds": 0.2}
WF = {
    "nodes": [
        {
            "key": "r1",
            "type": "agent_task",
            "agent": "Research Agent",
            "tools": ["document_search"],
            "input": {"platform": "Nimbus Inference"},
            "retry": FAST,
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


@pytest.fixture
async def roles(client):
    return {
        "dev": await login(client, "developer@agentos.dev", "agentos-dev"),
        "viewer": await login(client, "viewer@agentos.dev", "agentos-viewer"),
    }


async def test_catalog_scenarios_and_validation(client, roles):
    cat = (await client.get("/api/v1/chaos/catalog", headers=roles["viewer"])).json()
    types = {f["type"] for f in cat["faults"]}
    assert len(types) == 17 and {"worker_crash", "provider_timeout", "malicious_tool_result"} <= types
    assert all(f["expected_platform_response"] and f["points"] for f in cat["faults"])
    assert set(cat["metrics"]) >= {
        "task_completion_rate",
        "duplicate_side_effect_prevention",
        "state_consistency",
    }
    assert all(m["denominator"] for m in cat["metrics"].values()) and "No combined" in " ".join(cat["notes"])
    sc = (await client.get("/api/v1/chaos/scenarios", headers=roles["viewer"])).json()
    assert {s["name"] for s in sc} >= {"provider-brownout", "worker-crash-loop", "hostile-tool-output"}
    assert all(s["builtin"] for s in sc)

    plan = {"faults": [{"type": "tool_failure", "probability": 0.5}], "trials": 5}
    assert (
        await client.post(
            "/api/v1/chaos/scenarios", headers=roles["viewer"], json={"name": "mine", "plan": plan}
        )
    ).status_code == 403
    ok = await client.post(
        "/api/v1/chaos/scenarios", headers=roles["dev"], json={"name": "mine", "plan": plan}
    )
    assert ok.status_code == 201 and ok.json()["builtin"] is False
    assert (
        await client.post(
            "/api/v1/chaos/scenarios", headers=roles["dev"], json={"name": "mine", "plan": plan}
        )
    ).status_code == 409
    assert (
        await client.post(
            "/api/v1/chaos/scenarios", headers=roles["dev"], json={"name": "provider-brownout", "plan": plan}
        )
    ).status_code == 409
    bad = {"name": "bad", "plan": {"faults": [{"type": "tool_timeout", "point": "after_tool"}]}}
    assert (await client.post("/api/v1/chaos/scenarios", headers=roles["dev"], json=bad)).status_code == 422


async def test_experiment_lifecycle_roles_idempotency_compare_and_isolation(client, roles, seeded):
    wf_id = await save_workflow(seeded, "chaos-api-wf", WF)
    body = {
        "name": "brownout",
        "workflow_id": str(wf_id),
        "scenario": "provider-brownout",
        "overrides": {"trials": 6, "max_affected_runs": 6, "seed": 3, "duration_seconds": 60},
    }
    assert (
        await client.post("/api/v1/chaos/experiments", headers=roles["viewer"], json=body)
    ).status_code == 403
    assert (
        await client.post(
            "/api/v1/chaos/experiments",
            headers=roles["dev"],
            json={**body, "plan": {"faults": [{"type": "tool_failure"}]}},
        )
    ).status_code == 422
    assert (
        await client.post(
            "/api/v1/chaos/experiments", headers=roles["dev"], json={**body, "scenario": "nope"}
        )
    ).status_code == 404
    h = {**roles["dev"], "Idempotency-Key": "chaos-1"}
    first = await client.post("/api/v1/chaos/experiments", headers=h, json=body)
    assert first.status_code == 201 and first.json()["status"] == "running" and first.json()["trials"] == 6
    again = await client.post("/api/v1/chaos/experiments", headers=h, json=body)
    assert again.json()["id"] == first.json()["id"] and again.headers.get("Idempotent-Replayed") == "true"
    exp_id = first.json()["id"]

    async with running_workers(2):

        async def done():
            e = (await client.get(f"/api/v1/chaos/experiments/{exp_id}", headers=roles["viewer"])).json()
            return e if e["status"] != "running" else None

        e = await wait_for(done, timeout=90, interval=0.4)
    assert e["status"] == "completed" and e["results"]["metrics"]["task_completion_rate"]["denominator"] == 6
    assert e["injections"] and all(
        i["fault_type"] in ("provider_timeout", "provider_rate_limit") for i in e["injections"]
    )
    assert e["conditions"]["approver"].startswith("simulated:") and e["plan_sha256"]
    listed = (await client.get("/api/v1/chaos/experiments", headers=roles["viewer"])).json()
    assert [x["id"] for x in listed["items"]] == [exp_id]

    # abort of a finished experiment is refused; compare needs completed experiments with the same plan
    assert (
        await client.post(f"/api/v1/chaos/experiments/{exp_id}/abort", headers=roles["dev"])
    ).status_code == 409
    second = (await client.post("/api/v1/chaos/experiments", headers=roles["dev"], json=body)).json()
    async with running_workers(2):

        async def done2():
            r = (
                await client.get(f"/api/v1/chaos/experiments/{second['id']}", headers=roles["viewer"])
            ).json()
            return r if r["status"] != "running" else None

        await wait_for(done2, timeout=90, interval=0.4)
    cmp = await client.post(
        "/api/v1/chaos/compare",
        headers=roles["viewer"],
        json={"baseline_id": exp_id, "candidate_id": second["id"]},
    )
    assert cmp.status_code == 200 and cmp.json()["same_fault_schedule"]["seed"] == 3
    assert cmp.json()["paired_completion"]["n_matched"] == 6 and "caution" in cmp.json()
    other = {**body, "overrides": {**body["overrides"], "seed": 4}}
    third = (await client.post("/api/v1/chaos/experiments", headers=roles["dev"], json=other)).json()
    abort = await client.post(f"/api/v1/chaos/experiments/{third['id']}/abort", headers=roles["dev"])
    assert abort.status_code == 200 and abort.json()["status"] == "cancelled"
    refused = await client.post(
        "/api/v1/chaos/compare",
        headers=roles["viewer"],
        json={"baseline_id": exp_id, "candidate_id": third["id"]},
    )
    assert refused.status_code == 409 and refused.json()["error"]["code"] in (
        "experiment_not_finished",
        "different_plan",
    )

    # isolation: another workspace sees none of it
    async with unit_of_work() as s:
        mallory = User(
            id=uuid.uuid4(), email="mallory@example.com", password_hash=hash_password("pw"), role="admin"
        )
        s.add(mallory)
        await s.flush()
        ws2 = Workspace(id=uuid.uuid4(), name="Other", owner_id=mallory.id)
        s.add(ws2)
        await s.flush()
        s.add(WorkspaceMember(workspace_id=ws2.id, user_id=mallory.id, role="admin"))
    m = await login(client, "mallory@example.com", "pw")
    assert (await client.get(f"/api/v1/chaos/experiments/{exp_id}", headers=m)).status_code == 404
    assert (await client.post(f"/api/v1/chaos/experiments/{exp_id}/abort", headers=m)).status_code == 404
    assert (await client.get("/api/v1/chaos/experiments", headers=m)).json()["items"] == []
    assert (
        await client.post("/api/v1/chaos/experiments", headers=m, json=body)
    ).status_code == 404  # not her workflow
