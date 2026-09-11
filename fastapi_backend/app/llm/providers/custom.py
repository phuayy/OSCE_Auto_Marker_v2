"""Providers built from an operator's definition rather than from a module.

A shipped provider is a class; a custom one is a :class:`CustomProviderSpec`
plus one of these two adapters. Nothing new is implemented here — the OpenAI
and Anthropic transports, with their error taxonomy, truncation guard and
retry semantics, are the ones every shipped provider already uses. All these
classes do is take the parts a vendor varies (endpoint, auth placement,
versions, extra headers/query/body) from the definition instead of from source.

That is the whole reason only two formats are offered. Supporting a third wire
format means a third transport to maintain, and a definition that could point
at "some other protocol" would be a promise the router cannot keep. Every
commercial platform worth adding today speaks one of these two.
"""
from __future__ import annotations

from typing import Any

from app.llm.base import ChatRequest, LLMProvider, ProviderCredentials, RequestMode
from app.llm.custom import API_FORMAT_ANTHROPIC, CustomProviderSpec
from app.llm.providers.anthropic_provider import AnthropicProvider
from app.llm.providers.openai_compatible import OpenAICompatibleProvider


class CustomOpenAIProvider(OpenAICompatibleProvider):
    """An OpenAI-compatible endpoint described by an operator.

    Auth placement is the one genuinely fiddly part. The SDK always sends
    ``Authorization: Bearer <api_key>``, which is what the great majority of
    platforms want. For the ones that do not — Azure's ``api-key``, Anthropic-
    style ``x-api-key``, an appliance that wants the key in the query string —
    the definition's header or parameter is added on top. The Bearer header
    then rides along unused; every such platform ignores an authorization
    header it did not ask for, and suppressing it would mean reaching into the
    SDK's private sentinels.
    """

    def __init__(self, credentials: ProviderCredentials, spec: CustomProviderSpec) -> None:
        super().__init__(credentials)
        self.spec = spec
        # Instance attribute, shadowing the class-level one the shipped
        # providers declare: every custom provider is the same class with a
        # different descriptor.
        self.descriptor = spec.to_descriptor()

    def client(self, timeout_seconds: float) -> Any:
        from openai import OpenAI  # imported lazily, as in the shared base

        key = self.credentials.api_key
        headers = {**self.spec.headers(key), **dict(self.credentials.extra_headers or {})}
        query = self.spec.query(key)
        return OpenAI(
            base_url=self.base_url,
            api_key=key,
            # A definition may cap its own requests — a local box on slow
            # hardware, or a gateway that closes idle connections — but never
            # raise a ceiling the caller set for this particular call.
            timeout=min(timeout_seconds, self.spec.request_timeout_seconds)
            if self.spec.request_timeout_seconds
            else timeout_seconds,
            max_retries=0,  # The router owns retries; double backoff hides failures.
            default_headers=headers or None,
            default_query=query or None,
        )

    def supports_json_mode(self, model: str) -> bool:
        """Taken from the definition: a gateway in front of a model with no JSON
        mode 400s on ``response_format``, and the operator is the one who knows.

        Getting it wrong is survivable either way — the router's mode ladder
        drops the switch and retries — but a definition that says so up front
        saves every call one doomed attempt.
        """
        return bool(self.spec.supports_json_mode)

    def extra_body(self, request: ChatRequest, mode: RequestMode) -> dict[str, Any]:
        # PLAIN is the rung reached *after* the vendor rejected a richer body,
        # so it sends nothing beyond the standard fields — same rule the shipped
        # providers follow.
        if mode is RequestMode.PLAIN:
            return {}
        return dict(self.spec.extra_body or {})


class CustomAnthropicProvider(AnthropicProvider):
    """An Anthropic Messages-format endpoint described by an operator.

    Useful for Claude through a corporate gateway, a Bedrock-compatible proxy,
    or any relay that speaks ``/messages``. The version header defaults to the
    one the shipped provider sends, so a definition that omits it still works.
    """

    def __init__(self, credentials: ProviderCredentials, spec: CustomProviderSpec) -> None:
        super().__init__(credentials)
        self.spec = spec
        self.descriptor = spec.to_descriptor()

    def _headers(self) -> dict[str, str]:
        key = self.credentials.api_key
        headers = super()._headers()
        headers.update(self.spec.headers(key))
        if self.spec.auth_scheme != "header":
            # No custom auth header was configured, so the Anthropic default
            # (x-api-key) written by the base class is what authenticates.
            headers.setdefault("x-api-key", key)
        return headers

    def _query(self) -> dict[str, str]:
        return self.spec.query(self.credentials.api_key)

    def _timeout(self, timeout_seconds: float) -> float:
        if self.spec.request_timeout_seconds:
            return min(timeout_seconds, self.spec.request_timeout_seconds)
        return timeout_seconds


def build_custom_provider(spec: CustomProviderSpec, credentials: ProviderCredentials) -> LLMProvider:
    """The adapter for one definition's wire format."""
    if spec.api_format == API_FORMAT_ANTHROPIC:
        return CustomAnthropicProvider(credentials, spec)
    return CustomOpenAIProvider(credentials, spec)
