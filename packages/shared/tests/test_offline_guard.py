"""Offline execution guarantee: paid-provider requests cannot happen unless explicitly enabled.

The proof does not rely on mocks of the guard: a socket spy records every outbound connection attempt
and an HTTP-transport spy records every request that reaches the real transport. With credentials present
and the default (offline) configuration, both must stay empty.
"""

import socket

import anthropic
import httpx2
import pytest

from agentos_core.config import get_settings
from agentos_core.domain.errors import BudgetExceededError, ProviderNotConfiguredError
from agentos_core.providers.base import Message, ModelConfig
from agentos_core.providers.execution import (
    ExecutionPolicy,
    OfflineModeError,
    check_provider_call,
    enforce_real_spend,
    execution_status,
)
from agentos_core.providers.factory import clear_cache, get_provider

LIVE_ENV = {
    "AGENTOS_EXECUTION_MODE": "live",
    "AGENTOS_ALLOW_PAID_PROVIDERS": "true",
    "AGENTOS_ENABLE_NETWORK_MODEL_REQUESTS": "true",
    "AGENTOS_MAX_REAL_API_SPEND_USD": "1",
}
MSGS = [Message(role="user", text="hi")]
CFG = ModelConfig(model="claude-opus-5-5", max_tokens=50)


def configure(monkeypatch, **env):
    for k in LIVE_ENV:
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    get_settings.cache_clear()
    clear_cache()


@pytest.fixture(autouse=True)
def reset_settings():
    yield
    get_settings.cache_clear()
    clear_cache()


@pytest.fixture
def network_spy(monkeypatch):
    """Records outbound socket connections to non-loopback hosts and requests reaching a real transport."""
    seen = {"connects": [], "transport_requests": 0}
    real_connect = socket.socket.connect

    def connect(self, address):
        host = address[0] if isinstance(address, tuple) else str(address)
        if host not in ("127.0.0.1", "::1", "localhost"):
            seen["connects"].append(address)
            raise AssertionError(f"outbound connection attempted to {address}")
        return real_connect(self, address)

    async def inner(self, request):
        seen["transport_requests"] += 1
        raise AssertionError("a request reached the real HTTP transport")

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(httpx2.AsyncHTTPTransport, "handle_async_request", inner)
    return seen


def test_defaults_are_offline():
    s = get_settings()
    assert (s.execution_mode, s.allow_paid_providers, s.enable_network_model_requests) == (
        "offline",
        False,
        False,
    )
    assert s.max_real_api_spend_usd == 0
    assert not ExecutionPolicy.current().paid_requests_allowed
    assert "Simulated (offline)" in str(execution_status()["label"])


@pytest.mark.parametrize("provider", ["anthropic", "openai"])
async def test_adapters_refuse_before_any_connection_even_with_a_key(monkeypatch, network_spy, provider):
    configure(monkeypatch, ANTHROPIC_API_KEY="sk-ant-test-not-real", OPENAI_API_KEY="sk-test-not-real")
    p = get_provider(provider)
    with pytest.raises(OfflineModeError) as e:
        await p.generate("s", MSGS, [], ModelConfig(model="some-model", max_tokens=50))
    assert e.value.error_type == "provider_blocked_offline"
    assert "AGENTOS_EXECUTION_MODE is not 'live'" in e.value.message
    assert network_spy == {"connects": [], "transport_requests": 0}


def caused_by_offline(exc: BaseException | None) -> bool:
    # Depending on the SDK, the transport error surfaces directly or wrapped in APIConnectionError.
    while exc is not None:
        if isinstance(exc, OfflineModeError):
            return True
        exc = exc.__cause__ or exc.__context__
    return False


