"""Anthropic's Messages API, spoken natively over httpx.

Anthropic is the one shipped provider that is not OpenAI-compatible, and it is
implemented against the raw HTTP API rather than the ``anthropic`` SDK on
purpose: httpx is already a pinned dependency, so an operator who never selects
Claude installs nothing extra.

Three shape differences are handled here:

* the system prompt is a top-level field, not a message with ``role: system``;
* there is no ``response_format``, so JSON mode becomes an explicit instruction
  appended to the system prompt;
* ``max_tokens`` is required, and the stop reason for a truncated answer is
  ``max_tokens`` rather than ``length`` (the shared validator accepts both).
"""
from __future__ import annotations

from typing import Any

from app.llm.base import (
    ChatRequest,
    ChatResponse,
    LLMConfigError,
    LLMTransportError,
    ModelSpec,
    ProviderDescriptor,
    RequestMode,
    error_for_status,
)
from app.llm.base import LLMProvider

PROVIDER_ID = "anthropic"

API_VERSION = "2023-06-01"

# Appended to the system prompt in JSON mode. Anthropic has no server-side JSON
# switch, so the instruction is the only lever — and it must not contradict the
# caller's own prompt, which already asks for a JSON object.
JSON_INSTRUCTION = (
    "Respond with a single valid JSON object and nothing else. "
    "Do not wrap it in markdown fences and do not add commentary before or after it."
)

DESCRIPTOR = ProviderDescriptor(
    id=PROVIDER_ID,
    label="Anthropic",
    vendor="Anthropic",
    description="Claude models via the native Messages API. Strong at long rubric-following and evidence citation.",
    api_key_env=("ANTHROPIC_API_KEY",),
    base_url_env="ANTHROPIC_BASE_URL",
    default_base_url="https://api.anthropic.com/v1",
    requirements="Create a key at console.anthropic.com and set ANTHROPIC_API_KEY.",
    models=(
        ModelSpec(
            id="claude-sonnet-5",
            label="Claude Sonnet 5",
            context_window=200_000,
            max_output_tokens=64_000,
            supports_json_mode=False,
            supports_reasoning_control=True,
        ),
        ModelSpec(
            id="claude-opus-5",
            label="Claude Opus 5",
            context_window=200_000,
            max_output_tokens=64_000,
            supports_json_mode=False,
            supports_reasoning_control=True,
        ),
        ModelSpec(
            id="claude-haiku-4-5-20251001",
            label="Claude Haiku 4.5",
            context_window=200_000,
            max_output_tokens=64_000,
            supports_json_mode=False,
            notes="Fast and inexpensive; a good fallback for transcript preprocessing.",
        ),
    ),
)


def _split_system(messages: list[dict[str, str]]) -> tuple[str, list[dict[str, Any]]]:
    """Lift ``system`` turns into Anthropic's top-level field.

    Multiple system turns are joined rather than dropped — the scoring prompts
    send one, but a caller that sends two must not silently lose one.
    """
    system_parts: list[str] = []
    conversation: list[dict[str, Any]] = []
    for message in messages:
        role = str(message.get("role") or "user")
        content = str(message.get("content") or "")
        if role == "system":
            if content:
                system_parts.append(content)
            continue
        conversation.append({"role": "assistant" if role == "assistant" else "user", "content": content})
    return "\n\n".join(system_parts), conversation


class AnthropicProvider(LLMProvider):
    descriptor = DESCRIPTOR

    def complete(self, request: ChatRequest, model: str, mode: RequestMode) -> ChatResponse:
        self.ensure_ready()
        resolved_model = self.resolve_model(model)
        system_prompt, conversation = _split_system(request.messages)
        if not conversation:
            raise LLMConfigError(
                "Anthropic requires at least one user message.",
                provider_id=PROVIDER_ID,
                model=resolved_model,
            )
        if request.json_mode and mode in {RequestMode.STRUCTURED, RequestMode.JSON_ONLY}:
            system_prompt = f"{system_prompt}\n\n{JSON_INSTRUCTION}".strip()

        payload: dict[str, Any] = {
            "model": resolved_model,
            "messages": conversation,
            "max_tokens": request.max_tokens,
            "temperature": request.temperature,
        }
        if system_prompt:
            payload["system"] = system_prompt
        # top_p and temperature together are rejected by some Claude versions;
        # temperature alone is the setting that matters for deterministic marking.
        if mode is RequestMode.PLAIN:
            payload.pop("temperature", None)

        body, status_code, headers = self._post(payload, request.timeout_seconds, resolved_model)
        if status_code != 200:
            raise error_for_status(
                status_code,
                f"Anthropic returned {status_code}: {self._error_message(body)}",
                provider_id=PROVIDER_ID,
                model=resolved_model,
                retry_after_seconds=self._retry_after(headers),
            )

        content = "".join(
            str(block.get("text") or "")
            for block in (body.get("content") or [])
            if isinstance(block, dict) and block.get("type") == "text"
        )
        finish_reason = str(body.get("stop_reason") or "")
        text = self.validate_content(content, finish_reason, request, resolved_model)
        return ChatResponse(
            content=text,
            provider_id=PROVIDER_ID,
            model=resolved_model,
            mode=mode.value,
            finish_reason=finish_reason,
            usage=dict(body.get("usage") or {}),
        )

    def _post(
        self,
        payload: dict[str, Any],
        timeout_seconds: float,
        model: str,
    ) -> tuple[dict[str, Any], int, Any]:
        try:
            import httpx
        except ImportError as error:  # pragma: no cover - dependency is pinned
            raise LLMConfigError(
                "The 'httpx' package is required for the Anthropic provider.",
                provider_id=PROVIDER_ID,
            ) from error

        headers = {
            "x-api-key": self.credentials.api_key,
            "anthropic-version": API_VERSION,
            "content-type": "application/json",
            **dict(self.credentials.extra_headers or {}),
        }
        url = f"{self.base_url.rstrip('/')}/messages"
        try:
            with httpx.Client(timeout=timeout_seconds) as client:
                response = client.post(url, headers=headers, json=payload)
        except Exception as error:
            # Connection-level failures never reached the model, so they are
            # always worth another attempt.
            raise LLMTransportError(
                f"{type(error).__name__}: {error}",
                provider_id=PROVIDER_ID,
                model=model,
            ) from error
        try:
            body = response.json()
        except Exception:
            body = {"error": {"message": response.text[:2000]}}
        if not isinstance(body, dict):
            body = {"error": {"message": str(body)[:2000]}}
        return body, response.status_code, response.headers

    @staticmethod
    def _error_message(body: dict[str, Any]) -> str:
        error = body.get("error")
        if isinstance(error, dict):
            return str(error.get("message") or error)
        return str(error or body)[:2000]

    @staticmethod
    def _retry_after(headers: Any) -> float | None:
        try:
            raw = headers.get("retry-after")
        except Exception:
            return None
        try:
            seconds = float(str(raw).strip())
        except (TypeError, ValueError):
            return None
        return seconds if 0 < seconds <= 300 else None
