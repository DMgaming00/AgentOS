"""Release Center API: roles, gate preview, staged release over HTTP, refusal details, environments."""

from datetime import timedelta

import httpx2
import pytest
from sqlalchemy import update

from agentos.main import app
from agentos_core.db.models import WorkflowStep
from agentos_core.engine.tx import unit_of_work
from agentos_core.testing import start_run
from agentos_core.testing_autopilot import clean_security_run, eligible_experiment
from agentos_core.util import utcnow

pytestmark = pytest.mark.integration


@pytest.fixture
async def client(seeded):
    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def login(client, email, password):
    r = await client.post("/api/v1/auth/login", json={"email": email, "password": password})
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


async def fake_steps(seeded, agent_id, version_id, n, ok):
    for _ in range(n):
        run_id = await start_run(seeded, {"nodes": [{"key": "a", "type": "aggregation"}]})
        async with unit_of_work() as s:
            now = utcnow()
            await s.execute(
                update(WorkflowStep)
                .where(WorkflowStep.run_id == run_id)
                .values(
                    agent_definition_id=agent_id,
                    agent_version_id=version_id,
                    status="succeeded" if ok else "failed",
                    started_at=now - timedelta(seconds=1),
                    completed_at=now,
                )
            )


@pytest.mark.timeout(300)
async def test_release_over_http_with_roles_gates_and_rollback(client, seeded):
    admin = await login(client, "admin@agentos.dev", "agentos-admin")
    dev = await login(client, "developer@agentos.dev", "agentos-dev")
    viewer = await login(client, "viewer@agentos.dev", "agentos-viewer")
    agent, exp = await eligible_experiment(seeded)

    d = (await client.get("/api/v1/releases/defaults", headers=viewer)).json()
    assert d["schedule"] == [5, 10, 25, 50, 100] and d["environments"] == [
        "development",
        "staging",
        "production",
    ]
    envs = (await client.get(f"/api/v1/releases/environments?agent_id={agent.id}", headers=viewer)).json()
    item = envs["items"][0]["environments"]
    assert (
        item["production"]["version"]["version"] == 1
        and item["staging"]["pinned"] is False
        and "not a hosted" in envs["note"]
    )

    preview = (
        await client.get(f"/api/v1/releases/gates?agent_id={agent.id}&experiment_id={exp.id}", headers=viewer)
    ).json()
    assert preview["can_request"] is False and preview["blocking"] == ["security_evaluation"]
    assert [g["gate"] for g in preview["gates"]] == [
        "experiment_eligible",
        "task_quality",
        "regression_level",
        "minimum_sample_size",
        "budget_compliance",
        "security_evaluation",
        "human_approval",
    ]
    body = {
        "agent_id": str(agent.id),
        "environment": "production",
        "experiment_id": str(exp.id),
        "schedule": [50, 100],
        "seed": "api-seed",
        "min_stage_samples": 4,
        "auto_rollback": True,
    }
    assert (await client.post("/api/v1/releases", headers=viewer, json=body)).status_code == 403
    refused = await client.post("/api/v1/releases", headers=dev, json=body)
    assert refused.status_code == 409 and refused.json()["error"]["code"] == "gates_failed"
    assert [g["gate"] for g in refused.json()["error"]["details"]["gates"] if g["passed"] is False] == [
        "security_evaluation"
    ]

    await clean_security_run(seeded.workspace.id)
    bad = await client.post("/api/v1/releases", headers=dev, json={**body, "schedule": [5, 10]})
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "invalid_schedule"
    made = await client.post("/api/v1/releases", headers=dev, json=body)
    assert made.status_code == 201 and made.json()["status"] == "pending_approval"
    plan = made.json()
    detail = (await client.get(f"/api/v1/releases/{plan['id']}", headers=viewer)).json()
    assert (
        detail["approval"]["status"] == "pending"
        and detail["config_diff"]
        and detail["baseline_version"] == 1
    )

    # the existing approval flow activates the release
    assert (
        await client.post(f"/api/v1/deployments/requests/{plan['request_id']}/approve", headers=dev, json={})
    ).status_code == 403
    ok = await client.post(
        f"/api/v1/deployments/requests/{plan['request_id']}/approve",
        headers=admin,
        json={"reason": "go"},
    )
    assert ok.status_code == 200, ok.text
    detail = (await client.get(f"/api/v1/releases/{plan['id']}", headers=viewer)).json()
    assert (
        detail["status"] == "active" and detail["percent"] == 50 and detail["evaluation"]["canary_steps"] == 0
    )

    assert (
        await client.post(f"/api/v1/releases/{plan['id']}/advance", headers=dev)
    ).status_code == 403  # advancing is an admin decision
    blocked = await client.post(f"/api/v1/releases/{plan['id']}/advance", headers=admin)
    assert blocked.status_code == 409 and "stage_samples" in [
        g["gate"] for g in blocked.json()["error"]["details"]["gates"] if g["passed"] is False
    ]
    assert (
        await client.patch(f"/api/v1/releases/{plan['id']}/rules", headers=dev, json={"auto_rollback": False})
    ).status_code == 403
    bad_rules = await client.patch(
        f"/api/v1/releases/{plan['id']}/rules",
        headers=admin,
        json={"rules": {"failure_rate_increase": {"max_increase": 5}}},
    )
    assert bad_rules.status_code == 422
    upd = await client.patch(
        f"/api/v1/releases/{plan['id']}/rules",
        headers=admin,
        json={
            "auto_rollback": False,
            "rules": {"failure_rate_increase": {"max_increase": 0.1, "min_samples": 8}},
        },
    )
    assert (
        upd.json()["auto_rollback"] is False
        and upd.json()["rules"]["failure_rate_increase"]["min_samples"] == 8
    )

    await fake_steps(seeded, agent.id, plan["candidate_version_id"], 10, ok=False)
    await fake_steps(seeded, agent.id, plan["baseline_version_id"], 10, ok=True)
    ev = (await client.post(f"/api/v1/releases/{plan['id']}/evaluate", headers=dev)).json()
    assert ev["evaluation"]["triggered"] == ["failure_rate_increase"]
    assert (
        await client.post(f"/api/v1/releases/{plan['id']}/rollback", headers=dev, json={"reason": "operator"})
    ).status_code == 403
    rolled = await client.post(
        f"/api/v1/releases/{plan['id']}/rollback",
        headers=admin,
        json={"reason": "failure rate regression on canary"},
    )
    assert rolled.status_code == 200 and rolled.json()["status"] == "rolled_back"
    final = (await client.get(f"/api/v1/releases/{plan['id']}", headers=viewer)).json()
    types = [e["type"] for e in final["events"]]
    assert (
        types[:3] == ["gates_evaluated", "release_requested", "stage_started"]
        and "rules_updated" in types
        and types[-1] == "rolled_back"
    )
    envs = (await client.get(f"/api/v1/releases/environments?agent_id={agent.id}", headers=viewer)).json()[
        "items"
    ][0]["environments"]
    assert envs["production"]["canary"] is None and envs["production"]["version"]["version"] == 1
    assert (await client.get("/api/v1/releases?status=rolled_back", headers=viewer)).json()["items"][0][
        "id"
    ] == plan["id"]
