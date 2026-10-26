"""Security Center API: running the suite, role-based detail, catalog, incidents, isolation."""

import uuid

import httpx2
import pytest

from agentos.main import app
from agentos_core.db.models import User, Workspace, WorkspaceMember
from agentos_core.engine.tx import unit_of_work
from agentos_core.security.corpus import SUITE_VERSION
from agentos_core.security_passwords import hash_password
from agentos_core.testing import add_agent, running_workers, start_run, wait_for, wait_run_status

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
        "dev": await login(client, "developer@agentos.dev", "agentos-dev"),
        "viewer": await login(client, "viewer@agentos.dev", "agentos-viewer"),
    }


async def test_catalog_run_detail_roles_incidents_and_isolation(client, roles, seeded):
    cat = (await client.get("/api/v1/security/catalog", headers=roles["dev"])).json()
    assert cat["suite_version"] == SUITE_VERSION and cat["total"] == 51
    assert {c["kind"] for c in cat["cases"]} == {"policy", "injection", "benign", "exposure"}
    assert all(c["sha256"] for c in cat["cases"])

    # a real policy denial in an ordinary run becomes a security incident
    await add_agent(seeded, "Probe Agent", scenario="forbidden_tool", tools=["document_search"])
    rid = await start_run(
        seeded,
        {
            "nodes": [
                {
                    "key": "g",
                    "type": "agent_task",
                    "agent": "Probe Agent",
                    "tools": ["document_search"],
                    "retry": {"max_attempts": 1},
                }
            ]
        },
    )
    async with running_workers(1):
        await wait_run_status(rid, {"succeeded", "failed"}, 30)
    inc = (
        await client.get(
            "/api/v1/security/incidents", headers=roles["viewer"], params={"source": "policy_denial"}
        )
    ).json()
    assert (
        inc["total"] >= 1
        and inc["items"][0]["run_id"] == str(rid)
        and inc["items"][0]["evidence"]["tool_execution_id"]
    )

    # starting needs developer; one at a time; idempotent
    assert (await client.post("/api/v1/security/runs", headers=roles["viewer"])).status_code == 403
    h = {**roles["dev"], "Idempotency-Key": "sec-1"}
    first = await client.post("/api/v1/security/runs", headers=h)
    assert first.status_code == 201 and first.json()["status"] == "running"
    assert (await client.post("/api/v1/security/runs", headers=h)).headers.get(
        "Idempotent-Replayed"
    ) == "true"
    assert (await client.post("/api/v1/security/runs", headers=roles["dev"])).status_code == 409
    run_id = first.json()["id"]

    async with running_workers(2):

        async def done():
            r = (await client.get(f"/api/v1/security/runs/{run_id}", headers=roles["dev"])).json()
            return r if r["status"] != "running" else None

        detail = await wait_for(done, timeout=150, interval=0.5)
    assert detail["status"] == "completed" and detail["counts"]["injection"]["pass"] == 28
    assert len(detail["cases"]) == 51 and all(c["verdict"] == "pass" for c in detail["cases"])
    assert detail["cases"][0]["observed"] is not None and "fixtures" not in detail["conditions"]

    # viewers get verdicts and metrics, not the raw evidence
    v = (await client.get(f"/api/v1/security/runs/{run_id}", headers=roles["viewer"])).json()
    assert v["results"]["metrics"]["unsafe_side_effects"] == 0
    assert all(
        c["observed"] is None and c["evidence"] is None and c["workflow_run_id"] is None for c in v["cases"]
    )

    ov = (await client.get("/api/v1/security/overview", headers=roles["viewer"])).json()
    assert (
        ov["latest"]["id"] == run_id and ov["running"] is None and ov["incidents_30d"]["policy_denial"] >= 1
    )

    # isolation
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
    assert (await client.get(f"/api/v1/security/runs/{run_id}", headers=m)).status_code == 404
    assert (await client.get("/api/v1/security/runs", headers=m)).json()["items"] == []
    assert (await client.get("/api/v1/security/overview", headers=m)).json()["latest"] is None
    assert (await client.get("/api/v1/security/incidents", headers=m)).json()["total"] == 0
