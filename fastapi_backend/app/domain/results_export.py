"""The whole-exam results export: what a caller may ask for, and the rows and
spreadsheet layouts it is made of.

Two layouts of the same marks, for two ways staff use them:

* **long** — one line per criterion. What a script or a pivot table wants.
* **wide** — a mark-sheet matrix: one line per (scored session, result type)
  and a column per criterion. What goes into the school's sheet.

Everything here is pure: the repository supplies :class:`CriterionExportRow`
in export order, :class:`WideLayout` and :func:`long_row` turn them into cells,
and ``app.core.csv_export`` turns cells into text.

**A wide column is a criterion, not a position.** Criterion numbers are
positions, and a rubric edit or a different case study renumbers and rewords
them (see CLAUDE.md, "Criterion ids are positions"). So a column is keyed by
``(result type, criterion index, label)``: the same criterion across sessions
marked against the same rubric shares one column, and criterion 3 of one
rubric can never land under criterion 3 of another. When the export spans more
than one criteria set for any result type, a ``Rubric`` column numbers the set
each line was marked against, so a reader can tell which block of columns
applies to it.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
import re
from datetime import UTC, date, datetime, time, timedelta
from enum import StrEnum
from typing import Any

# Over this many criterion rows the export is refused rather than started: a
# file that stops half way looks finished, so the caller narrows the filter
# instead. Counted before a byte is sent.
MAX_EXPORT_ROWS = 250_000
MAX_ROOT_SESSION_IDS = 500
MAX_SESSION_IDS = 2_000
MAX_STUDENT_IDS = 2_000


_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_INSTANT = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2}(\.\d{1,6})?)?(Z|[+-]\d{2}:\d{2})$")
_MICROSECOND = timedelta(microseconds=1)


def _parse_instant(text: str) -> datetime:
    if not _INSTANT.match(text):
        raise ValueError(text)
    return datetime.fromisoformat(text).astimezone(UTC)


def parse_range_start(text: str) -> datetime:
    """The inclusive lower bound a ``from`` value names.

    Two spellings, both exact: a date (``2026-11-20``) is the start of that
    UTC day; an instant with an offset (``2026-11-20T00:00:00+08:00``) is
    itself. A bare local time with no offset is refused — it would depend on a
    zone nobody stated. Raises ``ValueError`` for anything else.
    """
    if _DAY.match(text):
        return datetime.combine(date.fromisoformat(text), time.min, tzinfo=UTC)
    return _parse_instant(text)


def parse_range_end(text: str) -> datetime | None:
    """The *exclusive* upper bound a ``to`` value names — ``None`` when there is
    no later moment to exclude from.

    A date is the whole UTC day, so the bound is the next midnight; an instant
    is *inclusive* (a row created at exactly that microsecond is kept), so the
    bound is one microsecond after it, the column's resolution. The two ends
    thus agree with the page's own window: ``created >= from`` and
    ``created <= to``. Raises ``ValueError`` for anything else.
    """
    try:
        if _DAY.match(text):
            return datetime.combine(date.fromisoformat(text) + timedelta(days=1), time.min, tzinfo=UTC)
        return _parse_instant(text) + _MICROSECOND
    except OverflowError:
        return None


class ExportLayout(StrEnum):
    LONG = "long"
    WIDE = "wide"


class ScoredResultType(StrEnum):
    """The result types that carry per-criterion marks and that the Analytics
    page shows — the audio-professionalism result has no rubric marks."""

    CONTENT = "content"
    COMMUNICATION = "communication"


@dataclass(frozen=True, slots=True)
class ResultsExportFilter:
    """What one export covers. An empty id tuple means "no restriction". The
    time window is on the assessment's creation time: ``created_from``
    inclusive, ``created_until`` exclusive, both UTC instants (see
    :func:`parse_range_start` / :func:`parse_range_end` for how a caller's text
    becomes them) — the same dimensions ``src/lib/analyticsFilters.js`` can
    express."""

    result_types: tuple[ScoredResultType, ...] = tuple(ScoredResultType)
    root_session_ids: tuple[str, ...] = ()
    session_ids: tuple[str, ...] = ()
    student_ids: tuple[str, ...] = ()
    created_from: datetime | None = None
    created_until: datetime | None = None


@dataclass(frozen=True, slots=True)
class CriterionExportRow:
    """One criterion of one result, with the context a spreadsheet row needs.

    A result that has no criterion rows still produces one of these, with the
    ``criterion_*`` fields empty, so a scored student never vanishes from a
    mark sheet because their criteria were not recorded.
    """

    result_id: str
    root_session_id: str
    root_session_name: str | None
    root_session_created_at: str | None  # ISO-8601, as the analytics rows carry it
    session_id: str
    student_name: str
    result_type: str
    result_total: float | None
    result_max: float | None
    result_pass_fail: str | None
    criterion_index: int | None
    criterion_label: str | None
    score: float | None
    max_score: float | None
    passed: bool | None
    is_critical: bool | None
    score_label: str | None
    timestamp: str | None


def result_type_label(result_type: str) -> str:
    return result_type.replace("_", " ").capitalize()


def _day(iso_timestamp: str | None) -> str:
    return iso_timestamp[:10] if iso_timestamp else ""


def _yes_no(value: bool | None) -> str:
    if value is None:
        return ""
    return "Yes" if value else "No"


LONG_HEADER: tuple[str, ...] = (
    "Recording",
    "Recording date",
    "Scored session id",
    "Student",
    "Result type",
    "Criterion #",
    "Criterion",
    "Score",
    "Max",
    "Score label",
    "Passed",
    "Critical",
    "Evidence time",
    "Result total",
    "Result max",
    "Result pass/fail",
)


def long_row(row: CriterionExportRow) -> list[Any]:
    return [
        row.root_session_name,
        _day(row.root_session_created_at),
        row.session_id,
        row.student_name,
        result_type_label(row.result_type),
        row.criterion_index,
        row.criterion_label,
        row.score,
        row.max_score,
        row.score_label,
        _yes_no(row.passed),
        _yes_no(row.is_critical),
        row.timestamp,
        row.result_total,
        row.result_max,
        row.result_pass_fail,
    ]


ColumnKey = tuple[str, int, str | None]
_CriteriaSet = tuple[ColumnKey, ...]

_WIDE_LEADING_HEADER = ("Recording", "Recording date", "Scored session id", "Student", "Result type")
_WIDE_TOTALS_HEADER = ("Total", "Max", "Pass/fail")


def _criteria_set(group: Sequence[CriterionExportRow]) -> _CriteriaSet:
    return tuple(
        (row.result_type, row.criterion_index, row.criterion_label)
        for row in group
        if row.criterion_index is not None
    )


class WideLayout:
    """The matrix's columns, learnt from a first pass over the export.

    ``observe`` every result's rows (all the rows of one result, in order),
    then read ``header`` and call ``row`` for each result in a second pass. The
    columns appear in order of first appearance, which is export order — so a
    mark sheet reads in the order the exam was marked, and the layout does not
    depend on which students happen to be in the filter beyond that.
    """

    def __init__(self) -> None:
        self._columns: dict[ColumnKey, int] = {}
        # Per result type: each distinct criteria set, numbered from 1 in the
        # order first seen.
        self._sets: dict[str, dict[_CriteriaSet, int]] = {}

    def observe(self, group: Sequence[CriterionExportRow]) -> None:
        criteria_set = _criteria_set(group)
        if not criteria_set:
            return
        for key in criteria_set:
            self._columns.setdefault(key, len(self._columns))
        numbered = self._sets.setdefault(group[0].result_type, {})
        numbered.setdefault(criteria_set, len(numbered) + 1)

    @property
    def shows_rubric(self) -> bool:
        return any(len(numbered) > 1 for numbered in self._sets.values())

    def header(self) -> list[str]:
        cells = list(_WIDE_LEADING_HEADER)
        if self.shows_rubric:
            cells.append("Rubric")
        cells.extend(_WIDE_TOTALS_HEADER)
        cells.extend(self._column_title(key) for key in self._columns)
        return cells

    def row(self, group: Sequence[CriterionExportRow]) -> list[Any]:
        first = group[0]
        cells: list[Any] = [
            first.root_session_name,
            _day(first.root_session_created_at),
            first.session_id,
            first.student_name,
            result_type_label(first.result_type),
        ]
        if self.shows_rubric:
            cells.append(self._rubric_label(group))
        cells.extend([first.result_total, first.result_max, first.result_pass_fail])
        marks: list[Any] = [None] * len(self._columns)
        for item in group:
            if item.criterion_index is None:
                continue
            # A criterion the first pass did not see means the result was
            # re-scored between the two passes; its cell is left empty rather
            # than shifting every later column.
            position = self._columns.get((item.result_type, item.criterion_index, item.criterion_label))
            if position is not None:
                marks[position] = item.score
        cells.extend(marks)
        return cells

    def _rubric_label(self, group: Sequence[CriterionExportRow]) -> str:
        number = self._sets.get(group[0].result_type, {}).get(_criteria_set(group))
        return f"{result_type_label(group[0].result_type)} {number}" if number else ""

    @staticmethod
    def _column_title(key: ColumnKey) -> str:
        result_type, index, label = key
        title = f"{result_type_label(result_type)} {index}"
        return f"{title}: {label}" if label else title
