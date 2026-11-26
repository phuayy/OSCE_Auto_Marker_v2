from __future__ import annotations

import re
from datetime import UTC, date, datetime

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse

from app.api.dependencies import get_container
from app.core.exceptions import AppError
from app.domain.results_export import (
    MAX_ROOT_SESSION_IDS,
    MAX_SESSION_IDS,
    MAX_STUDENT_IDS,
    ExportLayout,
    ResultsExportFilter,
    ScoredResultType,
)
from app.services.container import AppContainer

router = APIRouter(prefix="/analytics", tags=["analytics"])

_ALL_RESULT_TYPES = "all"
_ISO_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")


@router.get("/assessments")
async def list_assessment_results(container: AppContainer = Depends(get_container)) -> dict[str, object]:
    """Flat per-result rows (session + student + scores) — the frontend
    analytics page filters and aggregates client-side."""
    return {"results": await container.assessments.list_result_rows()}


@router.get("/export")
async def export_criterion_marks(
    layout: str = Query(ExportLayout.LONG.value, alias="format"),
    result_type: str = Query(_ALL_RESULT_TYPES, alias="resultType"),
    root_session_ids: list[str] = Query(default=[], alias="rootSessionIds"),
    session_ids: list[str] = Query(default=[], alias="sessionIds"),
    student_ids: list[str] = Query(default=[], alias="studentIds"),
    date_from: str | None = Query(default=None, alias="from"),
    date_to: str | None = Query(default=None, alias="to"),
    container: AppContainer = Depends(get_container),
) -> StreamingResponse:
    """Per-criterion marks as a CSV attachment, streamed.

    The parameters are the dimensions ``src/lib/analyticsFilters.js`` can
    express — recordings (``rootSessionIds``), scored sessions, students, a
    day range — so the Analytics page passes the filter it is showing. Every
    rejection is a 422 naming what to change; the schema-level 400 FastAPI
    would produce for a bad enum or date is deliberately bypassed so a caller
    reads one status for "this request cannot be an export".
    """
    export_layout = _parse_layout(layout)
    export_filter = ResultsExportFilter(
        result_types=_parse_result_types(result_type),
        root_session_ids=_parse_ids(root_session_ids, "rootSessionIds", MAX_ROOT_SESSION_IDS),
        session_ids=_parse_ids(session_ids, "sessionIds", MAX_SESSION_IDS),
        student_ids=_parse_ids(student_ids, "studentIds", MAX_STUDENT_IDS),
        date_from=_parse_day(date_from, "from"),
        date_to=_parse_day(date_to, "to"),
    )
    if export_filter.date_from and export_filter.date_to and export_filter.date_from > export_filter.date_to:
        raise AppError("'from' must not be after 'to'.", status_code=422)

    body = await container.assessments.open_results_export(export_filter, export_layout)
    filename = f"osce-criteria-{datetime.now(UTC).date().isoformat()}.csv"
    return StreamingResponse(
        body,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _parse_layout(value: str) -> ExportLayout:
    try:
        return ExportLayout(value)
    except ValueError:
        allowed = ", ".join(member.value for member in ExportLayout)
        raise AppError(f"'format' must be one of: {allowed}.", status_code=422) from None


def _parse_result_types(value: str) -> tuple[ScoredResultType, ...]:
    if value == _ALL_RESULT_TYPES:
        return tuple(ScoredResultType)
    try:
        return (ScoredResultType(value),)
    except ValueError:
        allowed = ", ".join([*(member.value for member in ScoredResultType), _ALL_RESULT_TYPES])
        raise AppError(f"'resultType' must be one of: {allowed}.", status_code=422) from None


def _parse_ids(values: list[str], name: str, limit: int) -> tuple[str, ...]:
    if len(values) > limit:
        raise AppError(f"'{name}' accepts at most {limit} ids ({len(values)} given).", status_code=422)
    if any(not value.strip() for value in values):
        raise AppError(f"'{name}' must not contain empty ids.", status_code=422)
    return tuple(dict.fromkeys(values))


def _parse_day(value: str | None, name: str) -> date | None:
    if value is None:
        return None
    try:
        if not _ISO_DAY.match(value):
            raise ValueError(value)
        return date.fromisoformat(value)
    except ValueError:
        raise AppError(f"'{name}' must be a date written YYYY-MM-DD.", status_code=422) from None
