"""API integration tests (real PostgreSQL, in-process ASGI client, in-process workers)."""

import uuid

import httpx2
import pytest
from sqlalchemy import func, select

from agentos.main import app
from agentos_core.config import get_settings
from agentos_core.db.models import User, WorkflowRun, Workspace, WorkspaceMember
from agentos_core.engine.tx import unit_of_work
from agentos_core.security_passwords import hash_password
from agentos_core.testing import running_workers, wait_for

pytestmark = pytest.mark.integration

CYCLIC = {
    "nodes": [
        {"key": "a", "type": "aggregation", "depends_on": ["b"]},
        {"key": "b", "type": "aggregation", "depends_on": ["a"]},
    ]
}
SIMPLE = {
    "nodes": [
        {
            "key": "sum",
            "type": "tool_task",
            "tool": "calculator",
            "arguments": {"operation": "sum", "values": [1, 2, 3]},
        },
        {"key": "agg", "type": "aggregation", "depends_on": ["sum"]},
    ]
}


@pytest.fixture
async def client(seeded):
    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def login(client, email, password):
    r = await client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest.fixture
async def admin(client):
    return await login(client, "admin@agentos.dev", "agentos-admin")


@pytest.fixture
async def viewer(client):
    return await login(client, "viewer@agentos.dev", "agentos-viewer")


@pytest.fixture
async def developer(client):
    return await login(client, "developer@agentos.dev", "agentos-dev")


async def workflow_id(client, h, name):
    r = await client.get("/api/v1/workflows", headers=h)
    return next(w["id"] for w in r.json()["items"] if w["name"] == name)


async def test_health_ready_and_auth(client):
    assert (await client.get("/health")).json() == {"status": "ok"}
    ready = (await client.get("/ready")).json()
    assert ready["checks"]["database"] == "ok"
    assert (await client.get("/api/v1/runs")).status_code == 401
    assert (await client.get("/api/v1/runs", headers={"Authorization": "Bearer nope"})).status_code == 401
    r = await client.post("/api/v1/auth/login", json={"email": "admin@agentos.dev", "password": "wrong"})
    assert r.status_code == 401 and r.json()["error"]["code"] == "invalid_credentials"
    h = await login(client, "admin@agentos.dev", "agentos-admin")
    me = (await client.get("/api/v1/auth/me", headers=h)).json()
    assert me["user"]["email"] == "admin@agentos.dev" and me["memberships"][0]["role"] == "admin"


async def test_workspace_isolation(client, admin):
    async with unit_of_work() as s:
        other = User(
            id=uuid.uuid4(), email="mallory@example.com", password_hash=hash_password("pw"), role="developer"
        )
        s.add(other)
        await s.flush()
        ws2 = Workspace(id=uuid.uuid4(), name="Other", owner_id=other.id)
        s.add(ws2)
        await s.flush()
        s.add(WorkspaceMember(workspace_id=ws2.id, user_id=other.id, role="admin"))
    wf = await workflow_id(client, admin, "research-report")
    run = (await client.post(f"/api/v1/workflows/{wf}/runs", headers=admin, json={})).json()
    mallory = await login(client, "mallory@example.com", "pw")
    assert (await client.get(f"/api/v1/runs/{run['id']}", headers=mallory)).status_code == 404
    assert (await client.get(f"/api/v1/workflows/{wf}", headers=mallory)).status_code == 404
    assert (await client.get(f"/api/v1/runs/{run['id']}/stream", headers=mallory)).status_code == 404
    ws1 = run["workspace_id"]
    r = await client.get("/api/v1/runs", headers={**mallory, "X-Workspace-Id": ws1})
    assert r.status_code == 404 and r.json()["error"]["code"] == "workspace_not_found"
    assert (await client.get("/api/v1/runs", headers=mallory)).json()["total"] == 0


async def test_role_enforcement(client, viewer, developer):
    body = {"name": "V", "model": "mock-echo", "provider": "mock"}
    assert (await client.post("/api/v1/agents", headers=viewer, json=body)).status_code == 403
    assert (await client.post("/api/v1/agents", headers=developer, json=body)).status_code == 201
    wf = await workflow_id(client, viewer, "research-report")
    assert (await client.post(f"/api/v1/workflows/{wf}/runs", headers=viewer, json={})).status_code == 403
    assert (await client.patch("/api/v1/workspace", headers=developer, json={"name": "x"})).status_code == 403


