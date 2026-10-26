"""Flight Recorder API: playback, comparison, deterministic re-execution, export, authorization."""

import json
import uuid

import httpx2
import pytest

from agentos.main import app
from agentos_core.db.models import User, Workspace, WorkspaceMember
from agentos_core.demo import research_report_workflow
from agentos_core.domain.states import ApprovalStatus
from agentos_core.engine import core
from agentos_core.engine.tx import unit_of_work
from agentos_core.security_passwords import hash_password
from agentos_core.testing import (
    get_run,
    pending_approvals,
    running_workers,
    start_run,
    wait_for,
    wait_run_status,
)

pytestmark = pytest.mark.integration
SECRET = "sk-ant-api03-abcdefghijklmnopqrstuvwxyz0123456789"


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


async def finished_research_run(seeded):
    run_id = await start_run(
        seeded,
        research_report_workflow(),
        name="research-report",
        run_input={"note": f"never store {SECRET}"},
    )

    async def step():
        run = await get_run(run_id)
        if run.status in ("succeeded", "failed"):
            return run
        for a in await pending_approvals(run_id):
            async with unit_of_work() as s:
                await core.resolve_approval(
                    s, a.id, decision=ApprovalStatus.APPROVED, user_id=seeded.users["admin"].id, reason="ok"
                )
        return None

    async with running_workers(2):
        run = await wait_for(step, timeout=60, interval=0.3)
    assert run.status == "succeeded"
    return run_id


async def test_flight_api_playback_state_and_model_call_detail(client, roles, seeded):
    run_id = await finished_research_run(seeded)
    r = await client.get(f"/api/v1/runs/{run_id}/flight", headers=roles["viewer"])  # viewers may inspect
    assert r.status_code == 200
    rec = r.json()
    assert rec["mode"] == "recorded" and rec["integrity"]["contiguous"] and rec["model_calls"]
    last = rec["integrity"]["last_seq"]
    st = (
        await client.get(f"/api/v1/runs/{run_id}/flight/state", headers=roles["viewer"], params={"seq": last})
    ).json()
    assert set(st["steps"].values()) == {"succeeded"}
    st0 = (
        await client.get(f"/api/v1/runs/{run_id}/flight/state", headers=roles["viewer"], params={"seq": 0})
    ).json()
    assert set(st0["steps"].values()) == {"pending"}
    mc = rec["model_calls"][0]
    detail = (
        await client.get(f"/api/v1/runs/{run_id}/flight/model-calls/{mc['id']}", headers=roles["viewer"])
    ).json()
    assert (
        detail["request"]["messages"] and detail["response"] is not None and SECRET not in json.dumps(detail)
    )
    assert (
        await client.get(f"/api/v1/runs/{run_id}/flight/model-calls/{uuid.uuid4()}", headers=roles["viewer"])
    ).status_code == 404


async def test_reexecute_export_and_compare_with_roles_and_idempotency(client, roles, seeded):
    run_id = await finished_research_run(seeded)
    denied = await client.post(f"/api/v1/runs/{run_id}/reexecute", headers=roles["viewer"])
    assert denied.status_code == 403
    key = {"Idempotency-Key": "reexec-1"}
    r1 = await client.post(f"/api/v1/runs/{run_id}/reexecute", headers={**roles["dev"], **key})
    assert r1.status_code == 201
    r2 = await client.post(f"/api/v1/runs/{run_id}/reexecute", headers={**roles["dev"], **key})
    assert r2.json()["id"] == r1.json()["id"] and r2.headers.get("Idempotent-Replayed") == "true"
    replay_id = uuid.UUID(r1.json()["id"])
    async with running_workers(2):
        run = await wait_run_status(replay_id, {"succeeded", "failed"}, 60)
    assert run.status == "succeeded" and run.purpose == "replay" and run.replay_of_run_id == run_id

    # a re-execution cannot be re-executed; the original is listed under purpose=replay filter only for replays
    again = await client.post(f"/api/v1/runs/{replay_id}/reexecute", headers=roles["dev"])
    assert again.status_code == 409 and again.json()["error"]["code"] == "run_not_replayable"
    listed = (await client.get("/api/v1/runs", headers=roles["viewer"], params={"purpose": "replay"})).json()
    assert [x["id"] for x in listed["items"]] == [str(replay_id)]

    cmp = await client.get(
        "/api/v1/flight/compare", headers=roles["viewer"], params={"a": str(run_id), "b": str(replay_id)}
    )
    assert cmp.status_code == 200
    summary = cmp.json()["summary"]
    assert summary["final_status"] == ["succeeded", "succeeded"] and summary["identical"] is False
    assert {s["key"] for s in cmp.json()["steps"] if s["differences"]} == {"write_report"}

    # export: developers only, sanitized, never contains the credential seeded into the run input
    assert (await client.get(f"/api/v1/runs/{run_id}/export", headers=roles["viewer"])).status_code == 403
    ex = await client.get(f"/api/v1/runs/{run_id}/export", headers=roles["dev"])
    assert ex.status_code == 200 and "attachment" in ex.headers["content-disposition"]
    assert SECRET not in ex.text and json.loads(ex.text)["sanitized"] is True


async def test_flight_endpoints_are_workspace_isolated(client, roles, seeded):
    run_id = await finished_research_run(seeded)
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
    m = await login(client, "mallory@example.com", "pw")  # admin of her own workspace only
    for path in (
        f"/api/v1/runs/{run_id}/flight",
        f"/api/v1/runs/{run_id}/flight/state?seq=1",
        f"/api/v1/runs/{run_id}/export",
    ):
        assert (await client.get(path, headers=m)).status_code == 404  # existence is not revealed
    assert (await client.post(f"/api/v1/runs/{run_id}/reexecute", headers=m)).status_code == 404
    assert (
        await client.get(f"/api/v1/runs/{uuid.uuid4()}/flight", headers=roles["viewer"])
    ).status_code == 404
    cmp = await client.get(
        "/api/v1/flight/compare", headers=roles["viewer"], params={"a": str(run_id), "b": str(uuid.uuid4())}
    )
    assert cmp.status_code == 404
    cross = await client.get("/api/v1/flight/compare", headers=m, params={"a": str(run_id), "b": str(run_id)})
    assert cross.status_code == 404
