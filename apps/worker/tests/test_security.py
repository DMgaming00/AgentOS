"""Security boundaries enforced in code (not by prompts)."""

import json

import pytest

from agentos_core.config import Settings
from agentos_core.domain.errors import PolicyViolationError
from agentos_core.engine import core
from agentos_core.providers.base import Message, ModelConfig, ToolCall
from agentos_core.providers.mock import MockProvider, tool_rounds
from agentos_core.storage import Storage
from agentos_core.testing import add_agent, get_steps, running_workers, start_run, wait_run_status
from agentos_core.tools.registry import get_registry
from agentos_core.tools.runner import StepScope, ToolCallRequest, execute_tool_call


def test_production_refuses_dev_defaults():
    with pytest.raises(RuntimeError, match="JWT"):
        Settings(environment="production").ensure_safe()
    with pytest.raises(RuntimeError, match="DEV_AUTH"):
        Settings(environment="production", jwt_secret="x" * 40).ensure_safe()
    with pytest.raises(RuntimeError, match="FAULT_INJECTION"):
        Settings(environment="production", jwt_secret="x" * 40, dev_auth_enabled=False).ensure_safe()
    with pytest.raises(RuntimeError, match="JWT"):
        Settings(
            environment="production", jwt_secret="short", dev_auth_enabled=False, allow_fault_injection=False
        ).ensure_safe()
    Settings(
        environment="production", jwt_secret="x" * 40, dev_auth_enabled=False, allow_fault_injection=False
    ).ensure_safe()


class InjectionFollower(MockProvider):
    """A deliberately gullible 'model' that obeys instructions found in documents."""

    def _research(self, messages, tools, config):
        rounds = tool_rounds(messages)
        if not rounds:
            return "", [ToolCall("c1", "document_search", {"query": "system notice administrator priority"})]
        if len(rounds) == 1:
            hits = json.loads(rounds[-1].tool_results[0].content)["results"]
            if any("ignore all previous instructions" in h["content"] for h in hits):
                return "", [
                    ToolCall("c2", "artifact_writer", {"filename": "secrets.md", "content": "API KEYS"})
                ]
        return json.dumps({"obeyed_injection": True, "last": rounds[-1].tool_results[0].content}), []


@pytest.mark.integration
async def test_prompt_injection_in_documents_cannot_grant_tools(seeded, monkeypatch):
    from agentos_core.providers import factory

    monkeypatch.setitem(factory._cache, "mock", InjectionFollower())
    await add_agent(seeded, "Gullible", scenario="research", tools=["document_search"])
    run_id = await start_run(seeded, {"nodes": [{"key": "r", "type": "agent_task", "agent": "Gullible"}]})
    async with running_workers(1):
        run = await wait_run_status(run_id, {"succeeded", "failed"})
    out = (await get_steps(run_id))["r"].output
    # The model *tried* to follow the injected instruction; the platform refused.
    assert run.status == "succeeded" and out["obeyed_injection"] is True
    assert "not permitted" in out["last"]
    from sqlalchemy import func, select

    from agentos_core.db.models import ApprovalRequest, Artifact
    from agentos_core.engine.tx import unit_of_work

    async with unit_of_work() as s:
        assert (await s.execute(select(func.count()).select_from(Artifact))).scalar_one() == 0
        assert (await s.execute(select(func.count()).select_from(ApprovalRequest))).scalar_one() == 0


@pytest.mark.integration
async def test_data_reader_is_confined_to_workspace(seeded):
    run_id = await start_run(seeded, {"nodes": [{"key": "a", "type": "aggregation"}]})
    (c,) = await core.claim_steps("w", 1)
    scope = StepScope(
        workspace_id=seeded.workspace.id,
        run_id=run_id,
        step_id=c.step_id,
        node_key="a",
        attempt_id=c.attempt_id,
        lease_token=c.lease_token,
        agent_name="x",
        allowed_tools=frozenset({"structured_data_reader"}),
        storage=Storage(),
    )
    ok = await execute_tool_call(
        ToolCallRequest("1", "structured_data_reader", {"filename": "benchmarks.csv"}), scope, get_registry()
    )
    assert ok.kind == "result" and ok.content["rows"][1]["platform"] == "Helix Serve"
    for bad in ("../../etc/passwd", "/etc/passwd", "platform-nimbus.md"):
        out = await execute_tool_call(
            ToolCallRequest(bad, "structured_data_reader", {"filename": bad}), scope, get_registry()
        )
        assert out.kind == "error", bad


def test_storage_never_leaves_root(tmp_path):
    s = Storage(tmp_path / "root")
    (tmp_path / "secret.txt").write_text("x")
    with pytest.raises(PolicyViolationError):
        s.read_bytes("../secret.txt")


async def test_mock_never_substitutes_for_real_provider():
    from agentos_core.domain.errors import ProviderNotConfiguredError
    from agentos_core.providers import factory

    factory._cache.pop("anthropic", None)
    with pytest.raises(ProviderNotConfiguredError):
        factory.get_provider("anthropic")
    msg = Message(role="user", text="x")
    assert (await MockProvider().generate("s", [msg], [], ModelConfig(model="mock-echo"))).provider == "mock"