async def test_agent_crud_and_validation(client, admin):
    r = await client.post(
        "/api/v1/agents", headers=admin, json={"name": "Bad", "model": "m", "allowed_tool_ids": ["shell"]}
    )
    assert r.status_code == 422 and r.json()["error"]["code"] == "unknown_tool"
    r = await client.post(
        "/api/v1/agents", headers=admin, json={"name": "A1", "model": "mock-echo", "max_steps": 0}
    )
    assert r.status_code == 422
    r = await client.post(
        "/api/v1/agents",
        headers=admin,
        json={"name": "A1", "model": "mock-echo", "allowed_tool_ids": ["calculator"]},
    )
    assert r.status_code == 201
    agent = r.json()
    assert (
        await client.post("/api/v1/agents", headers=admin, json={"name": "A1", "model": "x"})
    ).status_code == 409
    r = await client.patch(
        f"/api/v1/agents/{agent['id']}", headers=admin, json={"system_instructions": "be brief"}
    )
    assert r.json()["system_instructions"] == "be brief"
    listing = (await client.get("/api/v1/agents?limit=2", headers=admin)).json()
    assert listing["limit"] == 2 and len(listing["items"]) == 2 and listing["total"] == 7


async def test_workflow_validation_versioning(client, admin):
    r = (await client.post("/api/v1/workflows/validate", headers=admin, json={"definition": CYCLIC})).json()
    assert not r["valid"] and r["issues"][0]["code"] == "cycle"
    r = await client.post("/api/v1/workflows", headers=admin, json={"name": "cyc", "definition": CYCLIC})
    assert r.status_code == 422
    v1 = (
        await client.post("/api/v1/workflows", headers=admin, json={"name": "simple", "definition": SIMPLE})
    ).json()
    v2 = (
        await client.post("/api/v1/workflows", headers=admin, json={"name": "simple", "definition": SIMPLE})
    ).json()
    assert (v1["version"], v2["version"]) == (1, 2) and v1["id"] != v2["id"]
    detail = (await client.get(f"/api/v1/workflows/{v1['id']}", headers=admin)).json()
    assert detail["versions"] == [1, 2] and detail["validation"]["levels"] == {"sum": 0, "agg": 1}
    assert detail["definition"] == v1["definition"]  # v1 unchanged
    unauthorized = {
        "nodes": [{"key": "x", "type": "agent_task", "agent": "Research Agent", "tools": ["artifact_writer"]}]
    }
    r = (
        await client.post("/api/v1/workflows/validate", headers=admin, json={"definition": unauthorized})
    ).json()
    assert r["issues"][0]["code"] == "unauthorized_tool"


async def test_plan_endpoint_with_mock(client, admin):
    r = (
        await client.post(
            "/api/v1/workflows/plan", headers=admin, json={"task": "Compare three AI inference platforms"}
        )
    ).json()
    assert r["valid"] and r["simulated"] and r["provider"] == "mock"
    assert len(r["definition"]["nodes"]) == 5 and r["levels"]["write_report"] == 2
    r = (
        await client.post(
            "/api/v1/workflows/plan",
            headers=admin,
            json={"task": "Compare platforms", "scenario": "invalid_plan"},
        )
    ).json()
    assert not r["valid"] and len(r["attempts"]) == 3
    r = await client.post(
        "/api/v1/workflows/plan", headers=admin, json={"task": "x y z", "provider": "anthropic"}
    )
    assert r.status_code == 422 and r.json()["error"]["code"] == "provider_not_configured"


async def test_run_creation_idempotency(client, admin):
    wf = await workflow_id(client, admin, "research-report")
    h = {**admin, "Idempotency-Key": "abc-123"}
    r1 = await client.post(f"/api/v1/workflows/{wf}/runs", headers=h, json={"input": {"k": 1}})
    r2 = await client.post(f"/api/v1/workflows/{wf}/runs", headers=h, json={"input": {"k": 1}})
    assert r1.status_code == r2.status_code == 201
    assert r1.json()["id"] == r2.json()["id"] and r2.headers.get("Idempotent-Replayed") == "true"
    r3 = await client.post(f"/api/v1/workflows/{wf}/runs", headers=h, json={"input": {"k": 2}})
    assert r3.status_code == 422 and r3.json()["error"]["code"] == "idempotency_key_reused"
    async with unit_of_work() as s:
        assert (await s.execute(select(func.count()).select_from(WorkflowRun))).scalar_one() == 1


