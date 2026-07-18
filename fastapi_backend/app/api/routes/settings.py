from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.dependencies import get_container
from app.schemas.settings import UpdateSettingsRequest
from app.services.container import AppContainer

router = APIRouter(prefix="/settings", tags=["settings"])


@router.get("")
async def get_settings(container: AppContainer = Depends(get_container)) -> dict[str, object]:
    return {"settings": await container.app_settings.get_all()}


@router.put("")
async def update_settings(
    payload: UpdateSettingsRequest,
    container: AppContainer = Depends(get_container),
) -> dict[str, object]:
    return {"settings": await container.app_settings.set_values(payload.model_dump())}
