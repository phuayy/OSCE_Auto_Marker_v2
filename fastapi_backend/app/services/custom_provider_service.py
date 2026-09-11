"""Operator-defined scoring providers: storage, caching and the live catalogue.

This is the third value on the scoring hot path, and it is cached for the same
reason the other two are. Resolving "which providers exist" happens before every
assessment, in the API process and in the Hatchet worker alike; the table
changes when an institution signs with a vendor, which is a handful of times a
year. Querying it per run is pure waste.

Caching it is only defensible because a change reaches every process. That is
the identical contract :class:`~app.services.provider_credential_service.ProviderCredentialService`
rests on, and it is reused rather than reinvented:

* **Push** — ``llm_providers`` is in ``TRACKED_TABLES``, so a write fires the
  trigger that bumps ``table_versions`` and (on PostgreSQL) issues a
  ``pg_notify``. ``ChangeFeedService`` turns that into an observer callback in
  every listening process, so a definition edited in the API evicts the worker's
  catalogue within milliseconds.
* **Token comparison** — the snapshot records the counter it was built from, and
  a hit requires that counter to still match. This covers the window while a
  listener reconnects, a SQLite deployment with no ``NOTIFY``, and an event
  dropped under back-pressure.
* **Local writes evict directly**, because with the listener connected the token
  is answered from memory — so the response to an edit would otherwise be built
  from the catalogue the edit replaced.
* **No change feed means no cache at all.** A stale catalogue is not a
  cosmetic problem: an edited endpoint that has not propagated means this
  deployment's API key still going to the address the definition used to name.

What is cached is the *catalogue*, not the rows — built once per change rather
than per read. Building it involves parsing and validating every definition, so
it is the expensive part, and it is the part every caller actually wants.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from app.core.snapshot_cache import SnapshotCache
from app.llm.catalog import ProviderCatalog, builtin_catalog
from app.llm.custom import CustomProviderError, CustomProviderSpec
from app.llm.registry import PROVIDER_FACTORIES
from app.repositories.custom_provider_repository import CustomProviderRepository

if TYPE_CHECKING:  # pragma: no cover - import cycle guard
    from app.services.change_feed_service import ChangeFeedService

logger = logging.getLogger(__name__)

# The table whose counter this cache watches. Must match the name the trigger
# reports, which is the physical table name.
PROVIDERS_TABLE = "llm_providers"

# A deployment is not expected to need dozens of vendors, and an unbounded list
# would be forwarded into every scoring subprocess's environment — where
# operating systems impose their own, much less friendly, limit.
MAX_CUSTOM_PROVIDERS = 25


class CustomProviderService:
    def __init__(
        self,
        repository: CustomProviderRepository,
        *,
        changes: "ChangeFeedService | None" = None,
    ) -> None:
        self.repository = repository
        self._cache: SnapshotCache[ProviderCatalog] = SnapshotCache(
            "custom-providers",
            token_provider=(
                (lambda: changes.token((PROVIDERS_TABLE,))) if changes is not None else None
            ),
        )
        if changes is not None:
            # How an edit in another process reaches this one. Registered at
            # construction because the observer list is only read after start().
            changes.add_change_observer(self._cache.observer_for(PROVIDERS_TABLE))

    # --- cache -------------------------------------------------------------

    def invalidate(self, reason: str = "") -> None:
        self._cache.invalidate(reason)

    def cache_stats(self) -> dict[str, Any]:
        return self._cache.stats()

    async def catalog(self) -> ProviderCatalog:
        """Every provider this deployment can route to right now.

        Answered from memory while the snapshot is current. A read failure
        degrades to the shipped set rather than raising: losing the custom
        providers costs the runs that selected one, but raising here would cost
        every run, including the ones using a provider that shipped in the build.
        """
        try:
            return await self._cache.get(self._build)
        except Exception:
            logger.exception(
                "Failed to load the operator-defined LLM providers; falling back to the "
                "providers this build ships."
            )
            return builtin_catalog()

    async def _build(self) -> ProviderCatalog:
        return builtin_catalog().with_custom(await self._load_specs())

    async def _load_specs(self) -> list[CustomProviderSpec]:
        """Every stored definition, skipping any that no longer parses.

        A row can stop parsing when a validation rule tightens between releases.
        One bad row must not take out the others, and certainly must not take out
        scoring — so it is logged and dropped, and the settings screen shows the
        provider as simply absent, which is the state the router will act on.
        """
        specs: list[CustomProviderSpec] = []
        for record in await self.repository.list_all():
            try:
                specs.append(CustomProviderRepository.to_spec(record))
            except CustomProviderError as error:
                logger.warning(
                    "Stored custom provider '%s' is no longer a valid definition and was "
                    "skipped: %s",
                    record.id,
                    error,
                )
        return specs

    # --- reads -------------------------------------------------------------

    async def list_specs(self) -> list[CustomProviderSpec]:
        """The definitions, for the settings screen's list.

        Read from the cached catalogue so the screen and the router can never
        show different answers — the bug that a second query would eventually
        introduce.
        """
        return list((await self.catalog()).custom.values())

    async def get_spec(self, provider_id: str) -> CustomProviderSpec | None:
        return (await self.catalog()).spec_for(provider_id)

    # --- writes ------------------------------------------------------------

    async def save(
        self,
        raw: Any,
        *,
        provider_id: str = "",
        actor: str = "",
        allow_create: bool = True,
    ) -> CustomProviderSpec:
        """Validate and store one definition.

        Parsing happens before anything is written, so a rejected definition
        leaves the deployment exactly as it was — an operator mid-edit must never
        be able to half-save a provider that scoring then tries to call.
        """
        spec = CustomProviderSpec.from_raw(raw, provider_id=provider_id)

        if spec.id in PROVIDER_FACTORIES:
            raise CustomProviderError(
                f"'{spec.id}' is the id of a provider this build already ships. "
                "Choose a different id — redefining a shipped provider is not allowed."
            )

        existing = await self.repository.get(spec.id)
        if existing is None and not allow_create:
            raise CustomProviderError(f"No custom provider called '{spec.id}' exists.")
        if existing is None:
            # Counted against stored rows, not the cached catalogue: the cache
            # drops definitions that fail to parse, and a limit that ignored them
            # would let a deployment accumulate rows without bound.
            if len(await self.repository.list_all()) >= MAX_CUSTOM_PROVIDERS:
                raise CustomProviderError(
                    f"This deployment already has {MAX_CUSTOM_PROVIDERS} custom providers, "
                    "which is the maximum. Remove one before adding another."
                )

        await self.repository.upsert(spec, updated_by=str(actor or "") or None)
        # Evicted here rather than waiting for this process's own announcement
        # to come back round: with the listener connected the token is served
        # from memory, so the response to this very request would otherwise be
        # built from the catalogue the write just replaced.
        self.invalidate(f"custom provider '{spec.id}' saved")
        logger.info(
            "Custom LLM provider '%s' (%s, %s) saved by %s.",
            spec.id,
            spec.resolved_base_url(),
            spec.api_format,
            actor or "unknown",
        )
        return spec

    async def delete(self, provider_id: str, *, actor: str = "") -> bool:
        removed = await self.repository.delete(str(provider_id or "").strip())
        if removed:
            self.invalidate(f"custom provider '{provider_id}' removed")
            logger.info(
                "Custom LLM provider '%s' removed by %s; any routing that named it falls back "
                "to the next usable target.",
                provider_id,
                actor or "unknown",
            )
        return removed