async def test_full_flow_over_api_with_sse_replay(client, admin, viewer):
    wf = await workflow_id(client, admin, "research-report")
    run = (await client.post(f"/api/v1/workflows/{wf}/runs", headers=admin, json={})).json()
    run_id = run["id"]
    assert run["status"] == "running" and run["step_counts"] == {"ready": 3, "pending": 2}
    async with running_workers(2):

        async def pending():
            items = (await client.get("/api/v1/approvals?status=pending", headers=admin)).json()["items"]
            return items or None

        (ap,) = await wait_for(pending)
        assert ap["tool_name"] == "artifact_writer" and ap["node_key"] == "write_report"
        assert ap["workflow_name"] == "research-report" and ap["requesting_agent"] == "Writer Agent"
        assert (
            await client.post(f"/api/v1/approvals/{ap['id']}/approve", headers=viewer, json={})
        ).status_code == 403
        h = {**admin, "Idempotency-Key": "decide-1"}
        r1 = await client.post(f"/api/v1/approvals/{ap['id']}/approve", headers=h, json={"reason": "ok"})
        r2 = await client.post(f"/api/v1/approvals/{ap['id']}/approve", headers=h, json={"reason": "ok"})
        assert (
            r1.status_code == 200
            and r2.status_code == 200
            and r2.headers.get("Idempotent-Replayed") == "true"
        )
        r3 = await client.post(f"/api/v1/approvals/{ap['id']}/reject", headers=admin, json={})
        assert r3.status_code == 409 and r3.json()["error"]["code"] == "approval_not_pending"

        async def done():
            r = (await client.get(f"/api/v1/runs/{run_id}", headers=admin)).json()
            return r if r["status"] in ("succeeded", "failed") else None

        final = await wait_for(done)
    assert final["status"] == "succeeded" and final["step_counts"] == {"succeeded": 5}
    assert float(final["cost_spent_usd"]) > 0 and final["duration_ms"] > 0

    steps = (await client.get(f"/api/v1/runs/{run_id}/steps", headers=admin)).json()
    write = next(s for s in steps if s["node_key"] == "write_report")
    assert [a["status"] for a in write["attempts"]] == ["suspended", "succeeded"]
    detail = (await client.get(f"/api/v1/runs/{run_id}/steps/{write['id']}", headers=admin)).json()
    assert (
        detail["agent_name"] == "Writer Agent"
        and detail["approvals"][0]["decided_by_email"] == "admin@agentos.dev"
    )
    assert {t["tool_name"] for t in detail["tool_executions"]} == {"artifact_writer"}
    assert all(u["cost_basis"] == "simulated" for u in detail["model_usage"])

    (artifact,) = (await client.get(f"/api/v1/runs/{run_id}/artifacts", headers=admin)).json()
    dl = await client.get(f"/api/v1/artifacts/{artifact['id']}/download", headers=admin)
    assert dl.status_code == 200 and dl.headers["content-type"].startswith("text/markdown")
    assert (
        "attachment" in dl.headers["content-disposition"]
        and dl.headers["x-content-type-options"] == "nosniff"
    )
    import hashlib

    assert hashlib.sha256(dl.content).hexdigest() == artifact["sha256"]

    events = (await client.get(f"/api/v1/runs/{run_id}/events", headers=admin)).json()
    seqs = [e["sequence_number"] for e in events]
    assert seqs == list(range(1, len(seqs) + 1))
    types = {e["event_type"] for e in events}
    assert {
        "run.created",
        "run.started",
        "step.ready",
        "step.started",
        "model.call.started",
        "model.call.completed",
        "tool.started",
        "tool.completed",
        "approval.requested",
        "approval.resolved",
        "step.completed",
        "run.completed",
    } <= types

    full = await client.get(f"/api/v1/runs/{run_id}/stream", headers=admin)
    assert full.headers["content-type"].startswith("text/event-stream")
    ids = [int(line[4:]) for line in full.text.splitlines() if line.startswith("id: ")]
    assert ids == seqs and "event: stream.end" in full.text
    resumed = await client.get(
        f"/api/v1/runs/{run_id}/stream", headers={**admin, "Last-Event-ID": str(seqs[-5])}
    )
    assert [int(line[4:]) for line in resumed.text.splitlines() if line.startswith("id: ")] == seqs[-4:]

    overview = (await client.get("/api/v1/metrics/overview", headers=admin)).json()
    assert overview["succeeded"] == 1 and overview["includes_simulated_cost"] is True
    usage = (await client.get("/api/v1/metrics/usage", headers=admin)).json()
    assert usage["totals"]["calls"] == final["model_calls"]
    cost = (await client.get("/api/v1/metrics/cost", headers=admin)).json()
    assert cost["total_estimated_cost_usd"] == pytest.approx(float(final["cost_spent_usd"]))
    assert {a["agent"] for a in cost["by_agent"]} == {"Research Agent", "Analysis Agent", "Writer Agent"}
    obs = (await client.get("/api/v1/metrics/observability", headers=admin)).json()
    assert obs["tool_activity"]["document_search"]["succeeded"] == 3 and obs["workers"]


