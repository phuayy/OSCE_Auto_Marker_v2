"""OpenRouter — one key, many vendors.

Useful as a fallback target precisely because it is not a single vendor: if
NVIDIA is down, an OpenRouter route to a different lab keeps the run alive.
OpenRouter's own ``reasoning`` object switches a model's trace off across
vendors that each spell it differently, so it is worth sending on the first
rung of the ladder.
"""
from __future__ import annotations

from typing import Any

from app.llm.base import ChatRequest, ModelSpec, ProviderDescriptor, RequestMode
from app.llm.providers.openai_compatible import OpenAICompatibleProvider

PROVIDER_ID = "openrouter"

DESCRIPTOR = ProviderDescriptor(
    id=PROVIDER_ID,
    label="OpenRouter",
    vendor="OpenRouter",
    description="Gateway to many labs behind one key. A good fallback: it fails independently of any single vendor.",
    api_key_env=("OPENROUTER_API_KEY",),
    base_url_env="OPENROUTER_BASE_URL",
    default_base_url="https://openrouter.ai/api/v1",
    requirements="Create a key at openrouter.ai/keys and set OPENROUTER_API_KEY.",
    models=(
        ModelSpec(id="openai/gpt-4.1", label="GPT-4.1 (via OpenRouter)", context_window=1_000_000),
        ModelSpec(
            id="anthropic/claude-sonnet-4.5",
            label="Claude Sonnet 4.5 (via OpenRouter)",
            context_window=200_000,
        ),
        ModelSpec(id="deepseek/deepseek-chat", label="DeepSeek V3 (via OpenRouter)", context_window=128_000),
        ModelSpec(
            id="google/gemini-2.5-pro",
            label="Gemini 2.5 Pro (via OpenRouter)",
            context_window=1_000_000,
        ),
        ModelSpec(
            id="meta-llama/llama-3.3-70b-instruct",
            label="Llama 3.3 70B (via OpenRouter)",
            context_window=128_000,
        ),
    ),
)


class OpenRouterProvider(OpenAICompatibleProvider):
    descriptor = DESCRIPTOR

    def extra_body(self, request: ChatRequest, mode: RequestMode) -> dict[str, Any]:
        if mode is not RequestMode.STRUCTURED:
            return {}
        return {"reasoning": {"enabled": bool(request.reasoning.enabled)}}
