from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.dependencies import get_container
from app.schemas.settings import UpdateSettingsRequest
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


@router.put("")
async def update_settings(
    payload: UpdateSettingsRequest,
    container: AppContainer = Depends(get_container),
) -> dict[str, object]:
    return {"settings": await container.app_settings.set_values(payload.model_dump())}
