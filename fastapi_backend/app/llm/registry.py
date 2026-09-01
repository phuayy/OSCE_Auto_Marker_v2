"""The set of LLM providers this build knows about.

Adding a provider is: write the module, add one line to ``PROVIDER_FACTORIES``.
Nothing else enumerates providers — the settings API, the request validation,
the router and the subprocess bootstrap all read this registry, so a new entry
appears in the settings dropdowns without a frontend change.

Order matters only for presentation: the settings screen lists providers in the
order declared here.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Mapping

from app.llm.base import LLMProvider, ProviderCredentials, ProviderDescriptor
from app.llm.providers import (
    anthropic_provider,
    deepseek,
    gemini,
    nvidia,
    openai_provider,
    openrouter,
)

# The provider assumed when nothing has been selected. NVIDIA, because it is
# the vendor this project's prompts and scoring thresholds were tuned against.
DEFAULT_PROVIDER_ID = nvidia.PROVIDER_ID

ProviderFactory = Callable[[ProviderCredentials], LLMProvider]

PROVIDER_FACTORIES: dict[str, ProviderFactory] = {
    nvidia.PROVIDER_ID: nvidia.NvidiaProvider,
    openai_provider.PROVIDER_ID: openai_provider.OpenAIProvider,
    anthropic_provider.PROVIDER_ID: anthropic_provider.AnthropicProvider,
    deepseek.PROVIDER_ID: deepseek.DeepSeekProvider,
    gemini.PROVIDER_ID: gemini.GeminiProvider,
    openrouter.PROVIDER_ID: openrouter.OpenRouterProvider,
}

DESCRIPTORS: dict[str, ProviderDescriptor] = {
    nvidia.PROVIDER_ID: nvidia.DESCRIPTOR,
    openai_provider.PROVIDER_ID: openai_provider.DESCRIPTOR,
    anthropic_provider.PROVIDER_ID: anthropic_provider.DESCRIPTOR,
    deepseek.PROVIDER_ID: deepseek.DESCRIPTOR,
    gemini.PROVIDER_ID: gemini.DESCRIPTOR,
    openrouter.PROVIDER_ID: openrouter.DESCRIPTOR,
}


def provider_ids() -> list[str]:
    return list(PROVIDER_FACTORIES)


def descriptor_for(provider_id: str) -> ProviderDescriptor | None:
    return DESCRIPTORS.get(str(provider_id or "").strip())


def build_provider(provider_id: str, credentials: ProviderCredentials) -> LLMProvider:
    """Instantiate one provider. Raises ``KeyError`` for an unknown id — callers
    that accept operator input resolve the id against the registry first."""
    return PROVIDER_FACTORIES[str(provider_id).strip()](credentials)


def build_all(credentials_by_provider: Mapping[str, ProviderCredentials]) -> dict[str, LLMProvider]:
    """Instantiate every provider.

    Construction is inert — no network, no client objects, no key validation —
    so every provider is built even where no key is configured. That is what
    lets the settings screen list a provider as *available: false* with a
    reason instead of omitting it and leaving the operator guessing.
    """
    return {
        provider_id: factory(credentials_by_provider.get(provider_id) or ProviderCredentials())
        for provider_id, factory in PROVIDER_FACTORIES.items()
    }
