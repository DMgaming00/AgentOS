from __future__ import annotations

from agentos_core.config import get_settings
from agentos_core.domain.errors import ProviderNotConfiguredError
from agentos_core.providers.base import ModelProvider
from agentos_core.providers.mock import MockProvider

_cache: dict[str, ModelProvider] = {}


def get_provider(name: str) -> ModelProvider:
    """Return the provider by name. Never silently substitutes the mock for a real provider."""
    if name in _cache:
        return _cache[name]
    settings = get_settings()
    provider: ModelProvider
    if name == "mock":
        provider = MockProvider()
    elif name == "fixture":
        from agentos_core.providers.fixtures import FixtureProvider

        provider = FixtureProvider()
    elif name == "anthropic":
        from agentos_core.providers.anthropic import AnthropicProvider

        key = settings.anthropic_api_key.get_secret_value() if settings.anthropic_api_key else None
        provider = AnthropicProvider(key)
    elif name == "openai":
        from agentos_core.providers.openai import OpenAIProvider

        key = settings.openai_api_key.get_secret_value() if settings.openai_api_key else None
        provider = OpenAIProvider(key)
    else:
        raise ProviderNotConfiguredError(f"unknown provider {name!r}")
    _cache[name] = provider
    return provider


def provider_status() -> dict[str, dict[str, object]]:
    s = get_settings()
    return {
        "mock": {"configured": True, "default_model": "mock", "simulated": True},
        "fixture": {"configured": True, "default_model": "fixture", "simulated": True},
        "anthropic": {"configured": s.anthropic_api_key is not None, "default_model": s.anthropic_model},
        "openai": {"configured": s.openai_api_key is not None, "default_model": s.openai_model or None},
    }


def clear_cache() -> None:
    _cache.clear()
