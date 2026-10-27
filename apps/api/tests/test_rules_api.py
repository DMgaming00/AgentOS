"""Rule-based candidate generation over the API: public rule catalog, roles, honest empty result."""

import httpx2
import pytest

from agentos.main import app
from agentos_core.autopilot.demo import EXTRACTION_AGENT

pytestmark = pytest.mark.integration


@pytest.fixture
async def client(seeded):
    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def login(client, email, password):
    r = await client.post("/api/v1/auth/login", json={"email": email, "password": password})
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


async def test_catalog_roles_and_silence_without_evidence(client, seeded):
    viewer = await login(client, "viewer@agentos.dev", "agentos-viewer")
    dev = await login(client, "developer@agentos.dev", "agentos-dev")
    cat = (await client.get("/api/v1/autopilot/rules", headers=viewer)).json()
    assert cat["version"] == "rules-v1" and len(cat["rules"]) == 9
    body = {"agent_id": str(seeded.agents[EXTRACTION_AGENT].id), "generator": "rules"}
    assert (
        await client.post("/api/v1/autopilot/candidates/generate", headers=viewer, json=body)
    ).status_code == 403
    r = await client.post("/api/v1/autopilot/candidates/generate", headers=dev, json=body)
    assert r.status_code == 201 and r.json() == []  # no traces, no evidence, no candidates
