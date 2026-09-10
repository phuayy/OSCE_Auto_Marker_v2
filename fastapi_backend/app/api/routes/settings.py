from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status

from app.api.dependencies import get_auth_payload, get_container
from app.llm import registry as llm_registry
from app.pipeline import person_presets
from app.schemas.settings import (
    SetProviderKeyRequest,
    TestLLMTargetRequest,
    UpdateSettingsRequest,
)
from app.services.container import AppContainer
from app.services.provider_credential_service import CredentialError

router = APIRouter(prefix="/settings", tags=["settings"])


@router.get("")
async def get_settings(container: AppContainer = Depends(get_container)) -> dict[str, object]:
    return {"settings": await container.app_settings.get_all()}


@router.get("/transcription-engines")
async def get_transcription_engines(
    container: AppContainer = Depends(get_container),
) -> dict[str, object]:
    """Engines this build ships, their option schemas, this deployment's
    defaults for each, and whether each one can actually run here.

    The settings screen renders its controls from this response, so shipping a
    new engine needs no frontend change.
    """
    return await container.transcription.describe()


@router.get("/segmentation-presets")
async def get_segmentation_presets() -> dict[str, object]:
    """Occupancy presets the person detector offers, and their numbers.

    Same contract as the transcription-engines and llm-providers endpoints: the
    upload screen renders its picker from this response, so retuning a preset
    or adding one is a backend change with no frontend edit. The bounds are
    published alongside so the custom form validates the same range the API
    enforces instead of hardcoding a second copy.
    """
    return {
        "presets": person_presets.describe_presets(),
        "defaultPreset": person_presets.DEFAULT_PRESET,
        "customPreset": person_presets.CUSTOM_PRESET,
        "bounds": {
            "minPeople": list(person_presets.MIN_PEOPLE_RANGE),
            "minBoxHeightRatio": list(person_presets.MIN_BOX_HEIGHT_RATIO_RANGE),
            "minSessionSeconds": list(person_presets.MIN_SESSION_SECONDS_RANGE),
        },
    }


@router.get("/llm-providers")
async def get_llm_providers(container: AppContainer = Depends(get_container)) -> dict[str, object]:
    """Scoring providers this build ships, their models, and the current routing.

    Same contract as the transcription-engines endpoint: the settings screen
    renders its dropdowns from this response, so a provider added to the
    backend registry appears in the UI with no frontend change. Availability
    reflects whether a key is configured *on this machine* — the response never
    contains a key itself.
    """
    return await container.llm_settings.describe()


@router.post("/llm-providers/test")
async def test_llm_provider(
    payload: TestLLMTargetRequest,
    container: AppContainer = Depends(get_container),
) -> dict[str, object]:
    """One live round trip to a provider, so a bad key is found in the settings
    screen rather than forty minutes into a scoring run.

    Always 200: a failed probe is a *result* the screen renders, not an API
    error, and the body carries the provider's own message plus the attempt
    trail. Any credential that appears in that message is redacted first — some
    vendors quote the key they rejected.
    """
    return await container.llm_settings.test_target(
        payload.providerId,
        payload.model,
        api_key=payload.apiKey,
    )


def _resolve_provider_id(provider_id: str) -> str:
    resolved = str(provider_id or "").strip()
    if resolved not in llm_registry.PROVIDER_FACTORIES:
        known = ", ".join(llm_registry.provider_ids())
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Unknown LLM provider '{resolved}'. Available: {known}.",
        )
    return resolved


@router.put("/llm-providers/{provider_id}/key")
async def set_llm_provider_key(
    provider_id: str,
    payload: SetProviderKeyRequest,
    request: Request,
    container: AppContainer = Depends(get_container),
) -> dict[str, object]:
    """Store or rotate one provider's API key.

    Write-only by design: the response is the same provider description every
    other settings read gets, so the screen refreshes in one round trip without
    the key ever travelling back. The new key applies to the next scoring run in
    every process — no restart, no redeploy — which is the whole point of
    letting an operator rotate here.
    """
    resolved = _resolve_provider_id(provider_id)
    actor = str((get_auth_payload(request) or {}).get("username") or "")
    try:
        await container.provider_credentials.set_key(resolved, payload.apiKey, actor=actor)
    except CredentialError as error:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(error)) from None
    return await container.llm_settings.describe()


@router.delete("/llm-providers/{provider_id}/key")
async def clear_llm_provider_key(
    provider_id: str,
    request: Request,
    container: AppContainer = Depends(get_container),
) -> dict[str, object]:
    """Revoke the stored key for one provider.

    Deleting here does not revoke it at the vendor — that is a separate action
    on their dashboard — but it does stop this deployment sending it, which is
    the half an operator can do from this screen during an incident. Whatever
    the environment configures, if anything, takes over again.
    """
    resolved = _resolve_provider_id(provider_id)
    actor = str((get_auth_payload(request) or {}).get("username") or "")
    await container.provider_credentials.clear_key(resolved, actor=actor)
    return await container.llm_settings.describe()


@router.put("")
async def update_settings(
    payload: UpdateSettingsRequest,
    container: AppContainer = Depends(get_container),
) -> dict[str, object]:
    return {"settings": await container.app_settings.set_values(payload.model_dump())}
