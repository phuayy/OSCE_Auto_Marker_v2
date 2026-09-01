from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.dependencies import get_container
from app.schemas.settings import TestLLMTargetRequest, UpdateSettingsRequest
from app.services.container import AppContainer

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
    trail.
    """
    return await container.llm_settings.test_target(payload.providerId, payload.model)


@router.put("")
async def update_settings(
    payload: UpdateSettingsRequest,
    container: AppContainer = Depends(get_container),
) -> dict[str, object]:
    return {"settings": await container.app_settings.set_values(payload.model_dump())}
