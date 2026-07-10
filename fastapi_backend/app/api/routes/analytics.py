from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.dependencies import get_container
from app.services.container import AppContainer

router = APIRouter(prefix="/analytics", tags=["analytics"])


@router.get("/assessments")
async def list_assessment_results(container: AppContainer = Depends(get_container)) -> dict[str, object]:
    """Flat per-result rows (session + student + scores) — the frontend
    analytics page filters and aggregates client-side."""
    return {"results": await container.assessments.list_result_rows()}
