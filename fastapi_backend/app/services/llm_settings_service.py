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
from dataclasses import dataclass, field
from typing import Any, Union

from app.core.secret_box import redact_secrets
from app.llm import credentials as credential_resolver
from app.llm import registry
from app.llm.base import ChatRequest, LLMError, ProviderCredentials, ReasoningPolicy
from app.llm.catalog import ProviderCatalog, builtin_catalog
from app.llm.custom import redact_connection_secrets
from app.llm.panel import MarkingMode, PanelConfig, TieBreak, marker_key, parse_marking_mode
from app.llm.router import LLMRouter
from app.llm.routing import LLMTarget, RoutingConfig
from app.llm.runtime import retry_policy_from_env
from app.pipeline.marking.base import MarkerAssignment, MarkingPlan
from app.services.custom_provider_service import CustomProviderService
from app.services.preferences_service import PreferencesService
from app.services.provider_credential_service import CredentialSnapshot, ProviderCredentialService

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ResolvedProviders:
    """Everything one credential read yields, computed once.

    Threaded through a request rather than re-derived, for two reasons. The
    cheap one is cost: four independent reads per ``describe()`` was four token
    checks and, on a cold cache, four decrypt passes over the same rows. The
    load-bearing one is consistency — a rotation landing mid-request used to be
    able to produce a routing decision made against one snapshot and a forwarded
    key taken from another.
    """

    stored_keys: Mapping[str, str]
    statuses: Mapping[str, dict[str, Any]]
    overrides: Mapping[str, str]
    credentials: Mapping[str, ProviderCredentials]
    providers: Mapping[str, Any]
    # provider id -> "app" | "environment" | "none"
    sources: Mapping[str, str]
    # Which providers existed when this was resolved. Carried on the value
    # rather than re-read, because a provider added mid-request must not be able
    # to make the credential map and the routing decision disagree about what
    # the set of targets even is.
    catalog: ProviderCatalog = field(default_factory=builtin_catalog)

    def configured_ids(self) -> set[str]:
        return {
            provider_id
            for provider_id, provider in self.providers.items()
            if provider.availability().available
        }

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
        preferences: PreferencesService,
        *,
        key_overrides: KeyOverrideSource | None = None,
        credential_store: ProviderCredentialService | None = None,
        custom_providers: CustomProviderService | None = None,
    ) -> None:
        self.preferences = preferences
        # Keys the API holds in memory but that are not in os.environ — the
        # NVIDIA key loaded from the platform secrets file is the existing case.
        # Resolved through a callable rather than captured at construction: the
        # container is built before AuthService loads its secrets, so a snapshot
        # taken here would always be empty.
        self._key_overrides = key_overrides
        # Keys the operator saved in the settings screen. Optional so a service
        # built for a test, or a deployment that keeps every key in the
        # environment, behaves exactly as it did before this existed.
        self.credential_store = credential_store
        # Operator-defined providers. Optional for the same reason: a service
        # built for a test, or a deployment that only ever uses the vendors this
        # build ships, sees exactly the behaviour that predates this.
        self.custom_providers = custom_providers

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

    async def credential_snapshot(self) -> CredentialSnapshot:
        """One read of the stored credentials — cached, and shared by everything
        derived from it.

        The store answers from memory while its snapshot is current, and evicts
        on the database's own change announcement, so a key rotated in any
        process applies to the next run everywhere without either a restart or a
        per-run query. See ``provider_credential_service`` for the freshness
        contract.
        """
        if self.credential_store is None:
            return CredentialSnapshot()
        try:
            return await self.credential_store.snapshot()
        except Exception:
            # An unreadable credential table must not be the thing that fails a
            # run: the environment may still carry a usable key.
            logger.exception("Failed to read stored provider API keys; using the environment only.")
            return CredentialSnapshot()

    async def catalog(self) -> ProviderCatalog:
        """Every provider this deployment can route to, shipped and custom.

        Read through the custom-provider store's cache, so the common path costs
        no query and an edit anywhere still reaches this process - the same
        contract the model selection and the API keys are held to.
        """
        if self.custom_providers is None:
            return builtin_catalog()
        return await self.custom_providers.catalog()

    async def resolve(self, *, extra_keys: Mapping[str, str] | None = None) -> ResolvedProviders:
        """Everything derived from one credential read, computed together.

        This is the single entry point every public method funnels through.
        Previously each of ``credentials``/``providers``/``credential_sources``/
        ``credential_statuses`` reached for the store independently, so one
        ``describe()`` did four reads and four decrypt passes of the same rows.
        They are all projections of one snapshot, so they are built from one.
        """
        catalog = await self.catalog()
        snapshot = await self.credential_snapshot()
        stored = dict(snapshot.api_keys)

        # Order is the policy: a key saved in the settings screen beats one
        # loaded from the platform secrets file, which beats the environment
        # (applied last, inside ``resolve_credentials``). Rotation from the UI
        # has to win, or it is not rotation. ``extra_keys`` is the un-saved key a
        # connection test was handed, which outranks everything for that one call.
        environment = dict(self.key_overrides())
        overrides = {**environment, **stored}
        for provider_id, value in (extra_keys or {}).items():
            if str(value or "").strip():
                overrides[provider_id] = str(value).strip()

        credentials = credential_resolver.resolve_all(overrides=overrides, catalog=catalog)
        # Resolved a second time without the stored keys, purely to tell "saved
        # here" from "from the environment". Both calls are pure os.environ
        # reads — no database, no decryption — so this costs nothing.
        environment_only = credential_resolver.resolve_all(overrides=environment, catalog=catalog)

        sources: dict[str, str] = {}
        for provider_id in catalog.provider_ids():
            if str(stored.get(provider_id) or "").strip():
                sources[provider_id] = "app"
            elif environment_only.get(provider_id, ProviderCredentials()).configured:
                sources[provider_id] = "environment"
            else:
                sources[provider_id] = "none"

        return ResolvedProviders(
            stored_keys=stored,
            statuses=snapshot.statuses,
            overrides=overrides,
            credentials=credentials,
            providers=catalog.build_all(credentials),
            sources=sources,
            catalog=catalog,
        )

    # Thin wrappers over ``resolve``. Each is one credential read; a caller that
    # wants two of them should call ``resolve`` once instead.

    async def stored_keys(self) -> dict[str, str]:
        return dict((await self.credential_snapshot()).api_keys)

    async def merged_overrides(self, extra: Mapping[str, str] | None = None) -> dict[str, str]:
        return dict((await self.resolve(extra_keys=extra)).overrides)

    async def credentials(
        self, *, extra_keys: Mapping[str, str] | None = None
    ) -> dict[str, ProviderCredentials]:
        return dict((await self.resolve(extra_keys=extra_keys)).credentials)

    async def providers(self, *, extra_keys: Mapping[str, str] | None = None) -> dict[str, Any]:
        return dict((await self.resolve(extra_keys=extra_keys)).providers)

    async def configured_provider_ids(self) -> set[str]:
        return (await self.resolve()).configured_ids()

    async def credential_sources(self) -> dict[str, str]:
        """Where each provider's key is coming from right now: "app", the
        settings screen; "environment", this deployment's env or secrets file;
        "none", nowhere. Rendered in the settings screen so an operator can see
        that a saved key is shadowing a stale ``.env`` entry rather than
        wondering why the file they edited had no effect."""
        return dict((await self.resolve()).sources)

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

    async def stored_routing(self, user_id: str | None = None) -> RoutingConfig:
        """The account's raw selection (deployment default when ``user_id`` is
        None or has none of its own), before any usability filtering."""
        try:
            primary_raw, fallbacks_raw = await self.preferences.llm_routing_selection(user_id)
        except Exception:
            # A settings lookup must never be the thing that fails a run.
            logger.exception("Failed to read the LLM routing settings; using the deployment default.")
            return self.default_routing()
        config = RoutingConfig.from_raw(
            {"primary": primary_raw, "fallbacks": fallbacks_raw},
            default_provider_id=registry.DEFAULT_PROVIDER_ID,
        )
        return config.with_retry(retry_policy_from_env())

    async def routing(self, user_id: str | None = None) -> RoutingConfig:
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
        return self.filter_routing(await self.stored_routing(user_id), await self.resolve())

    def filter_routing(self, stored: RoutingConfig, resolved: ResolvedProviders) -> RoutingConfig:
        """``routing()``'s filtering, against an already-resolved provider set.

        Split out so a caller that needs both the raw selection and the filtered
        one — ``describe()`` and ``subprocess_env()`` both do — pays for one
        credential read and one settings read rather than two of each.
        """
        usable = resolved.configured_ids()
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

    @staticmethod
    def subprocess_env_for(config: RoutingConfig, resolved: ResolvedProviders) -> dict[str, str]:
        """Routing plus the credentials that routing needs, as env variables.

        Parameterised on the routing so a panel can hand each marker, and the
        adjudicator, an environment naming only *its* target and carrying only
        *its* key — all cut from the same ``resolved`` snapshot, which is what
        stops two markers holding keys from different worlds.
        """
        env = dict(config.to_env())
        # The definitions travel with the routing, not separately: a subprocess
        # told to call "our-gateway" has to be able to find out what that means,
        # and reading it from the same snapshot the routing was filtered against
        # is what stops the two disagreeing.
        env.update(resolved.catalog.to_env())
        provider_ids = [target.provider_id for target in config.targets()]
        env.update(
            credential_resolver.credential_env_for(
                provider_ids, overrides=resolved.overrides, catalog=resolved.catalog
            )
        )
        return env

    async def subprocess_env(self, user_id: str | None = None) -> dict[str, str]:
        """The single-mode handoff: the filtered routing and its credentials.

        One credential read for the whole handoff. The resolved set decides both
        which targets survive filtering and which keys are forwarded, so reading
        it twice could — on a rotation landing between the two — forward a key
        for a target chosen against the other snapshot.
        """
        resolved = await self.resolve()
        return self.subprocess_env_for(self.filter_routing(await self.stored_routing(user_id), resolved), resolved)

    # --- marking mode ------------------------------------------------------

    async def stored_marking(self, user_id: str | None = None) -> tuple[MarkingMode, PanelConfig]:
        """The account's raw marking selection (deployment default when
        ``user_id`` is None or has none of its own), before any usability
        filtering."""
        try:
            mode_raw, panel_raw = await self.preferences.marking_selection(user_id)
        except Exception:
            # A settings lookup must never be the thing that fails a run.
            logger.exception("Failed to read the marking-mode settings; using single-model marking.")
            return MarkingMode.SINGLE, PanelConfig()
        return parse_marking_mode(mode_raw), PanelConfig.from_raw(panel_raw)

    def build_marking_plan(
        self,
        selected_mode: MarkingMode,
        panel: PanelConfig,
        routing: RoutingConfig,
        resolved: ResolvedProviders,
    ) -> MarkingPlan:
        """``marking_plan()``'s decision, against already-resolved inputs.

        Pure and synchronous so the effective-vs-selected cases are testable
        without a database. The rules mirror ``filter_routing``: a target with
        no usable provider here is dropped with a reason rather than failing the
        run, and if that leaves fewer markers than a panel needs, the run is
        single mode against the ordinary routing — with the reasons carried on
        the plan so the settings screen can say so before a run proves it.
        """
        single_env = self.subprocess_env_for(routing, resolved)
        if selected_mode is not MarkingMode.PANEL:
            return MarkingPlan(single=routing, single_env=single_env)

        validation = panel.validate()
        reasons: list[str] = list(validation.warnings)
        if not validation.ok:
            reasons.extend(validation.errors)
            reasons.append("Panel marking is not configured correctly; this run marks with a single model.")
            return MarkingPlan(
                selected_mode=MarkingMode.PANEL, single=routing, single_env=single_env,
                tie_break=panel.tie_break, warnings=tuple(reasons),
            )

        usable = resolved.configured_ids()
        markers: list[MarkerAssignment] = []
        for target in panel.markers:
            if target.provider_id not in usable:
                reasons.append(f"Marker {target.key} has no API key configured here and was dropped.")
                continue
            markers.append(
                MarkerAssignment(
                    target=target,
                    key=marker_key(target),
                    llm_env=self.subprocess_env_for(RoutingConfig(primary=target, retry=routing.retry), resolved),
                )
            )
        if len(markers) < 2:
            reasons.append("Fewer than two markers can run here; this run marks with a single model.")
            return MarkingPlan(
                selected_mode=MarkingMode.PANEL, single=routing, single_env=single_env,
                tie_break=panel.tie_break, warnings=tuple(reasons),
            )

        adjudicator: MarkerAssignment | None = None
        if panel.adjudicator is not None and panel.adjudicator.provider_id in usable:
            adjudicator = MarkerAssignment(
                target=panel.adjudicator,
                key=marker_key(panel.adjudicator),
                llm_env=self.subprocess_env_for(
                    RoutingConfig(primary=panel.adjudicator, retry=routing.retry), resolved
                ),
            )
        elif panel.adjudicator is not None:
            reasons.append(
                f"Adjudicator {panel.adjudicator.key} has no API key configured here; "
                f"disputed criteria fall to the '{panel.tie_break}' tie-break."
            )

        return MarkingPlan(
            mode=MarkingMode.PANEL,
            selected_mode=MarkingMode.PANEL,
            single=routing,
            single_env=single_env,
            markers=tuple(markers),
            adjudicator=adjudicator,
            tie_break=panel.tie_break,
            warnings=tuple(reasons),
        )

    async def marking_plan(self, user_id: str | None = None) -> MarkingPlan:
        """What this run should do to mark content, resolved once for the
        session owner (``user_id``; the deployment default when None).

        Read live like ``routing()`` — a mode switched in the settings screen
        applies to the next assessment in every process — and built from one
        ``resolve()`` so every marker's environment comes from the same
        credential snapshot.
        """
        resolved = await self.resolve()
        routing = self.filter_routing(await self.stored_routing(user_id), resolved)
        selected_mode, panel = await self.stored_marking(user_id)
        return self.build_marking_plan(selected_mode, panel, routing, resolved)

    # --- settings screen ---------------------------------------------------

    async def describe(self, user_id: str | None = None, *, include_provider_secrets: bool = True) -> dict[str, Any]:
        """Every provider, its models, its availability, its credential state,
        and the current choice — the requesting account's own, falling back to
        the deployment default for whatever it has not personalised.

        The credential block is metadata only - where the key came from, its last
        four characters, when it was set, how the last test went. The key itself
        is never part of this response, and there is no endpoint that returns
        one: the settings screen is a place to *replace* a credential, not to
        read one back.

        ``include_provider_secrets`` gates a *different* thing: a custom
        provider's connection can carry a second credential of its own — a
        gateway token in ``extraHeaders``, say (see
        ``app.llm.custom.redact_connection_secrets``) — which the settings
        screen's edit form legitimately needs back, but which this method is
        also called to answer for every signed-in marker just so they can pick
        a provider from a dropdown. Only the caller that has already checked
        the requester is an operator should pass ``True``.
        """
        resolved = await self.resolve()
        stored = await self.stored_routing(user_id)
        effective = self.filter_routing(stored, resolved)
        selected_mode, panel = await self.stored_marking(user_id)
        plan = self.build_marking_plan(selected_mode, panel, effective, resolved)
        sources = resolved.sources
        statuses = resolved.statuses
        providers: list[dict[str, Any]] = []
        for provider_id, provider in resolved.providers.items():
            payload = provider.descriptor.to_public()
            if not include_provider_secrets and payload.get("isCustom"):
                payload["connection"] = redact_connection_secrets(payload["connection"])
            payload["availability"] = provider.availability().to_public()
            status = dict(statuses.get(provider_id) or {})
            payload["credential"] = {
                "source": sources.get(provider_id, "none"),
                "configured": sources.get(provider_id, "none") != "none",
                "maskedKey": status.get("maskedKey", ""),
                "updatedAt": status.get("updatedAt", ""),
                "updatedBy": status.get("updatedBy", ""),
                "lastTestedAt": status.get("lastTestedAt", ""),
                "lastTestOk": status.get("lastTestOk"),
                "lastTestError": status.get("lastTestError", ""),
                # False means the row exists but this deployment's master key
                # cannot open it - the operator has to re-enter, and saying so
                # is better than reporting the provider as simply unconfigured.
                "readable": bool(status.get("readable", True)) if status else True,
            }
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
            "credentialStorage": self.credential_storage_status(),
            # How content is marked. ``selected`` is the stored panel as the
            # operator entered it; ``effective`` is what a run would do right
            # now, after credential filtering, with the reasons it differs.
            "marking": {
                "mode": str(selected_mode),
                "modes": [str(mode) for mode in MarkingMode],
                "tieBreaks": [str(policy) for policy in TieBreak],
                "selected": panel.to_public(),
                "effective": plan.describe(),
                "warnings": list(plan.warnings),
            },
        }

    async def credential_statuses(self) -> dict[str, dict[str, Any]]:
        return {
            provider_id: dict(status)
            for provider_id, status in (await self.credential_snapshot()).statuses.items()
        }

    def credential_cache_stats(self) -> dict[str, Any]:
        """Cache counters for the readiness payload. Counts only, never content."""
        if self.credential_store is None:
            return {"enabled": False}
        return self.credential_store.cache_stats()

    def custom_provider_cache_stats(self) -> dict[str, Any]:
        """Cache counters for the readiness payload. Counts only, never content."""
        if self.custom_providers is None:
            return {"enabled": False}
        return self.custom_providers.cache_stats()

    def credential_storage_status(self) -> dict[str, Any]:
        """Whether this server can store keys at all.

        A deployment with no encryption key must say so rather than silently
        refusing every save: the screen hides the key fields and points at
        CREDENTIAL_ENCRYPTION_KEY instead.
        """
        if self.credential_store is None:
            return {
                "available": False,
                "source": "",
                "reason": "Credential storage is not enabled on this server.",
            }
        return self.credential_store.encryption_status()

    async def test_target(
        self,
        provider_id: str,
        model: str,
        *,
        api_key: str = "",
    ) -> dict[str, Any]:
        """One real round trip to a provider, for the settings screen's Test button.

        Deliberately runs a *single* target with no fallback: the operator is
        asking about this provider, and silently succeeding via a different one
        would be the opposite of useful. Retries still apply, so a transient
        blip does not report a working provider as broken.

        ``api_key`` probes a key that has *not* been saved. It is held for this
        call only, never written and never logged, so a mistyped credential can
        be caught before it replaces a working one. Without it the test uses
        whatever the server would actually use for a scoring run - which is the
        question an operator is really asking after a rotation - and the verdict
        is recorded against the stored key so the screen still shows it after a
        reload.
        """
        resolved_id = str(provider_id or "").strip()
        probe_key = str(api_key or "").strip()
        # Resolved before the id is checked, because "does this provider exist"
        # is now a question about this deployment's catalogue rather than about
        # the build - and the answer has to come from the same snapshot the call
        # itself will be made against.
        resolved = await self.resolve(extra_keys={resolved_id: probe_key} if probe_key else None)
        if not resolved.catalog.contains(resolved_id):
            known = ", ".join(resolved.catalog.provider_ids())
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
        providers = resolved.providers
        # Whatever key this call will actually send, so it can be scrubbed out of
        # any message that comes back. Some vendors quote the rejected credential
        # in their 401 body, and that body is rendered in the browser and written
        # to the log.
        target_provider = providers.get(resolved_id)
        sent_key = str(getattr(getattr(target_provider, "credentials", None), "api_key", "") or "")
        secrets = tuple(value for value in (probe_key, sent_key) if value)
        source = resolved.sources.get(resolved_id, "none")
        router = LLMRouter(dict(providers), config)
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
            message = redact_secrets(error.message, secrets)
            await self._remember_test(resolved_id, ok=False, error=message, probe=bool(probe_key))
            return {
                "ok": False,
                "providerId": resolved_id,
                "model": config.primary.model,
                "credentialSource": source,
                "error": message,
                "elapsedSeconds": round(time.monotonic() - started, 3),
                "attempts": [
                    {**record.to_public(), "error": redact_secrets(record.error, secrets)}
                    for record in getattr(error, "attempts", ())
                ],
            }
        except Exception as error:  # pragma: no cover - defensive
            logger.exception("LLM connection test raised an unexpected error.")
            message = redact_secrets(f"{type(error).__name__}: {error}", secrets)
            await self._remember_test(resolved_id, ok=False, error=message, probe=bool(probe_key))
            return {
                "ok": False,
                "providerId": resolved_id,
                "model": config.primary.model,
                "credentialSource": source,
                "error": message,
                "elapsedSeconds": round(time.monotonic() - started, 3),
            }

        await self._remember_test(resolved_id, ok=True, error="", probe=bool(probe_key))
        return {
            "ok": True,
            "providerId": response.provider_id,
            "model": response.model,
            "mode": response.mode,
            "credentialSource": source,
            "elapsedSeconds": round(time.monotonic() - started, 3),
            "attempts": [record.to_public() for record in response.attempts],
            "usage": dict(response.usage or {}),
        }

    async def _remember_test(self, provider_id: str, *, ok: bool, error: str, probe: bool) -> None:
        """Persist a verdict against the *stored* key only.

        A probe of an unsaved key says nothing about the key on the row, so
        writing its result there would put a red cross next to a credential that
        still works.
        """
        if probe or self.credential_store is None:
            return
        try:
            await self.credential_store.record_test(provider_id, ok=ok, error=error)
        except Exception:  # pragma: no cover - defensive
            logger.exception("Failed to record the connection-test result for '%s'.", provider_id)
