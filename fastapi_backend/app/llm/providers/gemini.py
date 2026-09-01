"""Google Gemini through its OpenAI-compatible endpoint.

Using the compatibility layer rather than ``google-genai`` keeps Gemini on the
same code path as every other vendor and adds no dependency. The trade is that
Gemini-only features (thinking budgets, safety settings) are unavailable here;
if one is ever needed, this module grows a native client without touching the
router.
"""
from __future__ import annotations

from app.llm.base import ModelSpec, ProviderDescriptor
from app.llm.providers.openai_compatible import OpenAICompatibleProvider

PROVIDER_ID = "gemini"

DESCRIPTOR = ProviderDescriptor(
    id=PROVIDER_ID,
    label="Google Gemini",
    vendor="Google",
    description="Gemini via Google's OpenAI-compatible endpoint. Very large context — useful for long stations.",
    api_key_env=("GEMINI_API_KEY", "GOOGLE_API_KEY"),
    base_url_env="GEMINI_BASE_URL",
    default_base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
    requirements="Create a key at aistudio.google.com/apikey and set GEMINI_API_KEY.",
    models=(
        ModelSpec(
            id="gemini-2.5-pro",
            label="Gemini 2.5 Pro",
            context_window=1_048_576,
            max_output_tokens=65_536,
            supports_reasoning_control=True,
        ),
        ModelSpec(
            id="gemini-2.5-flash",
            label="Gemini 2.5 Flash",
            context_window=1_048_576,
            max_output_tokens=65_536,
            notes="Fast and cheap; a sensible fallback rather than a primary marker.",
        ),
    ),
)


class GeminiProvider(OpenAICompatibleProvider):
    descriptor = DESCRIPTOR
