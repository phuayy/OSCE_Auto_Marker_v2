"""The set of providers available *to a particular run* — shipped plus custom.

``registry.py`` answers "what did this build ship". That was the whole answer
while providers were a build-time decision. Now an operator can define one, so
the answer is per-deployment and can change between two scoring runs in the same
process, and a module-level dict is the wrong shape for it: it would have to be
mutated, and every reader would be racing whoever last edited the settings
screen.

A :class:`ProviderCatalog` is that answer as an immutable value instead. One is
built from the database (in the API and the worker) or from an environment
variable (in a scoring subprocess), threaded through a request, and discarded.
Two runs can hold different catalogues without either being wrong, which is
exactly what "the operator added a provider mid-queue" means.

**Shipped providers always win a collision.** A definition whose id matches a
built-in one is ignored rather than allowed to redefine it: otherwise a row in
the database could silently repoint ``openai`` at an endpoint of someone's
choosing, which is a credential-exfiltration primitive, not a feature. The id
is rejected at the API boundary too — this is the second line, for a row that
predates a newly shipped provider.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping

from app.llm import registry
from app.llm.base import LLMProvider, ProviderCredentials, ProviderDescriptor
from app.llm.custom import CustomProviderError, CustomProviderSpec
from app.llm.providers.custom import build_custom_provider

logger = logging.getLogger(__name__)

ProviderFactory = Callable[[ProviderCredentials], LLMProvider]

# Carries the custom definitions into a scoring subprocess, alongside
# OSCE_LLM_ROUTING. Credential-free by construction, so it is safe to log.
CUSTOM_PROVIDERS_ENV_VAR = "OSCE_LLM_CUSTOM_PROVIDERS"

SCHEMA = "osce-llm-custom-providers-v1"


@dataclass(frozen=True)
class ProviderCatalog:
    """Every provider one run may route to, and how to build each of them."""

    factories: Mapping[str, ProviderFactory]
    descriptors: Mapping[str, ProviderDescriptor]
    default_provider_id: str
    # Definitions behind the custom entries, kept so the settings screen can
    # render an edit form and the subprocess handoff can re-serialise them.
    custom: Mapping[str, CustomProviderSpec] = field(default_factory=dict)

    # -- queries ------------------------------------------------------------

    def provider_ids(self) -> list[str]:
        return list(self.factories)

    def contains(self, provider_id: str) -> bool:
        return str(provider_id or "").strip() in self.factories

    def is_custom(self, provider_id: str) -> bool:
        return str(provider_id or "").strip() in self.custom

    def descriptor_for(self, provider_id: str) -> ProviderDescriptor | None:
        return self.descriptors.get(str(provider_id or "").strip())

    def spec_for(self, provider_id: str) -> CustomProviderSpec | None:
        return self.custom.get(str(provider_id or "").strip())

    # -- construction -------------------------------------------------------

    def build(self, provider_id: str, credentials: ProviderCredentials) -> LLMProvider:
        return self.factories[str(provider_id).strip()](credentials)

    def build_all(
        self, credentials_by_provider: Mapping[str, ProviderCredentials]
    ) -> dict[str, LLMProvider]:
        """Instantiate every provider.

        Construction is inert — no network, no client objects, no key validation
        — so a provider with no key is still built. That is what lets the
        settings screen report it as *available: false* with a reason rather
        than omitting it and leaving the operator guessing.
        """
        return {
            provider_id: factory(credentials_by_provider.get(provider_id) or ProviderCredentials())
            for provider_id, factory in self.factories.items()
        }

    def with_custom(self, specs: Iterable[CustomProviderSpec]) -> "ProviderCatalog":
        """This catalogue plus the given definitions.

        Disabled definitions are dropped here rather than filtered by each
        caller: "disabled" has to mean the router cannot reach it, not merely
        that the screen greys it out.
        """
        factories = dict(self.factories)
        descriptors = dict(self.descriptors)
        custom = dict(self.custom)

        for spec in specs:
            if not spec.enabled:
                continue
            if spec.id in registry.PROVIDER_FACTORIES:
                logger.warning(
                    "Ignoring the custom provider definition '%s': that id belongs to a provider "
                    "this build ships, and a stored row must not be able to repoint it.",
                    spec.id,
                )
                continue
            factories[spec.id] = _factory_for(spec)
            descriptors[spec.id] = spec.to_descriptor()
            custom[spec.id] = spec

        return ProviderCatalog(
            factories=factories,
            descriptors=descriptors,
            default_provider_id=self.default_provider_id,
            custom=custom,
        )

    # -- subprocess handoff -------------------------------------------------

    def to_env(self) -> dict[str, str]:
        """The custom definitions, for a subprocess's environment.

        Omitted entirely when there are none, so a deployment that never defines
        a provider hands its scorers exactly the environment it always did.
        """
        if not self.custom:
            return {}
        payload = {
            "schema": SCHEMA,
            "providers": [spec.to_public() for spec in self.custom.values()],
        }
        return {CUSTOM_PROVIDERS_ENV_VAR: json.dumps(payload, separators=(",", ":"))}


def _factory_for(spec: CustomProviderSpec) -> ProviderFactory:
    """Bind one definition to the adapter signature the router expects."""

    def factory(credentials: ProviderCredentials) -> LLMProvider:
        return build_custom_provider(spec, credentials)

    return factory


def builtin_catalog() -> ProviderCatalog:
    """Exactly what this build ships. The base every other catalogue extends."""
    return ProviderCatalog(
        factories=dict(registry.PROVIDER_FACTORIES),
        descriptors=dict(registry.DESCRIPTORS),
        default_provider_id=registry.DEFAULT_PROVIDER_ID,
        custom={},
    )


def specs_from_raw(raw: Any) -> list[CustomProviderSpec]:
    """Parse a serialised definition list, skipping entries that no longer parse.

    Tolerant on purpose. These definitions outlive the release that wrote them,
    and one malformed row must not take out scoring for every other provider —
    the router's whole point is surviving a broken target.
    """
    payload = raw if isinstance(raw, Mapping) else {}
    entries = payload.get("providers")
    if not isinstance(entries, (list, tuple)):
        return []
    specs: list[CustomProviderSpec] = []
    for entry in entries:
        try:
            specs.append(CustomProviderSpec.from_raw(entry))
        except CustomProviderError as error:
            logger.warning("Skipping an unusable custom provider definition: %s", error)
    return specs


def catalog_from_env(env: Mapping[str, str] | None = None) -> ProviderCatalog:
    """The catalogue a scoring subprocess should use.

    No database here: the API serialised the definitions into
    ``OSCE_LLM_CUSTOM_PROVIDERS`` before spawning the process, so a scorer and
    the settings screen can never disagree about what a provider id means.
    Running a scorer by hand with the variable unset yields the shipped set,
    exactly as before this feature existed.
    """
    import os

    source = os.environ if env is None else env
    serialized = str(source.get(CUSTOM_PROVIDERS_ENV_VAR) or "").strip()
    if not serialized:
        return builtin_catalog()
    try:
        parsed = json.loads(serialized)
    except (TypeError, ValueError) as error:
        logger.warning(
            "%s is not valid JSON (%s); falling back to the providers this build ships.",
            CUSTOM_PROVIDERS_ENV_VAR,
            error,
        )
        return builtin_catalog()
    return builtin_catalog().with_custom(specs_from_raw(parsed))
