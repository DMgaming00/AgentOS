"""Event integrity under contention: durable, gap-free, monotonic, replayable, deduplicable, workspace-scoped."""

import uuid

import httpx2
import pytest

from agentos.main import app
from agentos_core.db.models import User, Workspace, WorkspaceMember
from agentos_core.demo import research_report_workflow
from agentos_core.engine.tx import unit_of_work
from agentos_core.security_passwords import hash_password
from agentos_core.testing import pending_approvals, running_workers, start_run, wait_run_status

pytestmark = pytest.mark.integration


@pytest.fixture
async def client(seeded):
    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def login(client, email, password):
    r = await client.post("/api/v1/auth/login", json={"email": email, "password": password})
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def stream_ids(text):
    return [int(line[4:]) for line in text.splitlines() if line.startswith("id: ")]


@pytest.mark.timeout(120)
async def test_sequences_are_gap_free_replay_exactly_and_never_leak_across_workspaces(client, seeded):
    admin = await login(client, "admin@agentos.dev", "agentos-admin")
    run_id = await start_run(seeded, research_report_workflow(), name="integrity")
    async with running_workers(
        3, concurrency=6
    ):  # three research branches run in parallel on contending workers
        await wait_run_status(run_id, {"waiting_approval"}, timeout=60)
        (approval,) = await pending_approvals(run_id)
        assert (
            await client.post(
                f"/api/v1/approvals/{approval.id}/approve", headers=admin, json={"reason": "ok"}
            )
        ).status_code == 200
        await wait_run_status(run_id, {"succeeded"}, timeout=60)

    events = (await client.get(f"/api/v1/runs/{run_id}/events", headers=admin)).json()
    seqs = [e["sequence_number"] for e in events]
    assert len(seqs) > 30 and seqs == list(range(1, len(seqs) + 1))  # durable history, monotonic, no gaps
    inspector = (await client.get(f"/api/v1/runs/{run_id}/inspector", headers=admin)).json()
    assert inspector["integrity"] == {
        "events": len(seqs),
        "first_sequence": 1,
        "last_sequence": len(seqs),
        "gap_free": True,
        "monotonic": True,
    }

    # Reconnection: from any point the stream replays exactly the missed suffix (so a client that also dedupes by
    # sequence number can never double-apply an event).
    full = (await client.get(f"/api/v1/runs/{run_id}/stream", headers=admin)).text
    assert stream_ids(full) == seqs
    for cut in (1, 7, len(seqs) // 2, len(seqs) - 1, len(seqs)):
        resumed = (
            await client.get(f"/api/v1/runs/{run_id}/stream", headers={**admin, "Last-Event-ID": str(cut)})
        ).text
        assert stream_ids(resumed) == seqs[cut:], cut
    overlap = stream_ids(full)[10:] + stream_ids(full)[5:12]
    assert sorted(set(overlap)) == sorted(
        set(seqs[5:])
    )  # what a deduplicating client keeps after an overlapping replay

    # Authorization: another workspace's admin, and an anonymous caller, learn nothing about this run's history.
    async with unit_of_work() as s:
        mallory = User(
            id=uuid.uuid4(), email="mallory2@example.com", password_hash=hash_password("pw"), role="admin"
        )
        s.add(mallory)
        await s.flush()
        ws2 = Workspace(id=uuid.uuid4(), name="Other", owner_id=mallory.id)
        s.add(ws2)
        await s.flush()
        s.add(WorkspaceMember(workspace_id=ws2.id, user_id=mallory.id, role="admin"))
    m = await login(client, "mallory2@example.com", "pw")
    for path in (
        f"/api/v1/runs/{run_id}/events",
        f"/api/v1/runs/{run_id}/stream",
        f"/api/v1/runs/{run_id}/inspector",
    ):
        assert (await client.get(path, headers=m)).status_code == 404, path
        assert (await client.get(path)).status_code == 401, path
