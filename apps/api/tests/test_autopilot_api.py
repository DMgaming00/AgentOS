"""Autopilot API: authorization, idempotency, isolation, exports and an API-driven optimization cycle."""

import uuid

import httpx2
import pytest

from agentos.main import app
from agentos_core.autopilot.demo import EXTRACTION_AGENT
from agentos_core.db.models import User, Workspace, WorkspaceMember
from agentos_core.engine.tx import unit_of_work
from agentos_core.security_passwords import hash_password
from agentos_core.testing import running_workers, wait_for

pytestmark = pytest.mark.integration


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
        "admin": await login(client, "admin@agentos.dev", "agentos-admin"),
        "dev": await login(client, "developer@agentos.dev", "agentos-dev"),
        "viewer": await login(client, "viewer@agentos.dev", "agentos-viewer"),
    }


async def ids(client, h):
    agents = (await client.get("/api/v1/agents", headers=h)).json()["items"]
    agent = next(a for a in agents if a["name"] == EXTRACTION_AGENT)
    ds = {
        d["split"]: d for d in (await client.get("/api/v1/evaluations/datasets", headers=h)).json()["items"]
    }
    return agent, ds


async def wait_status(client, h, path, statuses, timeout=120):
    async def check():
        r = (await client.get(path, headers=h)).json()
        return r if r["status"] in statuses else None

    return await wait_for(check, timeout=timeout)


