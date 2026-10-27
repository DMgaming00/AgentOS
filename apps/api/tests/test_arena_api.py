"""Agent Arena API: contenders, corpus integrity, roles, the one-shot test split, offline-only enforcement."""

import httpx2
import pytest

from agentos.main import app
from agentos_core.testing import add_agent, running_workers, wait_for

pytestmark = pytest.mark.integration


@pytest.fixture
async def client(arena_seeded):
    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def login(client, email, password):
    r = await client.post("/api/v1/auth/login", json={"email": email, "password": password})
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


async def test_arena_flow_roles_lock_and_offline_only(client, arena_seeded):
    dev = await login(client, "developer@agentos.dev", "agentos-dev")
    viewer = await login(client, "viewer@agentos.dev", "agentos-viewer")

    cont = (await client.get("/api/v1/arena/contenders", headers=dev)).json()["items"]
    arena_agents = {c["name"]: c for c in cont if c["arena_contender"]}
    assert len(arena_agents) == 6 and all(c["eligible"] for c in arena_agents.values())

    corpus = (await client.get("/api/v1/arena/corpus", headers=viewer)).json()
    assert corpus["total_cases"] == sum(d["cases"] for d in corpus["datasets"])
    assert corpus["leakage"]["identical_case_across_splits"] == 0

    pick = [arena_agents[n]["agent_version_id"] for n in ("Arena · Bare", "Arena · Careful")]
    assert (
        await client.post("/api/v1/arena/runs", headers=viewer, json={"name": "x", "agent_version_ids": pick})
    ).status_code == 403
    assert (
        await client.post(
            "/api/v1/arena/runs", headers=dev, json={"name": "x", "agent_version_ids": pick[:1]}
        )
    ).status_code == 422

    # a paid-provider agent cannot enter the Arena, regardless of configuration
    paid = await add_agent(
        arena_seeded,
        "Paid Claude",
        scenario="echo",
        tools=["document_search"],
        provider="anthropic",
        model="claude-sonnet-5-5",
    )
    pv = (await client.get("/api/v1/arena/contenders", headers=dev)).json()["items"]
    paid_row = next(c for c in pv if c["agent_id"] == str(paid.id))
    assert paid_row["eligible"] is False
    r = await client.post(
        "/api/v1/arena/runs",
        headers=dev,
        json={"name": "paid", "agent_version_ids": [paid_row["agent_version_id"], pick[0]]},
    )
    assert r.status_code == 422 and r.json()["error"]["code"] == "offline_only"

    created = await client.post(
        "/api/v1/arena/runs", headers=dev, json={"name": "api arena", "agent_version_ids": pick}
    )
    assert created.status_code == 201
    arena_id = created.json()["id"]
    assert created.json()["stage"] == "selection_running"

    early = await client.post(f"/api/v1/arena/runs/{arena_id}/final-test", headers=dev)
    assert early.status_code == 409 and early.json()["error"]["code"] == "selection_not_complete"
    hidden = await client.get(f"/api/v1/arena/runs/{arena_id}/cases?split=test", headers=dev)
    assert hidden.status_code == 409

    async with running_workers(3, concurrency=4):

        async def selection_done():
            d = (await client.get(f"/api/v1/arena/runs/{arena_id}", headers=viewer)).json()
            return d if d["stage"] == "selection_complete" else None

        d = await wait_for(selection_done, timeout=200, interval=1.0)
        assert "test" not in d["leaderboards"] and set(d["leaderboards"]) == {"development", "validation"}
        rows = d["leaderboards"]["validation"]["rows"]
        assert rows[0]["agent"] == "Arena · Careful" and rows[0]["rank"] == 1
        assert rows[0]["overall"]["score"] > rows[1]["overall"]["score"]

        assert (
            await client.post(f"/api/v1/arena/runs/{arena_id}/final-test", headers=viewer)
        ).status_code == 403
        started = await client.post(f"/api/v1/arena/runs/{arena_id}/final-test", headers=dev)
        assert started.status_code == 201 and started.json()["final_test_started_at"]
        again = await client.post(f"/api/v1/arena/runs/{arena_id}/final-test", headers=dev)
        assert again.status_code == 409 and again.json()["error"]["code"] == "test_already_run"

        async def finished():
            d = (await client.get(f"/api/v1/arena/runs/{arena_id}", headers=viewer)).json()
            return d if d["stage"] == "completed" else None

        d = await wait_for(finished, timeout=200, interval=1.0)

    assert {r["agent"] for r in d["leaderboards"]["test"]["rows"]} == {"Arena · Bare", "Arena · Careful"}
    assert len(d["selection_vs_test"]) == 2
    bare = next(v for v in pick if v == arena_agents["Arena · Bare"]["agent_version_id"])
    fails = (
        await client.get(
            f"/api/v1/arena/runs/{arena_id}/cases?split=validation&agent_version_id={bare}", headers=dev
        )
    ).json()
    assert fails["expected_hidden"] is True and fails["items"]
    assert all(i["expected"] is None and i["observed"] is None for i in fails["items"])
    dev_cases = (
        await client.get(
            f"/api/v1/arena/runs/{arena_id}/cases?split=development&agent_version_id={bare}", headers=dev
        )
    ).json()
    assert dev_cases["expected_hidden"] is False and all("expected" in i for i in dev_cases["items"])