async def test_cancel_over_api(client, admin):
    wf = await workflow_id(client, admin, "research-report")
    run = (await client.post(f"/api/v1/workflows/{wf}/runs", headers=admin, json={})).json()
    r = await client.post(f"/api/v1/runs/{run['id']}/cancel", headers=admin)
    assert r.status_code == 200 and r.json()["status"] == "cancelled"
    assert r.json()["step_counts"] == {"cancelled": 5}
    again = await client.post(f"/api/v1/runs/{run['id']}/cancel", headers=admin)
    assert again.status_code == 409 and again.json()["error"]["code"] == "run_terminal"


async def test_planned_run_over_api(client, admin):
    r = await client.post(
        "/api/v1/runs/planned", headers=admin, json={"task": "Compare the inference platforms"}
    )
    assert r.status_code == 201 and r.json()["status"] == "planning"
    run_id = r.json()["id"]
    async with running_workers(1):

        async def planned():
            steps = (await client.get(f"/api/v1/runs/{run_id}/steps", headers=admin)).json()
            return steps if len(steps) == 6 else None

        steps = await wait_for(planned)
    assert {s["node_key"] for s in steps} >= {"__planner__", "analyze", "write_report"}
    r = await client.post("/api/v1/runs/planned", headers=admin, json={"task": "x y z", "max_cost_usd": 99})
    assert r.status_code == 422


async def test_documents_upload_and_limits(client, admin, viewer):
    files = {
        "file": ("notes.md", b"# Notes\n\n## Section\n\nZebra inference cluster details.", "text/markdown")
    }
    r = await client.post("/api/v1/documents", headers=admin, files=files)
    assert r.status_code == 201 and r.json()["chunk_count"] == 1
    assert (await client.post("/api/v1/documents", headers=viewer, files=files)).status_code == 403
    bad = {"file": ("../evil.md", b"x", "text/markdown")}
    assert (await client.post("/api/v1/documents", headers=admin, files=bad)).status_code == 422
    exe = {"file": ("run.exe", b"MZ", "application/octet-stream")}
    assert (await client.post("/api/v1/documents", headers=admin, files=exe)).status_code == 422
    from agentos_core.tools.document_search import search_documents

    ws_id = r.json()  # noqa: F841
    me = (await client.get("/api/v1/auth/me", headers=admin)).json()
    hits = await search_documents(uuid.UUID(me["memberships"][0]["workspace_id"]), "zebra cluster")
    assert hits[0]["document"] == "notes.md" and hits[0]["citation"] == "notes.md#Notes > Section"


async def test_request_size_limit(client, admin):
    big = {"definition": {"nodes": [], "pad": "x" * (get_settings().max_request_bytes + 10)}}
    r = await client.post("/api/v1/workflows/validate", headers=admin, json=big)
    assert r.status_code == 413 and r.json()["error"]["code"] == "request_too_large"