@pytest.mark.timeout(400)
async def test_api_driven_cycle_with_roles_and_idempotency(client, roles):
    admin, dev, viewer = roles["admin"], roles["dev"], roles["viewer"]
    agent, ds = await ids(client, dev)
    assert agent["active_version_id"] and set(ds) == {"development", "validation", "test"}
    versions = (await client.get(f"/api/v1/agents/{agent['id']}/versions", headers=viewer)).json()
    assert [v["version"] for v in versions] == [1] and versions[0]["is_active"]

    # viewers cannot run evaluations or analysis
    body = {"agent_id": agent["id"], "dataset_id": ds["development"]["id"]}
    assert (await client.post("/api/v1/evaluations/run", headers=viewer, json=body)).status_code == 403
    assert (
        await client.post("/api/v1/autopilot/failures/analyze", headers=viewer, json={})
    ).status_code == 403

    async with running_workers(2, concurrency=6):
        h = {**dev, "Idempotency-Key": "eval-dev-1"}
        r1 = await client.post("/api/v1/evaluations/run", headers=h, json=body)
        r2 = await client.post("/api/v1/evaluations/run", headers=h, json=body)
        assert r1.status_code == 201 and r2.json()["id"] == r1.json()["id"]
        assert r2.headers.get("Idempotent-Replayed") == "true"
        ev = await wait_status(client, dev, f"/api/v1/evaluations/runs/{r1.json()['id']}", {"completed"})
        assert ev["metrics"]["task_success_rate"]["denominator"] == 9

        analyzed = (
            await client.post("/api/v1/autopilot/failures/analyze", headers=dev, json={"window_hours": 2})
        ).json()
        assert analyzed["by_category"]["OUTPUT_SCHEMA_INVALID"] >= 1
        failures = (await client.get("/api/v1/autopilot/failures", headers=viewer)).json()
        assert any(
            s["action"] == "diagnose" and s["category"] == "OUTPUT_SCHEMA_INVALID"
            for s in failures["suggested_investigations"]
        )

        d = (
            await client.post(
                "/api/v1/autopilot/diagnoses",
                headers=dev,
                json={"agent_id": agent["id"], "category": "OUTPUT_SCHEMA_INVALID"},
            )
        ).json()
        assert d["status"] == "hypothesis" and d["label"].startswith("hypothesis")
        detail = (await client.get(f"/api/v1/autopilot/failures/{d['id']}", headers=viewer)).json()
        assert any(e["cited"] for e in detail["evidence"])

        cands = (
            await client.post(
                "/api/v1/autopilot/candidates/generate",
                headers=dev,
                json={"agent_id": agent["id"], "diagnosis_id": d["id"]},
            )
        ).json()
        assert [c["status"] for c in cands] == ["proposed", "proposed", "rejected_policy"]
        fix = cands[0]
        assert fix["candidate_version"] == 2 and fix["diff"]

        exp = (
            await client.post(
                "/api/v1/experiments",
                headers=dev,
                json={
                    "agent_id": agent["id"],
                    "dataset_id": ds["validation"]["id"],
                    "candidate_id": fix["id"],
                },
            )
        ).json()
        exp = await wait_status(client, viewer, f"/api/v1/experiments/{exp['id']}", {"completed", "failed"})
        assert exp["decision"] == "eligible" and exp["eligibility"]["improvement_demonstrated"]
        assert exp["results"]["comparison"]["per_case"] and exp["results"]["diff"]

    # promotion: developer requests (idempotent), admin approves (idempotent), others cannot approve
    h = {**dev, "Idempotency-Key": "promote-1"}
    req = (
        await client.post("/api/v1/deployments/promote", headers=h, json={"experiment_id": exp["id"]})
    ).json()
    again = await client.post("/api/v1/deployments/promote", headers=h, json={"experiment_id": exp["id"]})
    assert again.json()["id"] == req["id"] and req["status"] == "pending"
    for who in (viewer, dev):
        assert (
            await client.post(f"/api/v1/deployments/requests/{req['id']}/approve", headers=who, json={})
        ).status_code == 403
    ha = {**admin, "Idempotency-Key": "approve-1"}
    a1 = await client.post(
        f"/api/v1/deployments/requests/{req['id']}/approve", headers=ha, json={"reason": "ok"}
    )
    a2 = await client.post(
        f"/api/v1/deployments/requests/{req['id']}/approve", headers=ha, json={"reason": "ok"}
    )
    assert (
        a1.status_code == 200 and a1.json()["status"] == "approved" and a2.headers.get("Idempotent-Replayed")
    )
    a3 = await client.post(f"/api/v1/deployments/requests/{req['id']}/approve", headers=admin, json={})
    assert a3.status_code == 409 and a3.json()["error"]["code"] == "request_not_pending"
    agent_after = (await client.get(f"/api/v1/agents/{agent['id']}", headers=viewer)).json()
    assert agent_after["active_version_id"] == fix["candidate_version_id"]
    assert "JSON number" in agent_after["system_instructions"]  # legacy fields mirror the active version

    # rollback: admin only, reason required
    rb = {"agent_id": agent["id"], "reason": "verify rollback path"}
    assert (await client.post("/api/v1/deployments/rollback", headers=dev, json=rb)).status_code == 403
    r = await client.post("/api/v1/deployments/rollback", headers=admin, json=rb)
    assert r.status_code == 201 and r.json()["deployment_type"] == "rollback" and r.json()["version"] == 1
    hist = (
        await client.get("/api/v1/deployments/history", headers=viewer, params={"agent_id": agent["id"]})
    ).json()["items"]
    assert [h["deployment_type"] for h in hist] == ["rollback", "promotion", "initial"]
    assert hist[1]["status"] == "rolled_back" and hist[1]["rollout_events"]

    ov = (await client.get("/api/v1/autopilot/overview", headers=viewer)).json()
    assert ov["candidates_rejected_by_policy"] == 1 and ov["rollbacks"] == 1 and ov["measured_improvements"]
    events = (await client.get("/api/v1/autopilot/events", headers=viewer)).json()
    seqs = [e["sequence_number"] for e in events]
    assert seqs == list(range(1, len(seqs) + 1))
    types = {e["event_type"] for e in events}
    assert {
        "diagnosis.started",
        "diagnosis.completed",
        "candidate.generated",
        "candidate.rejected",
        "evaluation.started",
        "evaluation.completed",
        "experiment.completed",
        "approval.requested",
        "version.promoted",
        "version.rolled_back",
    } <= types