async def test_calling_the_sdk_client_directly_cannot_bypass_the_guard(monkeypatch, network_spy):
    configure(monkeypatch, ANTHROPIC_API_KEY="sk-ant-test-not-real", OPENAI_API_KEY="sk-test-not-real")
    a, o = get_provider("anthropic"), get_provider("openai")
    with pytest.raises(Exception) as ea:
        await a.client.messages.create(model="x", max_tokens=5, messages=[{"role": "user", "content": "hi"}])
    with pytest.raises(Exception) as eo:
        await o.client.responses.create(model="x", input="hi")
    assert caused_by_offline(ea.value) and caused_by_offline(eo.value)
    assert network_spy == {"connects": [], "transport_requests": 0}


@pytest.mark.parametrize("missing", sorted(LIVE_ENV))
async def test_every_flag_is_required(monkeypatch, missing):
    env = {k: v for k, v in LIVE_ENV.items() if k != missing}
    configure(monkeypatch, **env)
    with pytest.raises(OfflineModeError):
        check_provider_call("anthropic", "m")


def test_all_flags_together_unblock_the_policy(monkeypatch):
    configure(monkeypatch, **LIVE_ENV)
    check_provider_call("anthropic", "m")
    check_provider_call("openai", "m")
    assert ExecutionPolicy.current().blockers() == []


def test_networkless_providers_are_always_allowed(monkeypatch):
    configure(monkeypatch)
    check_provider_call("mock")
    check_provider_call("fixture")


async def test_mock_and_fixture_work_offline_with_no_network(monkeypatch, network_spy):
    configure(monkeypatch, ANTHROPIC_API_KEY="sk-ant-test-not-real")
    r = await get_provider("mock").generate(
        "s", MSGS, [], ModelConfig(model="mock-research", max_tokens=500, extra={"scenario": "research"})
    )
    assert r.provider == "mock" and r.usage.simulated
    assert network_spy["connects"] == []


async def test_contract_transport_is_allowed_offline_because_it_cannot_reach_the_network(monkeypatch):
    from agentos_core.providers.anthropic import AnthropicProvider

    configure(monkeypatch)
    body = {
        "id": "m",
        "type": "message",
        "role": "assistant",
        "model": "claude-opus-5-5",
        "content": [{"type": "text", "text": "ok"}],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }
    client = anthropic.AsyncAnthropic(
        api_key="k",
        max_retries=0,
        http_client=anthropic.DefaultAsyncHttpxClient(
            transport=httpx2.MockTransport(lambda r: httpx2.Response(200, json=body))
        ),
    )
    r = await AnthropicProvider(None, client=client).generate("s", MSGS, [], CFG)
    assert r.text == "ok"


async def test_live_provider_failure_is_an_error_not_a_mock_answer(monkeypatch):
    from agentos_core.domain.errors import ProviderUnavailableError
    from agentos_core.providers.anthropic import AnthropicProvider

    configure(monkeypatch, **LIVE_ENV)
    client = anthropic.AsyncAnthropic(
        api_key="k",
        max_retries=0,
        http_client=anthropic.DefaultAsyncHttpxClient(
            transport=httpx2.MockTransport(
                lambda r: httpx2.Response(
                    503, json={"type": "error", "error": {"type": "overloaded_error", "message": "x"}}
                )
            )
        ),
    )
    with pytest.raises(ProviderUnavailableError):
        await AnthropicProvider(None, client=client).generate("s", MSGS, [], CFG)


def test_missing_key_never_falls_back_to_mock(monkeypatch):
    configure(monkeypatch, **LIVE_ENV)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    get_settings.cache_clear()
    with pytest.raises(ProviderNotConfiguredError):
        get_provider("anthropic")


async def test_spend_cap_is_enforced_against_recorded_spend(monkeypatch):
    from decimal import Decimal

    import agentos_core.providers.execution as ex

    configure(monkeypatch, **LIVE_ENV)

    async def spent():
        return Decimal("0.97")

    monkeypatch.setattr(ex, "real_spend_usd", spent)
    await enforce_real_spend("anthropic", Decimal("0.02"))
    with pytest.raises(BudgetExceededError):
        await enforce_real_spend("anthropic", Decimal("0.05"))
    await enforce_real_spend("mock", Decimal("100"))  # mock is never counted
