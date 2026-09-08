"""Provider implementation for every vendor that speaks the OpenAI wire format.

Five of the six providers this build ships — OpenAI, DeepSeek, Google Gemini
(through its OpenAI-compatible endpoint), OpenRouter and NVIDIA — accept the
same ``/chat/completions`` request. Sharing one implementation means the retry
semantics, the truncation guard and the error taxonomy are identical across
them, and a new OpenAI-compatible vendor is a descriptor plus two lines.

Where vendors genuinely differ is the *optional* body: NVIDIA's Nemotron
deployments take ``chat_template_kwargs``, OpenRouter takes a ``reasoning``
object, plain OpenAI takes neither and 400s on both. That difference is isolated
in :meth:`OpenAICompatibleProvider.extra_body`, which subclasses override.
"""
from __future__ import annotations

from typing import Any

from app.llm.base import (
    ChatRequest,
    ChatResponse,
    LLMConfigError,
    LLMError,
    LLMProvider,
    LLMResponseError,
    ProviderDescriptor,
    RequestMode,
    error_for_status,
)


def _retry_after_seconds(headers: Any) -> float | None:
    """Read a provider's own backoff advice, when it sends one."""
    try:
        raw = headers.get("retry-after") if headers is not None else None
    except Exception:
        return None
    if raw is None:
        return None
    try:
        seconds = float(str(raw).strip())
    except (TypeError, ValueError):
        return None
    # Ignore nonsense so a malformed header cannot stall a run for an hour.
    return seconds if 0 < seconds <= 300 else None


class OpenAICompatibleProvider(LLMProvider):
    """Chat completions over the OpenAI SDK against ``descriptor.base_url``."""

    descriptor: ProviderDescriptor

    def client(self, timeout_seconds: float) -> Any:
        """Build a client per call.

        The SDK object is cheap, and per-call construction keeps the timeout —
        which differs between a 6-minute scoring call and a 20-second
        connection test — attached to the client rather than smuggled through
        request kwargs that some gateways drop.
        """
        try:
            from openai import OpenAI
        except ImportError as error:  # pragma: no cover - dependency is pinned
            raise LLMConfigError(
                "The 'openai' package is required for OpenAI-compatible providers. "
                "Install it with: uv sync",
                provider_id=self.descriptor.id,
            ) from error
        return OpenAI(
            base_url=self.base_url,
            api_key=self.credentials.api_key,
            timeout=timeout_seconds,
            max_retries=0,  # The router owns retries; double backoff hides failures.
            default_headers=dict(self.credentials.extra_headers or {}),
        )

    # --- request shaping ---------------------------------------------------

    def extra_body(self, request: ChatRequest, mode: RequestMode) -> dict[str, Any]:
        """Vendor-specific switches. Empty for a standards-compliant endpoint."""
        return {}

    def supports_json_mode(self, model: str) -> bool:
        spec = self.descriptor.model(model)
        return spec.supports_json_mode if spec is not None else True

    def build_payload(self, request: ChatRequest, model: str, mode: RequestMode) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": model,
            "messages": request.messages,
            "temperature": request.temperature,
            "top_p": request.top_p,
            "max_tokens": request.max_tokens,
        }
        wants_json = request.json_mode and mode in {RequestMode.STRUCTURED, RequestMode.JSON_ONLY}
        if wants_json and self.supports_json_mode(model):
            payload["response_format"] = {"type": "json_object"}
        extra_body = self.extra_body(request, mode)
        if extra_body:
            payload["extra_body"] = extra_body
        return payload

    # --- execution ---------------------------------------------------------

    def complete(self, request: ChatRequest, model: str, mode: RequestMode) -> ChatResponse:
        self.ensure_ready()
        resolved_model = self.resolve_model(model)
        client = self.client(request.timeout_seconds)
        payload = self.build_payload(request, resolved_model, mode)

        try:
            response = client.chat.completions.create(**payload)
        except Exception as error:
            raise self.translate_error(error, resolved_model) from error

        content, finish_reason = self.extract_content(response, resolved_model)
        text = self.validate_content(content, finish_reason, request, resolved_model)
        return ChatResponse(
            content=text,
            provider_id=self.descriptor.id,
            model=resolved_model,
            mode=mode.value,
            finish_reason=finish_reason,
            usage=self.extract_usage(response),
        )

    def extract_content(self, response: Any, model: str) -> tuple[str, str]:
        """Pull text out of a completion, refusing 200-with-an-error bodies.

        Gateways such as OpenRouter relay an upstream failure as a 200 whose
        body carries an ``error`` object and no choices. Left unchecked that
        surfaced as an opaque ``IndexError`` several frames away.
        """
        embedded_error = getattr(response, "error", None)
        if embedded_error is None:
            try:
                embedded_error = response.model_dump().get("error")
            except Exception:
                embedded_error = None
        if embedded_error:
            if isinstance(embedded_error, dict):
                message = str(embedded_error.get("message") or "Provider returned error").strip()
                code = embedded_error.get("code")
            else:
                message = str(embedded_error).strip() or "Provider returned error"
                code = None
            status_code = code if isinstance(code, int) else None
            raise error_for_status(
                status_code,
                f"Provider returned error (code={code if code is not None else 'unknown'}): {message}",
                provider_id=self.descriptor.id,
                model=model,
            )

        choices = getattr(response, "choices", None)
        if not choices:
            raise LLMResponseError(
                "Model response did not include any choices.",
                provider_id=self.descriptor.id,
                model=model,
            )
        first = choices[0]
        message_obj = getattr(first, "message", None)
        if message_obj is None:
            raise LLMResponseError(
                "Model response did not include a message payload.",
                provider_id=self.descriptor.id,
                model=model,
            )
        finish_reason = str(getattr(first, "finish_reason", "") or "")
        return str(getattr(message_obj, "content", "") or ""), finish_reason

    @staticmethod
    def extract_usage(response: Any) -> dict[str, Any]:
        usage = getattr(response, "usage", None)
        if usage is None:
            return {}
        for attribute in ("model_dump", "dict"):
            dump = getattr(usage, attribute, None)
            if callable(dump):
                try:
                    return dict(dump())
                except Exception:
                    break
        return {
            key: getattr(usage, key)
            for key in ("prompt_tokens", "completion_tokens", "total_tokens")
            if getattr(usage, key, None) is not None
        }

    def translate_error(self, error: Exception, model: str) -> LLMError:
        """Turn an SDK exception into one the router can act on."""
        if isinstance(error, LLMError):
            return error
        status_code = getattr(error, "status_code", None)
        if not isinstance(status_code, int):
            response = getattr(error, "response", None)
            candidate = getattr(response, "status_code", None)
            status_code = candidate if isinstance(candidate, int) else None
        headers = getattr(getattr(error, "response", None), "headers", None)
        return error_for_status(
            status_code,
            f"{type(error).__name__}: {error}",
            provider_id=self.descriptor.id,
            model=model,
            retry_after_seconds=_retry_after_seconds(headers),
        )
