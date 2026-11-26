from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

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
    parse_range_end,
    parse_range_start,
)
from app.services.container import AppContainer

router = APIRouter(prefix="/analytics", tags=["analytics"])

_ALL_RESULT_TYPES = "all"


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
    time window — so the Analytics page passes the filter it is showing. Every
    rejection is a 422 naming what to change; the schema-level 400 FastAPI
    would produce for a bad enum or date is deliberately bypassed so a caller
    reads one status for "this request cannot be an export".

    ``from`` and ``to`` are each a date (a whole UTC day: ``from`` starts at its
    midnight, ``to`` includes the day) or an ISO-8601 instant with an offset
    (``2026-11-20T00:00:00+08:00``): ``from`` is inclusive and ``to`` is
    inclusive, to the microsecond, on the assessment's creation time.
    """
    export_layout = _parse_layout(layout)
    export_filter = ResultsExportFilter(
        result_types=_parse_result_types(result_type),
        root_session_ids=_parse_ids(root_session_ids, "rootSessionIds", MAX_ROOT_SESSION_IDS),
        session_ids=_parse_ids(session_ids, "sessionIds", MAX_SESSION_IDS),
        student_ids=_parse_ids(student_ids, "studentIds", MAX_STUDENT_IDS),
        created_from=_parse_bound(date_from, "from", parse_range_start),
        created_until=_parse_bound(date_to, "to", parse_range_end),
    )
    if (
        export_filter.created_from
        and export_filter.created_until
        and export_filter.created_from >= export_filter.created_until
    ):
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


def _parse_bound(value: str | None, name: str, parse: Callable[[str], datetime | None]) -> datetime | None:
    if value is None:
        return None
    try:
        return parse(value)
    except ValueError:
        raise AppError(
            f"'{name}' must be a date (YYYY-MM-DD) or an ISO-8601 instant with an offset "
            "(2026-11-20T00:00:00+08:00).",
            status_code=422,
        ) from None