async def test_exports_hidden_answers_and_cost_confirmation(client, roles, seeded):
    dev, viewer = roles["dev"], roles["viewer"]
    agent, ds = await ids(client, dev)
    hidden = (
        await client.get(f"/api/v1/evaluations/datasets/{ds['validation']['id']}", headers=viewer)
    ).json()
    assert hidden["expected_hidden"] and all(c["expected_output"] is None for c in hidden["cases"])
    visible = (await client.get(f"/api/v1/evaluations/datasets/{ds['validation']['id']}", headers=dev)).json()
    assert not visible["expected_hidden"] and visible["cases"][0]["expected_output"]
    async with running_workers(1, concurrency=6):
        run = (
            await client.post(
                "/api/v1/evaluations/run",
                headers=dev,
                json={"agent_id": agent["id"], "dataset_id": ds["test"]["id"]},
            )
        ).json()
        await wait_status(client, dev, f"/api/v1/evaluations/runs/{run['id']}", {"completed"})
    md = await client.get(
        f"/api/v1/evaluations/runs/{run['id']}/export", headers=viewer, params={"format": "md"}
    )
    assert (
        md.status_code == 200
        and "## Methodology" in md.text
        and "SIMULATED" in md.text
        and ds["test"]["dataset_hash"] in md.text
    )
    csv = await client.get(
        f"/api/v1/evaluations/runs/{run['id']}/export", headers=viewer, params={"format": "csv"}
    )
    assert "methodology=" in csv.text.splitlines()[0] and "agent_version=1" in csv.text.splitlines()[0]
    js = (
        await client.get(
            f"/api/v1/evaluations/runs/{run['id']}/export", headers=viewer, params={"format": "json"}
        )
    ).json()
    assert js["dataset"]["version"] == 1 and js["conditions"]["model"] == "mock-extractor"

    # a paid-provider version needs explicit cost confirmation
    r = await client.post(
        "/api/v1/autopilot/candidates",
        headers=dev,
        json={
            "agent_id": agent["id"],
            "title": "Use Claude",
            "changes": [
                {"field": "provider", "value": "anthropic"},
                {"field": "model", "value": "claude-haiku-5-5"},
                {"field": "model_configuration.scenario", "value": None},
            ],
        },
    )
    assert (
        r.json()["status"] == "rejected_policy"
    )  # scenario wiring is never optimizable; model not in policy
    exp = await client.post(
        "/api/v1/experiments",
        headers=dev,
        json={
            "agent_id": agent["id"],
            "dataset_id": ds["development"]["id"],
            "baseline_version_id": agent["active_version_id"],
            "candidate_version_id": str(uuid.uuid4()),
        },
    )
    assert exp.status_code == 422


async def test_autopilot_is_workspace_isolated(client, roles):
    agent, ds = await ids(client, roles["dev"])
    async with unit_of_work() as s:
        u = User(id=uuid.uuid4(), email="eve@example.com", password_hash=hash_password("pw"), role="admin")
        s.add(u)
        await s.flush()
        w = Workspace(id=uuid.uuid4(), name="Eve", owner_id=u.id)
        s.add(w)
        await s.flush()
        s.add(WorkspaceMember(workspace_id=w.id, user_id=u.id, role="admin"))
    eve = await login(client, "eve@example.com", "pw")
    assert (await client.get(f"/api/v1/agents/{agent['id']}/versions", headers=eve)).status_code == 404
    assert (
        await client.get(f"/api/v1/evaluations/datasets/{ds['validation']['id']}", headers=eve)
    ).status_code == 404
    assert (
        await client.post(
            "/api/v1/evaluations/run",
            headers=eve,
            json={"agent_id": agent["id"], "dataset_id": ds["validation"]["id"]},
        )
    ).status_code == 404
    assert (
        await client.post(
            "/api/v1/deployments/rollback", headers=eve, json={"agent_id": agent["id"], "reason": "attack"}
        )
    ).status_code == 404
    assert (await client.get("/api/v1/autopilot/candidates", headers=eve)).json()["total"] == 0
    assert (await client.get("/api/v1/autopilot/events", headers=eve)).json() == []


async def test_policy_requires_admin_and_cannot_widen(client, roles):
    p = (await client.get("/api/v1/autopilot/policy", headers=roles["viewer"])).json()
    assert p["max_candidates"] == 3
    p["optimizable_fields"] = ["tool_strategy", "max_cost_usd"]
    assert (await client.put("/api/v1/autopilot/policy", headers=roles["dev"], json=p)).status_code == 403
    r = (await client.put("/api/v1/autopilot/policy", headers=roles["admin"], json=p)).json()
    assert r["optimizable_fields"] == ["tool_strategy"]