async def test_rate_limit_on_critical_endpoint(client, admin, monkeypatch):
    monkeypatch.setattr(get_settings(), "critical_rate_limit_per_minute", 2)
    codes = [
        (await client.post("/api/v1/workflows/plan", headers=admin, json={"task": "abc"})).status_code
        for _ in range(4)
    ]
    assert codes[:2] == [200, 200] and codes[-1] == 429


async def test_system_info_never_leaks_secrets(client, admin):
    r = (await client.get("/api/v1/system", headers=admin)).json()
    assert r["providers"]["mock"]["configured"] is True
    assert set(r["providers"]["anthropic"]) == {"configured", "default_model"}
    assert "dev-insecure" not in str(r) and "jwt" not in str(r).lower()


async def test_openapi_document(client):
    spec = (await client.get("/openapi.json")).json()
    for path in [
        "/api/v1/runs/{run_id}/stream",
        "/api/v1/approvals/{approval_id}/approve",
        "/api/v1/metrics/overview",
        "/api/v1/workflows/plan",
        "/api/v1/artifacts/{artifact_id}/download",
    ]:
        assert path in spec["paths"]


def test_request_transaction_commits_before_the_response_is_sent():
    # With FastAPI's default "request" scope the commit would run after the response, so a client
    # could receive a new run id and get a 404 reading it (observed in the recovery demo).
    from typing import get_args

    from agentos.dependencies import SessionDep

    assert get_args(SessionDep)[1].scope == "function"


async def test_priority_pause_resume_and_branching_over_api(client, admin, viewer):
    # priority is validated and exposed
    wf = await workflow_id(client, admin, "research-report")
    bad = await client.post(f"/api/v1/workflows/{wf}/runs", headers=admin, json={"priority": 10})
    assert bad.status_code == 422
    run = (await client.post(f"/api/v1/workflows/{wf}/runs", headers=admin, json={"priority": 7})).json()
    assert run["priority"] == 7 and run["paused_at"] is None and run["depth"] == 0

    # pause / resume: developer+, idempotent, reason recorded, terminal runs refused
    denied = await client.post(f"/api/v1/runs/{run['id']}/pause", headers=viewer)
    assert denied.status_code == 403
    paused = await client.post(f"/api/v1/runs/{run['id']}/pause", headers=admin, json={"reason": "inspect"})
    assert (
        paused.status_code == 200
        and paused.json()["paused_at"]
        and paused.json()["pause_reason"] == "inspect"
    )
    again = await client.post(f"/api/v1/runs/{run['id']}/pause", headers=admin)
    assert again.status_code == 200 and again.json()["paused_at"] == paused.json()["paused_at"]
    resumed = await client.post(f"/api/v1/runs/{run['id']}/resume", headers=admin)
    assert resumed.status_code == 200 and resumed.json()["paused_at"] is None
    await client.post(f"/api/v1/runs/{run['id']}/cancel", headers=admin)
    done = await client.post(f"/api/v1/runs/{run['id']}/pause", headers=admin)
    assert done.status_code == 409 and done.json()["error"]["code"] == "run_terminal"

    # the new node types validate through the normal endpoint, with per-node issues
    bad_def = {
        "nodes": [
            {
                "key": "f",
                "type": "fan_out",
                "over": "input.x",
                "max_items": 99,
                "template": {"type": "tool_task", "tool": "calculator"},
            }
        ]
    }
    v = await client.post("/api/v1/workflows/validate", headers=admin, json={"definition": bad_def})
    assert v.status_code == 200 and v.json()["valid"] is False
    ok_def = {
        "nodes": [
            {"key": "a", "type": "aggregation"},
            {
                "key": "f",
                "type": "fan_out",
                "depends_on": ["a"],
                "over": "steps.a.output.items",
                "max_items": 3,
                "template": {
                    "type": "tool_task",
                    "tool": "calculator",
                    "arguments": {"operation": "add", "values": [{"$ref": "item"}, 1]},
                },
            },
            {
                "key": "s",
                "type": "sub_workflow",
                "workflow": "child",
                "depends_on": ["f"],
                "when": {"ref": "steps.f.output.count", "op": "gt", "value": 0},
            },
        ]
    }
    v = await client.post("/api/v1/workflows/validate", headers=admin, json={"definition": ok_def})
    assert v.json()["valid"] is True
