"""Offline guarantee through the real engine: workers never reach a paid provider and never substitute the mock."""

import socket

import pytest
from sqlalchemy import select

from agentos_core.config import get_settings
from agentos_core.db.models import ModelUsage, StepAttempt
from agentos_core.engine.tx import unit_of_work
from agentos_core.providers.factory import clear_cache
from agentos_core.testing import add_agent, get_steps, running_workers, start_run, wait_run_status

pytestmark = pytest.mark.integration

ONE_STEP = {"nodes": [{"key": "a", "type": "agent_task", "agent": "Agent", "retry": {"max_attempts": 3}}]}


@pytest.fixture
def credentials_present(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-not-real")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-real")
    get_settings.cache_clear()
    clear_cache()
    yield
    get_settings.cache_clear()
    clear_cache()


@pytest.fixture
def no_outbound(monkeypatch):
    attempts = []
    real = socket.socket.connect

    def connect(self, address):
        host = address[0] if isinstance(address, tuple) else str(address)
        if host not in ("127.0.0.1", "::1", "localhost"):
            attempts.append(address)
            raise AssertionError(f"outbound connection to {address}")
        return real(self, address)

    monkeypatch.setattr(socket.socket, "connect", connect)
    return attempts


@pytest.mark.parametrize("provider,model", [("anthropic", "claude-opus-5-5"), ("openai", "some-model")])
async def test_real_provider_agent_fails_closed_offline_with_keys_present(
    seeded, credentials_present, no_outbound, provider, model
):
    await add_agent(seeded, "Agent", scenario="research", tools=[], provider=provider, model=model)
    run_id = await start_run(seeded, ONE_STEP)
    async with running_workers(1):
        run = await wait_run_status(run_id, {"succeeded", "failed"}, timeout=30)
    assert run.status == "failed"
    step = (await get_steps(run_id))["a"]
    assert step.error["type"] == "provider_blocked_offline"
    async with unit_of_work() as s:
        attempts = (await s.execute(select(StepAttempt).where(StepAttempt.run_id == run_id))).scalars().all()
        usage = (await s.execute(select(ModelUsage).where(ModelUsage.run_id == run_id))).scalars().all()
    assert len(attempts) == 1  # permanent error: not retried
    assert [(u.provider, u.status, u.error_type) for u in usage] == [
        (provider, "failed", "provider_blocked_offline")
    ]
    assert all(u.provider != "mock" for u in usage)  # no silent mock substitution
    assert no_outbound == []


async def test_fixture_provider_runs_through_the_engine_and_is_priced_as_simulated(seeded, no_outbound):
    await add_agent(
        seeded,
        "Agent",
        scenario="x",
        tools=[],
        provider="fixture",
        model="fixture-smoke",
        system_instructions="fixture-smoke",
        config={"fixture_id": "greeting"},
    )
    run_id = await start_run(seeded, ONE_STEP)
    async with running_workers(1):
        run = await wait_run_status(run_id, {"succeeded", "failed"}, timeout=30)
    assert run.status == "succeeded"
    async with unit_of_work() as s:
        usage = (await s.execute(select(ModelUsage).where(ModelUsage.run_id == run_id))).scalars().all()
    assert [(u.provider, u.cost_basis) for u in usage] == [("fixture", "simulated")]
    assert no_outbound == []
