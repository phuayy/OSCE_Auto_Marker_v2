"""Resolving each provider's key and endpoint from the environment.

Keys stay in environment variables rather than the settings database on
purpose. The settings row is operator-editable through an authenticated HTTP
API and is dumped verbatim into GET /api/settings; a credential stored there
would be readable by anyone who can open the settings screen, and would end up
in database backups. The database records *which* provider to use; the
deployment records how to authenticate to it.

Each provider names the variables it accepts (``api_key_env``), so adding a
provider adds its credentials automatically.
"""
from __future__ import annotations

import os
from typing import Mapping

from app.llm import registry
from app.llm.base import ProviderCredentials


def resolve_credentials(
    provider_id: str,
    env: Mapping[str, str] | None = None,
    *,
    overrides: Mapping[str, str] | None = None,
) -> ProviderCredentials:
    """Credentials for one provider.

    ``overrides`` lets the API supply a key it holds in memory but that is not
    in ``os.environ`` — the NVIDIA key loaded from the platform secrets file is
    the existing case.
    """
    source = os.environ if env is None else env
    descriptor = registry.descriptor_for(provider_id)
    if descriptor is None:
        return ProviderCredentials()

    api_key = str((overrides or {}).get(provider_id) or "").strip()
    if not api_key:
        for name in descriptor.api_key_env:
            candidate = str(source.get(name) or "").strip()
            # Placeholder values in a committed .env.example must not read as
            # "configured" — a 401 forty minutes into a run is a bad way to
            # discover the key was never filled in.
            if candidate and not (candidate.startswith("<") and candidate.endswith(">")):
                api_key = candidate
                break

    base_url = ""
    if descriptor.base_url_env:
        base_url = str(source.get(descriptor.base_url_env) or "").strip()

    return ProviderCredentials(api_key=api_key, base_url=base_url or descriptor.default_base_url)


def resolve_all(
    env: Mapping[str, str] | None = None,
    *,
    overrides: Mapping[str, str] | None = None,
) -> dict[str, ProviderCredentials]:
    return {
        provider_id: resolve_credentials(provider_id, env, overrides=overrides)
        for provider_id in registry.provider_ids()
    }


def credential_env_for(
    provider_ids: list[str],
    env: Mapping[str, str] | None = None,
    *,
    overrides: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """The environment variables a subprocess needs for the given providers.

    Only the providers actually routed to are forwarded. A scoring subprocess
    has no reason to hold a key for a vendor it will never call, and narrowing
    the blast radius of a crash dump or a leaked log costs nothing here.
    """
    forwarded: dict[str, str] = {}
    for provider_id in provider_ids:
        descriptor = registry.descriptor_for(provider_id)
        if descriptor is None:
            continue
        credentials = resolve_credentials(provider_id, env, overrides=overrides)
        if credentials.api_key and descriptor.api_key_env:
            forwarded[descriptor.api_key_env[0]] = credentials.api_key
        if descriptor.base_url_env and credentials.base_url and credentials.base_url != descriptor.default_base_url:
            forwarded[descriptor.base_url_env] = credentials.base_url
    return forwarded
