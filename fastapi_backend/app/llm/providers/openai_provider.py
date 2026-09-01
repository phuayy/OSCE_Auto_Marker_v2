"""OpenAI's own API.

Named ``openai_provider`` rather than ``openai`` so the module never shadows the
SDK package for anything importing it by name.

The endpoint is strict: it rejects unknown body fields outright, so no
``extra_body`` is ever sent. Reasoning models are steered by model choice, not
by a per-request switch.
"""
from __future__ import annotations

from app.llm.base import ModelSpec, ProviderDescriptor
from app.llm.providers.openai_compatible import OpenAICompatibleProvider

PROVIDER_ID = "openai"

DESCRIPTOR = ProviderDescriptor(
    id=PROVIDER_ID,
    label="OpenAI",
    vendor="OpenAI",
    description="GPT models direct from OpenAI. Reliable JSON mode; the least surprising fallback.",
    api_key_env=("OPENAI_API_KEY",),
    base_url_env="OPENAI_BASE_URL",
    default_base_url="https://api.openai.com/v1",
    requirements="Create a key at platform.openai.com/api-keys and set OPENAI_API_KEY.",
    models=(
        ModelSpec(id="gpt-4.1", label="GPT-4.1", context_window=1_000_000, max_output_tokens=32_768),
        ModelSpec(id="gpt-4.1-mini", label="GPT-4.1 mini", context_window=1_000_000, max_output_tokens=32_768),
        ModelSpec(id="gpt-4o", label="GPT-4o", context_window=128_000, max_output_tokens=16_384),
        ModelSpec(
            id="o4-mini",
            label="o4-mini (reasoning)",
            context_window=200_000,
            max_output_tokens=100_000,
            supports_reasoning_control=True,
            notes="Reasoning tokens count against the output budget; leave max tokens generous.",
        ),
    ),
)


class OpenAIProvider(OpenAICompatibleProvider):
    descriptor = DESCRIPTOR
