from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.dependencies import get_container, require_admin
from app.core.exceptions import AppError
from app.services.container import AppContainer

# Read-only audit history — see PromptRegistryService's module docstring. No
# marker-facing counterpart: unlike corpora, nothing here is a marker's input
# to a run, so the whole surface is admin-only.
router = APIRouter(
    prefix="/admin/prompt-versions", tags=["prompt-versions"], dependencies=[Depends(require_admin)]
)


@router.get("")
async def list_prompt_versions(
    prompt_key: str | None = None,
    container: AppContainer = Depends(get_container),
) -> dict[str, object]:
    return {"promptVersions": await container.prompt_registry.list_versions(prompt_key)}


@router.get("/{record_id}")
async def get_prompt_version(
    record_id: str,
    container: AppContainer = Depends(get_container),
) -> dict[str, object]:
    record = await container.prompt_registry.get(record_id)
    if record is None:
        raise AppError("Prompt version not found.", status_code=404)
    return {"promptVersion": record}
