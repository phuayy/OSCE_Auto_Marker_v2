"""A request is shaped for the model it is sent to, not for the deployment default.

``LLM_MAX_TOKENS`` is one number for every target, but each shortlisted model
declares its own output ceiling and some (OpenAI's o-series) spell the budget
differently and refuse sampling controls outright. A vendor that gets the wrong
shape answers 400, which the router treats as terminal for the target - so a
panel marker on such a model always degraded while the Settings test, sending a
tiny request, stayed green.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

import pytest

from app.llm.base import (
    ChatRequest,
    ModelSpec,
    ProviderCredentials,
    ProviderDescriptor,
    RequestMode,
)
from app.llm.providers.anthropic_provider import AnthropicProvider
from app.llm.providers.deepseek import DeepSeekProvider
from app.llm.providers.openai_compatible import OpenAICompatibleProvider
from app.llm.providers.openai_provider import OpenAIProvider
from app.llm.router import LLMRouter
from app.llm.routing import LLMTarget, RetryPolicy, RoutingConfig
from app.llm import base as llm_base

from tests.test_llm_settings_api import build_service

CREDENTIALS = ProviderCredentials(api_key="key")
MESSAGES = [{"role": "user", "content": "hi"}]


def request(max_tokens: int = 24_576) -> ChatRequest:
    return ChatRequest(messages=MESSAGES, max_tokens=max_tokens)


def custom_provider(spec: ModelSpec) -> OpenAICompatibleProvider:
    provider = OpenAICompatibleProvider(CREDENTIALS)
    provider.descriptor = ProviderDescriptor(
        id="lab", label="Lab", vendor="Lab", description="t", api_key_env=("LAB_KEY",),
        default_base_url="https://lab.test/v1", models=(spec,),
    )
    return provider


def test_deepseek_budget_is_clamped_to_the_model_limit() -> None:
    payload = DeepSeekProvider(CREDENTIALS).build_payload(request(24_576), "deepseek-chat", RequestMode.PLAIN)
    assert payload["max_tokens"] == 8_192


def test_budget_below_the_limit_is_unchanged() -> None:
    payload = DeepSeekProvider(CREDENTIALS).build_payload(request(4_000), "deepseek-chat", RequestMode.PLAIN)
    assert payload["max_tokens"] == 4_000


def test_unknown_model_id_keeps_the_configured_budget() -> None:
    payload = DeepSeekProvider(CREDENTIALS).build_payload(request(24_576), "my-own-finetune", RequestMode.PLAIN)
    assert payload["max_tokens"] == 24_576


def test_spec_without_a_declared_limit_keeps_the_configured_budget() -> None:
    provider = custom_provider(ModelSpec(id="m", label="M", max_output_tokens=0))
    assert provider.build_payload(request(24_576), "m", RequestMode.PLAIN)["max_tokens"] == 24_576


def test_reasoning_model_uses_max_completion_tokens_and_no_sampling() -> None:
    payload = OpenAIProvider(CREDENTIALS).build_payload(request(24_576), "o4-mini", RequestMode.JSON_ONLY)
    assert payload["max_completion_tokens"] == 24_576
    assert "max_tokens" not in payload
    assert "temperature" not in payload
    assert "top_p" not in payload


def test_reasoning_model_budget_is_clamped_too() -> None:
    payload = OpenAIProvider(CREDENTIALS).build_payload(request(500_000), "o4-mini", RequestMode.PLAIN)
    assert payload["max_completion_tokens"] == 100_000


def test_chat_model_keeps_max_tokens_and_sampling() -> None:
    payload = OpenAIProvider(CREDENTIALS).build_payload(request(), "gpt-4o", RequestMode.PLAIN)
    assert payload["max_tokens"] == 16_384
    assert "max_completion_tokens" not in payload
    assert payload["temperature"] == 0.2 and payload["top_p"] == 0.9


def test_anthropic_adapter_clamps(monkeypatch) -> None:
    provider = AnthropicProvider(CREDENTIALS)
    provider.descriptor = ProviderDescriptor(
        id="anthropic", label="A", vendor="A", description="t", api_key_env=("K",),
        default_base_url="https://a.test/v1", models=(ModelSpec(id="small", label="S", max_output_tokens=4_096),),
    )
    sent: dict[str, Any] = {}

    def fake_post(payload, timeout, model):
        sent.update(payload)
        return {"content": [{"type": "text", "text": "x" * 80}], "stop_reason": "end_turn"}, 200, {}

    monkeypatch.setattr(provider, "_post", fake_post)
    provider.complete(request(24_576), "small", RequestMode.PLAIN)
    assert sent["max_tokens"] == 4_096
    provider.complete(request(24_576), "unlisted", RequestMode.PLAIN)
    assert sent["max_tokens"] == 24_576


def test_clamp_is_logged_once(caplog) -> None:
    llm_base._CLAMP_LOGGED.clear()
    provider = DeepSeekProvider(CREDENTIALS)
    with caplog.at_level(logging.INFO, logger="app.llm.base"):
        for _ in range(3):
            provider.build_payload(request(24_576), "deepseek-chat", RequestMode.PLAIN)
    clamps = [r for r in caplog.records if "clamp" in r.getMessage().lower()]
    assert len(clamps) == 1
    assert "key" not in clamps[0].getMessage()


class Capturing(OpenAICompatibleProvider):
    def __init__(self, provider_id: str, spec: ModelSpec) -> None:
        super().__init__(CREDENTIALS)
        self.descriptor = ProviderDescriptor(
            id=provider_id, label=provider_id, vendor=provider_id, description="t",
            api_key_env=("K",), default_base_url="https://x.test/v1", models=(spec,),
        )
        self.payloads: list[dict[str, Any]] = []

    def build_payload(self, request, model, mode):
        payload = super().build_payload(request, model, mode)
        self.payloads.append(payload)
        return payload

    def client(self, timeout_seconds):
        class Completions:
            def create(self, **payload):
                raise RuntimeError("stop here")

        class Chat:
            completions = Completions()

        class Client:
            chat = Chat()

        return Client()


def test_fallback_target_gets_its_own_clamp() -> None:
    primary = Capturing("big", ModelSpec(id="p", label="P", max_output_tokens=32_000))
    fallback = Capturing("small", ModelSpec(id="f", label="F", max_output_tokens=8_192))
    router = LLMRouter(
        {"big": primary, "small": fallback},
        RoutingConfig(
            primary=LLMTarget("big", "p"),
            fallbacks=(LLMTarget("small", "f"),),
            retry=RetryPolicy(max_attempts_per_mode=1),
        ),
        sleep=lambda _s: None,
    )
    with pytest.raises(Exception):
        router.complete(request(24_576))
    assert {p["max_tokens"] for p in primary.payloads} == {24_576}
    assert {p["max_tokens"] for p in fallback.payloads} == {8_192}


def test_connection_test_sends_the_same_request_shape_as_a_real_run(tmp_path: Path, monkeypatch) -> None:
    """The probe must fail wherever the real request would, so it uses the real shape."""
    from app.llm.runtime import request_defaults_from_env

    seen: list[dict[str, Any]] = []

    class FakeResponse:
        error = None
        usage = None
        choices = [type("C", (), {"finish_reason": "stop", "message": type("M", (), {"content": '{"ok": true}'})()})()]

    class FakeClient:
        class chat:  # noqa: N801
            class completions:  # noqa: N801
                @staticmethod
                def create(**payload):
                    seen.append(payload)
                    return FakeResponse()

    monkeypatch.setattr(OpenAICompatibleProvider, "client", lambda self, timeout: FakeClient())
    service = build_service(tmp_path, openai="sk-test")

    result = asyncio.run(service.test_target("openai", "o4-mini"))
    assert result["ok"] is True

    real = OpenAIProvider(CREDENTIALS).build_payload(request_defaults_from_env(), "o4-mini", RequestMode.STRUCTURED)
    probe = seen[0]
    for key in ("max_completion_tokens",):
        assert probe[key] == real[key]
    assert ("max_tokens" in probe) == ("max_tokens" in real)
    assert ("temperature" in probe) == ("temperature" in real)
    assert ("top_p" in probe) == ("top_p" in real)

    seen.clear()
    asyncio.run(service.test_target("openai", "gpt-4o"))
    assert seen[0]["max_tokens"] == 16_384  # the real budget, clamped - not a toy 64
