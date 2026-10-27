"""Memory Lab API: roles, lifecycle, optimistic concurrency, search, audit, experiments."""

import httpx2
import pytest

from agentos.main import app
from agentos_core.testing import running_workers, wait_for

pytestmark = pytest.mark.integration


@pytest.fixture
async def client(seeded):
    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def login(client, email, password):
    r = await client.post("/api/v1/auth/login", json={"email": email, "password": password})
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


async def test_records_roles_versions_search_and_audit(client):
    dev = await login(client, "developer@agentos.dev", "agentos-dev")
    viewer = await login(client, "viewer@agentos.dev", "agentos-viewer")
    body = {
        "kind": "kv",
        "key": "atlas.owner",
        "content": "Atlas owner: Priya",
        "tags": ["atlas"],
        "namespace": "demo",
    }
    assert (await client.post("/api/v1/memory/records", headers=viewer, json=body)).status_code == 403
    r = await client.post("/api/v1/memory/records", headers=dev, json=body)
    assert r.status_code == 201
    rec = r.json()
    assert rec["version"] == 1 and rec["provenance"]["origin"] == "api" and rec["state"] == "live"
    dup = await client.post("/api/v1/memory/records", headers=dev, json=body)
    assert dup.status_code == 409 and dup.json()["error"]["code"] == "key_exists"
    secret = await client.post(
        "/api/v1/memory/records", headers=dev, json={"kind": "note", "content": "token: abcdefgh12345678"}
    )
    assert secret.status_code == 422 and secret.json()["error"]["code"] == "sensitive_content"

    up = await client.patch(
        f"/api/v1/memory/records/{rec['id']}",
        headers=dev,
        json={"expected_version": 1, "content": "Atlas owner: Hana"},
    )
    assert up.status_code == 200 and up.json()["version"] == 2
    stale = await client.patch(
        f"/api/v1/memory/records/{rec['id']}",
        headers=dev,
        json={"expected_version": 1, "content": "Atlas owner: Zoya"},
    )
    assert stale.status_code == 409 and stale.json()["error"]["code"] == "version_conflict"
    assert (
        await client.patch(
            f"/api/v1/memory/records/{rec['id']}",
            headers=viewer,
            json={"expected_version": 2, "content": "x"},
        )
    ).status_code == 403

    found = (
        await client.post(
            "/api/v1/memory/search", headers=viewer, json={"query": "Atlas owner", "namespace": "demo"}
        )
    ).json()
    assert [i["content"] for i in found["items"]] == ["Atlas owner: Hana"] and found["items"][0][
        "untrusted"
    ] is True
    assert "untrusted" in found["notice"]
    hist = (await client.get(f"/api/v1/memory/records/{rec['id']}/history", headers=viewer)).json()["items"]
    assert [(h["version"], h["change_type"]) for h in hist] == [(1, "create"), (2, "update")]

    assert (await client.get("/api/v1/memory/audit", headers=viewer)).status_code == 403
    audit = (await client.get(f"/api/v1/memory/audit?record_id={rec['id']}", headers=dev)).json()["items"]
    assert {"create", "update"} <= {a["action"] for a in audit}

    assert (await client.delete(f"/api/v1/memory/records/{rec['id']}", headers=viewer)).status_code == 403
    gone = await client.delete(f"/api/v1/memory/records/{rec['id']}", headers=dev)
    assert gone.status_code == 200 and gone.json()["state"] == "deleted"
    assert (await client.get(f"/api/v1/memory/records/{rec['id']}", headers=dev)).status_code == 404
    live = (await client.get("/api/v1/memory/records?namespace=demo", headers=dev)).json()["items"]
    assert live == []
    inactive = (
        await client.get("/api/v1/memory/records?namespace=demo&include_inactive=true", headers=dev)
    ).json()["items"]
    assert len(inactive) == 1


async def test_experiment_flow_over_the_api(client):
    dev = await login(client, "developer@agentos.dev", "agentos-dev")
    viewer = await login(client, "viewer@agentos.dev", "agentos-viewer")
    assert (await client.post("/api/v1/memory/experiments", headers=viewer)).status_code == 403
    started = await client.post("/api/v1/memory/experiments", headers=dev)
    assert started.status_code == 201
    exp_id = started.json()["id"]
    assert (await client.post("/api/v1/memory/experiments", headers=dev)).status_code == 409  # one at a time

    async with running_workers(2, concurrency=4):

        async def finished():
            d = (await client.get(f"/api/v1/memory/experiments/{exp_id}", headers=viewer)).json()
            return d if d["status"] != "running" else None

        d = await wait_for(finished, timeout=240, interval=1.0)
    assert d["status"] == "completed"
    assert set(d["results"]["strategies"]) == set(d["strategies"]) and d["definitions"]["retrieval_top3"]
    listing = (await client.get("/api/v1/memory/experiments", headers=viewer)).json()
    assert listing["items"][0]["id"] == exp_id
