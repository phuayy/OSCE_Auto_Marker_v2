"""DeepSeek — OpenAI-compatible, cheap, and strong at long structured output.

``deepseek-reasoner`` emits its chain of thought in a separate
``reasoning_content`` field rather than inside ``content``, so no reasoning
switch is needed and the shared content extraction already reads the right
field.
"""
from __future__ import annotations

from app.llm.base import ModelSpec, ProviderDescriptor
from app.llm.providers.openai_compatible import OpenAICompatibleProvider

PROVIDER_ID = "deepseek"

DESCRIPTOR = ProviderDescriptor(
    id=PROVIDER_ID,
    label="DeepSeek",
    vendor="DeepSeek",
    description="Low-cost models with dependable JSON output. Practical primary for high-volume marking.",
    api_key_env=("DEEPSEEK_API_KEY",),
    base_url_env="DEEPSEEK_BASE_URL",
    default_base_url="https://api.deepseek.com/v1",
    requirements="Create a key at platform.deepseek.com and set DEEPSEEK_API_KEY.",
    models=(
        ModelSpec(id="deepseek-chat", label="DeepSeek V3 (chat)", context_window=128_000, max_output_tokens=8_192),
        ModelSpec(
            id="deepseek-reasoner",
            label="DeepSeek R1 (reasoner)",
            context_window=128_000,
            max_output_tokens=8_192,
            supports_reasoning_control=True,
            notes="Reasoning is returned separately from the answer, so it never truncates the JSON.",
        ),
    ),
)


class DeepSeekProvider(OpenAICompatibleProvider):
    descriptor = DESCRIPTOR
