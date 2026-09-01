"""NVIDIA NIM / build.nvidia.com — the project's original provider.

Nemotron deployments accept two switches nothing else does, and both matter:
``chat_template_kwargs.enable_thinking`` and ``reasoning_effort``. With thinking
left on, the hidden trace is billed against ``max_tokens`` and the visible
answer arrives truncated — which is how a rubric once came back scored entirely
"No". ``reasoning_budget`` is therefore sent only when thinking is deliberately
enabled.
"""
from __future__ import annotations

from typing import Any

from app.llm.base import ChatRequest, ModelSpec, ProviderDescriptor, RequestMode
from app.llm.providers.openai_compatible import OpenAICompatibleProvider

PROVIDER_ID = "nvidia"

DESCRIPTOR = ProviderDescriptor(
    id=PROVIDER_ID,
    label="NVIDIA NIM",
    vendor="NVIDIA",
    description="Nemotron and hosted open models on build.nvidia.com. The default for this deployment.",
    api_key_env=("NVIDIA_API_KEY",),
    base_url_env="NVIDIA_BASE_URL",
    default_base_url="https://integrate.api.nvidia.com/v1",
    requirements="Create an API key at build.nvidia.com and set NVIDIA_API_KEY.",
    models=(
        ModelSpec(
            id="nvidia/nemotron-3-super-120b-a12b",
            label="Nemotron 3 Super 120B",
            context_window=128_000,
            supports_reasoning_control=True,
            notes="The rubric-scoring default. Long transcripts routinely need 5+ minutes.",
        ),
        ModelSpec(
            id="nvidia/llama-3.3-nemotron-super-49b-v1.5",
            label="Llama 3.3 Nemotron Super 49B",
            context_window=128_000,
            supports_reasoning_control=True,
        ),
        ModelSpec(
            id="meta/llama-3.3-70b-instruct",
            label="Llama 3.3 70B Instruct",
            context_window=128_000,
        ),
        ModelSpec(
            id="deepseek-ai/deepseek-r1",
            label="DeepSeek R1 (NVIDIA-hosted)",
            context_window=128_000,
            supports_reasoning_control=True,
        ),
    ),
)


class NvidiaProvider(OpenAICompatibleProvider):
    descriptor = DESCRIPTOR

    def extra_body(self, request: ChatRequest, mode: RequestMode) -> dict[str, Any]:
        # PLAIN is the rung reached after the vendor rejected a richer body, so
        # it sends nothing beyond the standard fields.
        if mode is RequestMode.PLAIN:
            return {}
        reasoning = request.reasoning
        body: dict[str, Any] = {
            "reasoning_effort": reasoning.effort if reasoning.enabled else "none",
            "chat_template_kwargs": {"enable_thinking": bool(reasoning.enabled)},
        }
        if reasoning.enabled:
            body["reasoning_budget"] = reasoning.budget_tokens
        return body
