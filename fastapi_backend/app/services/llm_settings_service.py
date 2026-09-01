"""Resolves the operator's model choice into something a subprocess can run.

The API side of the LLM router. It answers three questions:

* **What will run?** ``routing()`` reads the stored primary/fallback selection
  live from the database on every call — the same live-read contract the
  transcription engine and the preprocess toggle use — so a change in the
  settings screen applies to the next scoring run in every process, including
  clip children and the Hatchet worker, with no restart.
* **What could run?** ``describe()`` feeds the settings screen: every provider
  this build ships, its model shortlist, and whether a key is configured here.
* **How does a subprocess find out?** ``subprocess_env()`` serialises the
  resolved routing plus only the credentials that routing actually needs.

A stored selection is never trusted blindly. A provider that this build no
longer ships, or one whose key was removed from the deployment, is dropped from
the routing with a warning rather than failing the run — the fallback exists
precisely so a broken primary is survivable.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Mapping
from typing import Any, Union

from app.llm import credentials as credential_resolver
from app.llm import registry
from app.llm.base import ChatRequest, LLMError, ProviderCredentials, ReasoningPolicy
from app.llm.router import LLMRouter
from app.llm.routing import LLMTarget, RoutingConfig
from app.llm.runtime import retry_policy_from_env
from app.repositories.app_settings_repository import AppSettingsRepository

logger = logging.getLogger(__name__)

# Either a fixed map of provider id -> API key, or a callable returning one.
# The callable form exists so secrets loaded after container construction
# (AuthService reads them at startup) are still picked up.
KeyOverrideSource = Union[Mapping[str, str], Callable[[], Mapping[str, str]]]

# Kept short and cheap: the settings screen's "Test" button must answer while
# the operator is still looking at it, and a scoring-length 6-minute timeout
# would make a dead endpoint indistinguishable from a hung browser tab.
TEST_TIMEOUT_SECONDS = 30.0
TEST_MAX_TOKENS = 64


class LLMSettingsService:
    def __init__(
        self,
        app_settings: AppSettingsRepository,
        *,
        key_overrides: KeyOverrideSource | None = None,
    ) -> None:
        self.app_settings = app_settings
        # Keys the API holds in memory but that are not in os.environ — the
        # NVIDIA key loaded from the platform secrets file is the existing case.
        # Resolved through a callable rather than captured at construction: the
        # container is built before AuthService loads its secrets, so a snapshot
        # taken here would always be empty.
        self._key_overrides = key_overrides

    # --- credentials -------------------------------------------------------

    def key_overrides(self) -> dict[str, str]:
        if self._key_overrides is None:
            return {}
        if callable(self._key_overrides):
            try:
                return {key: value for key, value in (self._key_overrides() or {}).items() if value}
            except Exception:  # pragma: no cover - defensive
                logger.exception("Failed to resolve in-memory LLM API keys; using the environment only.")
                return {}
        return {key: value for key, value in dict(self._key_overrides).items() if value}

    def credentials(self) -> dict[str, ProviderCredentials]:
        return credential_resolver.resolve_all(overrides=self.key_overrides())

    def providers(self) -> dict[str, Any]:
        return registry.build_all(self.credentials())

    def configured_provider_ids(self) -> set[str]:
        return {
            provider_id
            for provider_id, provider in self.providers().items()
            if provider.availability().available
        }

    # --- selection ---------------------------------------------------------

    def default_routing(self) -> RoutingConfig:
        """What runs when nothing has been chosen.

        The default provider with no explicit model, which resolves to that
        provider's first shortlisted model — the checkpoint this project's
        prompts were tuned against.
        """
        return RoutingConfig(
            primary=LLMTarget(registry.DEFAULT_PROVIDER_ID, ""),
            retry=retry_policy_from_env({}),
        )

    async def stored_routing(self) -> RoutingConfig:
        """The operator's raw selection, before any usability filtering."""
        try:
            primary_raw, fallbacks_raw = await self.app_settings.llm_routing_selection()
        except Exception:
            # A settings lookup must never be the thing that fails a run.
            logger.exception("Failed to read the LLM routing settings; using the deployment default.")
            return self.default_routing()
        config = RoutingConfig.from_raw(
            {"primary": primary_raw, "fallbacks": fallbacks_raw},
            default_provider_id=registry.DEFAULT_PROVIDER_ID,
        )
        return config.with_retry(retry_policy_from_env())

    async def routing(self) -> RoutingConfig:
        """The selection, filtered down to targets that can actually run here.

        Two filters, both of which would otherwise cost a run:

        * a provider this build no longer ships (the stored row outlived a
          release);
        * a provider with no API key on this machine (a fallback configured on
          a laptop that the GPU box was never given a key for).

        If filtering empties the list the unfiltered selection is returned
        instead, so the failure surfaces as a clear provider error from the
        router rather than as "no target configured".
        """
        stored = await self.stored_routing()
        usable = self.configured_provider_ids()
        primary = stored.primary if stored.primary.provider_id in usable else None
        fallbacks = tuple(target for target in stored.fallbacks if target.provider_id in usable)

        if primary is None and fallbacks:
            logger.warning(
                "Primary LLM provider '%s' has no API key configured here; promoting the first fallback.",
                stored.primary.provider_id,
            )
            return RoutingConfig(primary=fallbacks[0], fallbacks=fallbacks[1:], retry=stored.retry)
        if primary is None:
            logger.warning(
                "No configured LLM provider has an API key; keeping the stored selection so the "
                "failure names the missing credential."
            )
            return stored
        return RoutingConfig(primary=primary, fallbacks=fallbacks, retry=stored.retry)

    # --- subprocess handoff ------------------------------------------------

    async def subprocess_env(self) -> dict[str, str]:
        """Routing plus the credentials that routing needs, as env variables."""
        config = await self.routing()
        env = dict(config.to_env())
        provider_ids = [target.provider_id for target in config.targets()]
        env.update(credential_resolver.credential_env_for(provider_ids, overrides=self.key_overrides()))
        return env

    # --- settings screen ---------------------------------------------------

    async def describe(self) -> dict[str, Any]:
        """Every provider, its models, its availability, and the current choice."""
        stored = await self.stored_routing()
        effective = await self.routing()
        providers: list[dict[str, Any]] = []
        for provider_id, provider in self.providers().items():
            payload = provider.descriptor.to_public()
            payload["availability"] = provider.availability().to_public()
            providers.append(payload)
        return {
            "providers": providers,
            "defaultProviderId": registry.DEFAULT_PROVIDER_ID,
            "selected": stored.to_public(),
            # What would run right now, after unusable targets are dropped. The
            # screen shows this when it differs from the selection, so an
            # operator learns their fallback is inert before a run proves it.
            "effective": effective.to_public(),
            "retry": effective.retry.to_public(),
        }

    async def test_target(self, provider_id: str, model: str) -> dict[str, Any]:
        """One real round trip to a provider, for the settings screen's Test button.

        Deliberately runs a *single* target with no fallback: the operator is
        asking about this provider, and silently succeeding via a different one
        would be the opposite of useful. Retries still apply, so a transient
        blip does not report a working provider as broken.
        """
        resolved_id = str(provider_id or "").strip()
        if resolved_id not in registry.PROVIDER_FACTORIES:
            known = ", ".join(registry.provider_ids())
            return {
                "ok": False,
                "providerId": resolved_id,
                "model": model,
                "error": f"Unknown provider '{resolved_id}'. Available: {known}.",
            }

        config = RoutingConfig(
            primary=LLMTarget(resolved_id, str(model or "").strip()),
            retry=retry_policy_from_env(),
        )
        router = LLMRouter(self.providers(), config)
        request = ChatRequest(
            messages=[
                {"role": "system", "content": "You are a connectivity probe. Answer with JSON only."},
                {"role": "user", "content": 'Reply with exactly this JSON object: {"ok": true}'},
            ],
            temperature=0.0,
            max_tokens=TEST_MAX_TOKENS,
            timeout_seconds=TEST_TIMEOUT_SECONDS,
            json_mode=True,
            reasoning=ReasoningPolicy(enabled=False),
            min_content_chars=1,
            label="settings-connection-test",
        )

        started = time.monotonic()
        try:
            response = await asyncio.to_thread(router.complete, request)
        except LLMError as error:
            return {
                "ok": False,
                "providerId": resolved_id,
                "model": config.primary.model,
                "error": error.message,
                "elapsedSeconds": round(time.monotonic() - started, 3),
                "attempts": [record.to_public() for record in getattr(error, "attempts", ())],
            }
        except Exception as error:  # pragma: no cover - defensive
            logger.exception("LLM connection test raised an unexpected error.")
            return {
                "ok": False,
                "providerId": resolved_id,
                "model": config.primary.model,
                "error": f"{type(error).__name__}: {error}",
                "elapsedSeconds": round(time.monotonic() - started, 3),
            }

        return {
            "ok": True,
            "providerId": response.provider_id,
            "model": response.model,
            "mode": response.mode,
            "elapsedSeconds": round(time.monotonic() - started, 3),
            "attempts": [record.to_public() for record in response.attempts],
            "usage": dict(response.usage or {}),
        }
