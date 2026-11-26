from __future__ import annotations

import json
from collections.abc import AsyncIterator, Sequence
from contextlib import aclosing
from typing import Any

from app.core.artifacts import read_artifact_payload
from app.core.csv_export import CSV_BOM, csv_line
from app.core.exceptions import AppError
from app.domain.enums import AssessmentResultStatus
from app.domain.results_export import (
    LONG_HEADER,
    MAX_EXPORT_ROWS,
    CriterionExportRow,
    ExportLayout,
    ResultsExportFilter,
    WideLayout,
    long_row,
)
from app.repositories.assessment_repository import AssessmentRepository


# Text handed to the client per chunk of a streamed export. Not a limit on the
# file — only how many rows are joined before a write — so a monkeypatched
# smaller value changes chunking and nothing else.
EXPORT_CHUNK_CHARS = 64 * 1024


class AssessmentService:
    def __init__(self, repository: AssessmentRepository) -> None:
        self.repository = repository

    async def record_session_results(self, session: dict[str, Any]) -> None:
        results: list[dict[str, Any]] = []
        for output_key, result_type in (
            ("scores", "content"),
            ("communicationScores", "communication"),
            ("audioProfessionalism", "audio_professionalism"),
        ):
            output = (session.get("outputs") or {}).get(output_key)
            payload = await self._load_payload(output)
            if payload is None:
                continue
            results.append(self._result_payload(result_type, output, payload))

        if results:
            await self.repository.upsert_assessment(session=session, results=results)

    async def list_result_rows(self) -> list[dict[str, Any]]:
        return await self.repository.list_result_rows()

    async def open_results_export(
        self, export_filter: ResultsExportFilter, layout: ExportLayout
    ) -> AsyncIterator[str]:
        """Start a per-criterion CSV export and hand back its text, streamed.

        The rows are counted first and an export over ``MAX_EXPORT_ROWS`` is
        refused here — before any byte exists to be sent — because a file that
        stops half way looks finished. What is returned is an unstarted
        generator: the rows are read as the caller consumes it, never held
        whole. Empty results are a header-only file, not an error.
        """
        count = await self.repository.count_criterion_export_rows(export_filter)
        if count > MAX_EXPORT_ROWS:
            raise AppError(
                f"This export would have {count:,} rows; the limit is {MAX_EXPORT_ROWS:,}. "
                "Narrow the filter (a date range, fewer recordings) and try again.",
                status_code=422,
            )
        lines = self._long_lines(export_filter) if layout is ExportLayout.LONG else self._wide_lines(export_filter)
        return self._chunked(lines)

    async def _long_lines(self, export_filter: ResultsExportFilter) -> AsyncIterator[str]:
        yield csv_line(LONG_HEADER)
        async with aclosing(self.repository.stream_criterion_export_rows(export_filter)) as rows:
            async for row in rows:
                yield csv_line(long_row(row))

    async def _wide_lines(self, export_filter: ResultsExportFilter) -> AsyncIterator[str]:
        # The header needs every column, and a column can first appear on the
        # last row: so the export reads the rows twice — once to learn the
        # columns, once to write — holding only the column map, never the rows.
        layout = WideLayout()
        async with aclosing(self._results(export_filter)) as groups:
            async for group in groups:
                layout.observe(group)
        yield csv_line(layout.header())
        async with aclosing(self._results(export_filter)) as groups:
            async for group in groups:
                yield csv_line(layout.row(group))

    async def _results(self, export_filter: ResultsExportFilter) -> AsyncIterator[Sequence[CriterionExportRow]]:
        """The export's rows regrouped one result at a time (a result's rows are
        adjacent in export order)."""
        group: list[CriterionExportRow] = []
        async with aclosing(self.repository.stream_criterion_export_rows(export_filter)) as rows:
            async for row in rows:
                if group and group[0].result_id != row.result_id:
                    yield group
                    group = []
                group.append(row)
        if group:
            yield group

    @staticmethod
    async def _chunked(lines: AsyncIterator[str]) -> AsyncIterator[str]:
        """Join lines into chunks of about ``EXPORT_CHUNK_CHARS``; the byte-order
        mark leads the first chunk exactly once, even for a header-only file."""
        buffer = [CSV_BOM]
        size = len(CSV_BOM)
        async with aclosing(lines) as source:
            async for line in source:
                buffer.append(line)
                size += len(line)
                if size >= EXPORT_CHUNK_CHARS:
                    yield "".join(buffer)
                    buffer, size = [], 0
        if buffer:
            yield "".join(buffer)

    async def delete_session_results(self, session_id: str) -> bool:
        return await self.repository.delete_for_session(session_id)

    async def _load_payload(self, output: Any) -> dict[str, Any] | None:
        if not isinstance(output, dict):
            return None
        return await read_artifact_payload(output)

    def _result_payload(self, result_type: str, output: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
        summary = payload.get("scoring_summary") if isinstance(payload.get("scoring_summary"), dict) else {}
        criteria = payload.get("criteria") if isinstance(payload.get("criteria"), list) else []
        # The communication scorer's per-criterion max lives only in the
        # top-level scoring_scale ({"All": 3, ...}); propagate it so each
        # criterion row carries max_score for normalised analytics.
        scale = payload.get("scoring_scale")
        default_max_score = (
            max(v for v in scale.values() if isinstance(v, (int, float)))
            if isinstance(scale, dict) and any(isinstance(v, (int, float)) for v in scale.values())
            else None
        )
        return {
            "resultType": result_type,
            "status": AssessmentResultStatus.COMPLETED,
            # "yes_count" is the achieved score for the content scorer's Yes/No
            # checklist summary, which emits no total_score key.
            "scoreTotal": self._first_value(summary, "total_score", "score", "achieved_score", "yes_count"),
            "scoreMax": self._first_value(summary, "max_score", "total_criteria", "maximum_score"),
            "passFail": self._first_value(summary, "pass_fail", "result", "status"),
            "outputPath": output.get("absolutePath"),
            "payload": payload,
            "criteria": [
                self._criterion_payload(item, default_max_score)
                for item in criteria
                if isinstance(item, dict)
            ],
        }

    @classmethod
    def _criterion_payload(cls, item: dict[str, Any], default_max_score: float | None = None) -> dict[str, Any]:
        score = cls._first_value(item, "score", "points")
        max_score = cls._first_value(item, "max_score", "max_points")
        # "value" carries the content scorer's Yes/No checklist verdict; the
        # repository's _bool_or_none maps yes/no strings to booleans.
        passed = cls._first_value(item, "passed", "met", "is_met", "value")
        # Yes/No checklist criteria have no numeric score key — store 1/0 out
        # of 1 so criterion rows aggregate uniformly across result types.
        if score is None and passed is not None:
            token = str(passed).strip().lower()
            if token in {"true", "yes", "y", "1", "pass", "passed"}:
                score, max_score = 1.0, max_score or 1.0
            elif token in {"false", "no", "n", "0", "fail", "failed"}:
                score, max_score = 0.0, max_score or 1.0
        return {
            "criterionKey": cls._first_value(item, "id", "criterion_id", "key", "number"),
            "label": cls._first_value(item, "criterion", "label", "name", "description"),
            "score": score,
            "maxScore": max_score if max_score is not None else default_max_score,
            "passed": passed,
            "isCritical": cls._first_value(item, "is_critical", "critical"),
            "scoreLabel": cls._first_value(item, "score_label", "label_score"),
            "timestamp": cls._first_value(item, "timestamp", "time"),
            "evidence": cls._first_value(item, "evidence", "rationale", "reasoning", "feedback", "reason"),
            "payload": json.loads(json.dumps(item, ensure_ascii=False)),
        }

    @staticmethod
    def _first_value(payload: dict[str, Any], *keys: str) -> Any:
        for key in keys:
            value = payload.get(key)
            if value is not None and value != "":
                return value
        return None
