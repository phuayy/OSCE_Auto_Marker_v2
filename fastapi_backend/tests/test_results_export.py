"""The whole-exam results export: ``GET /api/analytics/export`` and what sits under it.

Covers the CSV encoder (``app.core.csv_export``), the pure layouts
(``app.domain.results_export``), the streaming query, and the route's
validation. Fixtures are written straight into the assessment tables so every
date, rubric and criterion is exactly what a test says it is.
"""

from __future__ import annotations

import asyncio
import csv
import io
from datetime import UTC, date, datetime
from typing import Any

import pytest
from sqlalchemy import event

from app.api.routes import analytics
from app.core.csv_export import CSV_BOM, csv_cell, csv_line
from app.core.exceptions import AppError
from app.database.models import (
    AssessmentCriterionRecord,
    AssessmentResultRecord,
    AssessmentSessionRecord,
    ExaminerRecord,
    SessionRecord,
    StudentRecord,
)
from app.database.orm import OrmDatabase
from app.domain.results_export import (
    ExportLayout,
    ResultsExportFilter,
    ScoredResultType,
)
from app.repositories.assessment_repository import AssessmentRepository
from app.services import assessment_service
from app.services.assessment_service import AssessmentService
from tests.test_routes import build_test_client


def _at(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=UTC)


# --------------------------------------------------------------------------- csv


@pytest.mark.parametrize("leader", ["=", "+", "-", "@", "\t"])
def test_a_text_cell_that_could_be_a_formula_is_neutralised(leader: str) -> None:
    assert csv_cell(f"{leader}SUM(A1)") == f"'{leader}SUM(A1)"


def test_a_number_is_data_not_a_formula() -> None:
    assert csv_cell(-2.5) == "-2.5"
    assert csv_cell(-3) == "-3"
    assert csv_cell(3.0) == "3"
    assert csv_cell(0.0) == "0"
    assert csv_cell(True) == "True"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, ""),
        ("", ""),
        ("plain", "plain"),
        (" =leading space is text", " =leading space is text"),
        ("a,b", '"a,b"'),
        ('say "hi"', '"say ""hi"""'),
        ("two\nlines", '"two\nlines"'),
        ("windows\r\nbreak", '"windows\nbreak"'),
        ("old mac\rbreak", '"old mac\nbreak"'),
        ("Zoë Müller 李", "Zoë Müller 李"),
        ("=1,2", '"\'=1,2"'),
    ],
)
def test_csv_cell_quoting(value: Any, expected: str) -> None:
    assert csv_cell(value) == expected


def test_csv_line_ends_in_crlf() -> None:
    assert csv_line(["a", None, 1.0]) == "a,,1\r\n"


# ---------------------------------------------------------------------- fixtures


async def add_recording(db: OrmDatabase, recording_id: str, name: str | None, created: str) -> None:
    async with db.transaction() as session:
        session.add(SessionRecord(id=recording_id, name=name, status="completed", created_at=_at(created)))


async def add_result(
    db: OrmDatabase,
    *,
    assessment_id: str,
    student: str,
    result_type: str,
    created: str,
    criteria: list[dict[str, Any]] | None = None,
    parent: str | None = None,
    session_name: str | None = None,
    total: float | None = None,
    maximum: float | None = None,
    pass_fail: str | None = None,
) -> str:
    """One scored result and its criteria; returns the result id."""
    async with db.transaction() as session:
        examiner = await session.get(ExaminerRecord, "ai")
        if examiner is None:
            examiner = ExaminerRecord(id="ai", external_id="ai", display_name="AI")
            session.add(examiner)
        student_row = await session.get(StudentRecord, f"stu-{student}")
        if student_row is None:
            student_row = StudentRecord(id=f"stu-{student}", external_id=student, display_name=student)
            session.add(student_row)
        assessment = await session.get(AssessmentSessionRecord, assessment_id)
        if assessment is None:
            assessment = AssessmentSessionRecord(
                id=assessment_id,
                student_id=student_row.id,
                parent_session_id=parent,
                status="completed",
                session_name=session_name,
                created_at=_at(created),
            )
            session.add(assessment)
        await session.flush()
        result_id = f"res-{assessment_id}-{result_type}"
        session.add(
            AssessmentResultRecord(
                id=result_id,
                assessment_session_id=assessment_id,
                examiner_id="ai",
                result_type=result_type,
                score_total=total,
                score_max=maximum,
                pass_fail=pass_fail,
            )
        )
        await session.flush()
        for index, item in enumerate(criteria or [], start=1):
            session.add(
                AssessmentCriterionRecord(
                    id=f"{result_id}-{index}",
                    result_id=result_id,
                    criterion_index=index,
                    label=item.get("label"),
                    score=item.get("score"),
                    max_score=item.get("max"),
                    passed=item.get("passed"),
                    is_critical=item.get("critical"),
                    score_label=item.get("score_label"),
                    timestamp=item.get("ts"),
                    payload_json={"evidence": "x" * 200},
                )
            )
    return result_id


def crit(label: str | None, score: float | None, maximum: float | None = 1, **extra: Any) -> dict[str, Any]:
    return {"label": label, "score": score, "max": maximum, **extra}


async def seed_exam(db: OrmDatabase) -> None:
    """A long recording with two clips, a standard session, and an orphaned clip.

    * ``rec1`` "Constipation" (2026-01-10): clips ``c1`` Ann (2026-01-10 08:00)
      and ``c2`` Ben (2026-01-11 23:59:59.999999), each with content + communication.
    * ``s3`` "Asthma" (2026-01-20): Cara, assessed 2026-02-01 00:00:00 exactly,
      content + a *different* communication rubric.
    * ``o1``: Dev, a clip whose recording row is gone, 2026-01-05.
    """
    await add_recording(db, "rec1", "Constipation", "2026-01-10 08:00:00")
    await add_recording(db, "s3", "Asthma", "2026-01-20 09:00:00")
    content_a = [crit("Introduces self", 1, passed=True, ts="00:12"), crit("Asks about pain", 0, passed=False, critical=True)]
    comm_a = [
        crit("Rapport", 3, 3, score_label="All"),
        crit("Listening", 2, 3, score_label="Most"),
    ]
    for assessment_id, student, created, parent in (
        ("c1", "Ann", "2026-01-10 08:00:00", "rec1"),
        ("c2", "Ben", "2026-01-11 23:59:59.999999", "rec1"),
    ):
        await add_result(db, assessment_id=assessment_id, student=student, result_type="content", created=created,
                         parent=parent, session_name=f"Constipation - {student}", total=1, maximum=2,
                         pass_fail="Fail", criteria=content_a)
        await add_result(db, assessment_id=assessment_id, student=student, result_type="communication", created=created,
                         parent=parent, total=5, maximum=6, pass_fail="Pass", criteria=comm_a)
    await add_result(db, assessment_id="s3", student="Cara", result_type="content", created="2026-02-01 00:00:00",
                     session_name="Asthma", total=2, maximum=2, pass_fail="Pass",
                     criteria=[crit("Introduces self", 1), crit("Checks inhalers", 1)])
    await add_result(db, assessment_id="s3", student="Cara", result_type="communication", created="2026-02-01 00:00:00",
                     total=4, maximum=6, pass_fail="Pass",
                     criteria=[crit("Rapport", 2, 3), crit("Safety netting", 2, 3)])
    await add_result(db, assessment_id="o1", student="Dev", result_type="content", created="2026-01-05 12:00:00",
                     parent="recording-gone", session_name="Gone - Clip 1", total=0, maximum=2, pass_fail="Fail",
                     criteria=[crit("Introduces self", None, None)])


_DATABASE_COUNTER = 0


def make_service(tmp_path) -> tuple[OrmDatabase, AssessmentService]:
    global _DATABASE_COUNTER
    _DATABASE_COUNTER += 1
    database = OrmDatabase(tmp_path / f"app-{_DATABASE_COUNTER}.sqlite3")
    return database, AssessmentService(AssessmentRepository(database))


async def export_text(service: AssessmentService, layout: ExportLayout, **filter_fields: Any) -> str:
    stream = await service.open_results_export(ResultsExportFilter(**filter_fields), layout)
    return "".join([chunk async for chunk in stream])


def parse(text: str) -> list[list[str]]:
    assert text.startswith(CSV_BOM)
    return list(csv.reader(io.StringIO(text[len(CSV_BOM):], newline="")))


def run_export(tmp_path, layout: ExportLayout = ExportLayout.LONG, *, seed=seed_exam, **filter_fields: Any) -> list[list[str]]:
    async def _run() -> list[list[str]]:
        database, service = make_service(tmp_path)
        await seed(database)
        try:
            return parse(await export_text(service, layout, **filter_fields))
        finally:
            await database.shutdown()

    return asyncio.run(_run())


def column(table: list[list[str]], name: str) -> list[str]:
    index = table[0].index(name)
    return [row[index] for row in table[1:]]


LONG_HEADER = [
    "Recording", "Recording date", "Scored session id", "Student", "Result type", "Criterion #", "Criterion",
    "Score", "Max", "Score label", "Passed", "Critical", "Evidence time", "Result total", "Result max",
    "Result pass/fail",
]


# ------------------------------------------------------------------------- long


def test_long_export_is_one_row_per_criterion_in_recording_date_order(tmp_path) -> None:
    table = run_export(tmp_path)
    assert table[0] == LONG_HEADER
    rows = table[1:]
    # o1 (no recording row: ordered by its own date), then rec1's clips, then s3.
    assert [row[2] for row in rows] == ["o1"] + ["c1"] * 4 + ["c2"] * 4 + ["s3"] * 4
    # Within a scored session: result type, then criterion number.
    assert [(row[4], row[5]) for row in rows[1:5]] == [
        ("Communication", "1"), ("Communication", "2"), ("Content", "1"), ("Content", "2"),
    ]
    content_1 = rows[3]
    assert content_1 == ["Constipation", "2026-01-10", "c1", "Ann", "Content", "1", "Introduces self", "1", "1", "",
                         "Yes", "", "00:12", "1", "2", "Fail"]
    content_2 = rows[4]
    assert content_2[6:13] == ["Asks about pain", "0", "1", "", "No", "Yes", ""]
    assert rows[1][4:10] == ["Communication", "1", "Rapport", "3", "3", "All"]


def test_the_export_is_identical_run_to_run(tmp_path) -> None:
    assert run_export(tmp_path) == run_export(tmp_path)


def test_a_clip_whose_recording_was_deleted_keeps_its_rows_with_a_blank_recording(tmp_path) -> None:
    orphan = [row for row in run_export(tmp_path)[1:] if row[2] == "o1"]
    assert len(orphan) == 1
    assert orphan[0][:2] == ["", ""]
    assert orphan[0][3] == "Dev"
    # A null score, max, label, evidence time and critical flag are empty cells.
    assert orphan[0][7:13] == ["", "", "", "", "", ""]


def test_a_standard_session_is_named_by_its_live_recording_row(tmp_path) -> None:
    s3 = [row for row in run_export(tmp_path)[1:] if row[2] == "s3"]
    assert {row[0] for row in s3} == {"Asthma"} and {row[1] for row in s3} == {"2026-01-20"}


def test_hostile_and_unicode_cells_are_escaped_or_kept(tmp_path) -> None:
    async def seed(db: OrmDatabase) -> None:
        await add_recording(db, "r", '=HYPERLINK("http://x")', "2026-03-01 09:00:00")
        await add_result(
            db, assessment_id="r", student='+Zoë, "the" 李\nnewline', result_type="content",
            created="2026-03-01 09:00:00", session_name="r", total=1, maximum=1, pass_fail="@Pass",
            criteria=[crit("-Opening, then\nask", 1, passed=True, score_label="=1+1", ts="\t9")],
        )

    row = run_export(tmp_path, seed=seed)[1]
    assert row[0] == '\'=HYPERLINK("http://x")'
    assert row[3] == '\'+Zoë, "the" 李\nnewline'
    assert row[6] == "'-Opening, then\nask"
    assert row[9] == "'=1+1" and row[12] == "'\t9" and row[15] == "'@Pass"


@pytest.mark.parametrize("layout", list(ExportLayout))
def test_no_matching_results_is_a_header_only_file_not_an_error(tmp_path, layout: ExportLayout) -> None:
    table = run_export(tmp_path, layout, student_ids=("stu-nobody",))
    assert len(table) == 1 and table[0][0] == "Recording"


def test_the_byte_order_mark_leads_the_file_exactly_once(tmp_path) -> None:
    async def _run() -> str:
        database, service = make_service(tmp_path)
        await seed_exam(database)
        try:
            return await export_text(service, ExportLayout.LONG)
        finally:
            await database.shutdown()

    text = asyncio.run(_run())
    assert text.startswith(CSV_BOM + "Recording,")
    assert text.count(CSV_BOM) == 1
    assert text.endswith("\r\n")


def test_a_result_with_no_criteria_still_appears_in_both_layouts(tmp_path) -> None:
    async def seed(db: OrmDatabase) -> None:
        await add_recording(db, "bare", "Bare", "2026-03-01 09:00:00")
        await add_result(db, assessment_id="bare", student="Eve", result_type="content",
                         created="2026-03-01 09:00:00", total=3, maximum=4, pass_fail="Pass")

    long_table = run_export(tmp_path, seed=seed)
    assert len(long_table) == 2
    assert long_table[1][3] == "Eve" and long_table[1][5:7] == ["", ""] and long_table[1][13:16] == ["3", "4", "Pass"]
    wide = run_export(tmp_path, ExportLayout.WIDE, seed=seed)
    assert len(wide) == 2
    assert wide[1][:3 + 1] == ["Bare", "2026-03-01", "bare", "Eve"] and wide[1][5:8] == ["3", "4", "Pass"]


def test_audio_professionalism_results_are_never_exported(tmp_path) -> None:
    async def seed(db: OrmDatabase) -> None:
        await add_recording(db, "a", "A", "2026-03-01 09:00:00")
        await add_result(db, assessment_id="a", student="Ann", result_type="audio_professionalism",
                         created="2026-03-01 09:00:00", criteria=[crit("Pace", 1)])

    assert len(run_export(tmp_path, seed=seed)) == 1


# ---------------------------------------------------------------------- filters


def scored_sessions(table: list[list[str]]) -> list[str]:
    return sorted(set(column(table, "Scored session id")))


def test_each_filter_alone(tmp_path) -> None:
    assert scored_sessions(run_export(tmp_path, root_session_ids=("rec1",))) == ["c1", "c2"]
    # A clip whose recording is gone is still selectable by the recording id it names.
    assert scored_sessions(run_export(tmp_path, root_session_ids=("recording-gone",))) == ["o1"]
    # A standard session is its own recording.
    assert scored_sessions(run_export(tmp_path, root_session_ids=("s3",))) == ["s3"]
    assert scored_sessions(run_export(tmp_path, session_ids=("c1", "s3"))) == ["c1", "s3"]
    assert scored_sessions(run_export(tmp_path, student_ids=("stu-Ben", "stu-Dev"))) == ["c2", "o1"]
    communication = run_export(tmp_path, result_types=(ScoredResultType.COMMUNICATION,))
    assert set(column(communication, "Result type")) == {"Communication"}
    content = run_export(tmp_path, result_types=(ScoredResultType.CONTENT,))
    assert set(column(content, "Result type")) == {"Content"}


def test_filters_combine_with_and(tmp_path) -> None:
    table = run_export(
        tmp_path,
        root_session_ids=("rec1", "s3"),
        student_ids=("stu-Ben", "stu-Cara"),
        date_from=date(2026, 1, 11),
        result_types=(ScoredResultType.COMMUNICATION,),
    )
    assert scored_sessions(table) == ["c2", "s3"]
    assert set(column(table, "Result type")) == {"Communication"}
    assert scored_sessions(run_export(tmp_path, root_session_ids=("rec1",), student_ids=("stu-Cara",))) == []


def test_date_bounds_are_inclusive_whole_days(tmp_path) -> None:
    # c2 is assessed at 23:59:59.999999 on the 11th; s3 at exactly midnight on 1 Feb.
    assert scored_sessions(run_export(tmp_path, date_to=date(2026, 1, 11))) == ["c1", "c2", "o1"]
    assert scored_sessions(run_export(tmp_path, date_to=date(2026, 1, 10))) == ["c1", "o1"]
    assert scored_sessions(run_export(tmp_path, date_from=date(2026, 1, 11))) == ["c2", "s3"]
    assert scored_sessions(run_export(tmp_path, date_from=date(2026, 2, 1))) == ["s3"]
    assert scored_sessions(run_export(tmp_path, date_to=date(2026, 1, 31), date_from=date(2026, 1, 5))) == ["c1", "c2", "o1"]
    assert scored_sessions(run_export(tmp_path, date_from=date(2026, 1, 11), date_to=date(2026, 1, 11))) == ["c2"]
    assert scored_sessions(run_export(tmp_path, date_to=date.max)) == ["c1", "c2", "o1", "s3"]


# ------------------------------------------------------------------------- wide


WIDE_LEADING = ["Recording", "Recording date", "Scored session id", "Student", "Result type"]


def test_wide_columns_follow_criteria_not_positions(tmp_path) -> None:
    table = run_export(tmp_path, ExportLayout.WIDE)
    assert table[0] == [
        *WIDE_LEADING, "Rubric", "Total", "Max", "Pass/fail",
        "Content 1: Introduces self",
        "Communication 1: Rapport",
        "Communication 2: Listening",
        "Content 2: Asks about pain",
        "Communication 2: Safety netting",
        "Content 2: Checks inhalers",
    ]
    assert len({*table[0]}) == len(table[0])  # no two criteria share a header


def test_wide_has_one_row_per_scored_session_and_result_type(tmp_path) -> None:
    table = run_export(tmp_path, ExportLayout.WIDE)
    rows = table[1:]
    assert [(row[2], row[4]) for row in rows] == [
        ("o1", "Content"),
        ("c1", "Communication"), ("c1", "Content"),
        ("c2", "Communication"), ("c2", "Content"),
        ("s3", "Communication"), ("s3", "Content"),
    ]


def test_the_same_criterion_shares_a_column_and_a_different_rubric_gets_its_own(tmp_path) -> None:
    table = run_export(tmp_path, ExportLayout.WIDE)
    by_key = {(row[2], row[4]): dict(zip(table[0], row, strict=True)) for row in table[1:]}
    # "Introduces self" is criterion 1 under every rubric: one shared column.
    assert by_key[("c1", "Content")]["Content 1: Introduces self"] == "1"
    assert by_key[("s3", "Content")]["Content 1: Introduces self"] == "1"
    assert by_key[("o1", "Content")]["Content 1: Introduces self"] == ""  # o1's score is null
    # Criterion 2 differs between the Constipation rubric and the Asthma one.
    assert by_key[("c1", "Content")]["Content 2: Asks about pain"] == "0"
    assert by_key[("c1", "Content")]["Content 2: Checks inhalers"] == ""
    assert by_key[("s3", "Content")]["Content 2: Checks inhalers"] == "1"
    assert by_key[("s3", "Content")]["Content 2: Asks about pain"] == ""
    # Communication criterion 1 ("Rapport") is shared; criterion 2 is not.
    assert by_key[("c1", "Communication")]["Communication 1: Rapport"] == "3"
    assert by_key[("s3", "Communication")]["Communication 1: Rapport"] == "2"
    assert by_key[("c1", "Communication")]["Communication 2: Listening"] == "2"
    assert by_key[("s3", "Communication")]["Communication 2: Safety netting"] == "2"
    assert by_key[("s3", "Communication")]["Communication 2: Listening"] == ""


def test_the_rubric_column_numbers_each_criteria_set_per_result_type(tmp_path) -> None:
    table = run_export(tmp_path, ExportLayout.WIDE)
    rubric = {(row[2], row[4]): row[5] for row in table[1:]}
    assert rubric == {
        ("o1", "Content"): "Content 1",
        ("c1", "Content"): "Content 2", ("c2", "Content"): "Content 2",
        ("c1", "Communication"): "Communication 1", ("c2", "Communication"): "Communication 1",
        ("s3", "Content"): "Content 3",
        ("s3", "Communication"): "Communication 2",
    }


def test_the_rubric_column_is_absent_when_every_result_shares_one_set(tmp_path) -> None:
    table = run_export(tmp_path, ExportLayout.WIDE, root_session_ids=("rec1",))
    assert "Rubric" not in table[0]
    assert table[0] == [
        *WIDE_LEADING, "Total", "Max", "Pass/fail",
        "Communication 1: Rapport", "Communication 2: Listening",
        "Content 1: Introduces self", "Content 2: Asks about pain",
    ]
    content = run_export(tmp_path, ExportLayout.WIDE, root_session_ids=("rec1",), result_types=(ScoredResultType.CONTENT,))
    assert content[0][-2:] == ["Content 1: Introduces self", "Content 2: Asks about pain"]
    assert content[1][5:] == ["1", "2", "Fail", "1", "0"]


def test_two_in_place_rubric_edits_with_a_reworded_label_do_not_share_a_column(tmp_path) -> None:
    async def seed(db: OrmDatabase) -> None:
        await add_recording(db, "r1", "Before edit", "2026-03-01 09:00:00")
        await add_recording(db, "r2", "After edit", "2026-03-02 09:00:00")
        await add_result(db, assessment_id="r1", student="Ann", result_type="communication", created="2026-03-01 09:00:00",
                         criteria=[crit("Empathy", 3, 3)])
        await add_result(db, assessment_id="r2", student="Ben", result_type="communication", created="2026-03-02 09:00:00",
                         criteria=[crit("Shows empathy", 1, 3)])

    table = run_export(tmp_path, ExportLayout.WIDE, seed=seed)
    assert table[0][-2:] == ["Communication 1: Empathy", "Communication 1: Shows empathy"]
    assert [row[5] for row in table[1:]] == ["Communication 1", "Communication 2"]
    assert table[1][-2:] == ["3", ""] and table[2][-2:] == ["", "1"]


def test_a_criterion_with_no_label_still_gets_its_own_column(tmp_path) -> None:
    async def seed(db: OrmDatabase) -> None:
        await add_recording(db, "r", "R", "2026-03-01 09:00:00")
        await add_result(db, assessment_id="r", student="Ann", result_type="content", created="2026-03-01 09:00:00",
                         criteria=[crit(None, 1), crit("Named", 0)])

    table = run_export(tmp_path, ExportLayout.WIDE, seed=seed)
    assert table[0][-2:] == ["Content 1", "Content 2: Named"]


# ----------------------------------------------------- streaming, cap, SQL shape


def test_the_export_is_streamed_in_several_chunks(tmp_path, monkeypatch) -> None:
    async def _run() -> tuple[list[str], str]:
        database, service = make_service(tmp_path)
        await seed_exam(database)
        try:
            whole = await export_text(service, ExportLayout.LONG)
            monkeypatch.setattr(assessment_service, "EXPORT_CHUNK_CHARS", 200)
            stream = await service.open_results_export(ResultsExportFilter(), ExportLayout.LONG)
            return [chunk async for chunk in stream], whole
        finally:
            await database.shutdown()

    chunks, whole = asyncio.run(_run())
    assert len(chunks) > 3
    assert "".join(chunks) == whole
    assert chunks[0].startswith(CSV_BOM) and all(CSV_BOM not in chunk for chunk in chunks[1:])


def test_closing_the_stream_early_releases_the_query(tmp_path, monkeypatch) -> None:
    async def _run() -> list[list[str]]:
        database, service = make_service(tmp_path)
        await seed_exam(database)
        try:
            monkeypatch.setattr(assessment_service, "EXPORT_CHUNK_CHARS", 100)
            stream = await service.open_results_export(ResultsExportFilter(), ExportLayout.WIDE)
            await anext(stream)
            await stream.aclose()
            monkeypatch.undo()
            return parse(await export_text(service, ExportLayout.LONG))
        finally:
            await database.shutdown()

    assert len(asyncio.run(_run())) == 14


def test_an_export_over_the_cap_is_refused_before_anything_is_streamed(tmp_path, monkeypatch) -> None:
    async def _run() -> None:
        database, service = make_service(tmp_path)
        await seed_exam(database)
        try:
            monkeypatch.setattr(assessment_service, "MAX_EXPORT_ROWS", 13)
            assert len(parse(await export_text(service, ExportLayout.LONG))) == 14  # exactly at the cap
            monkeypatch.setattr(assessment_service, "MAX_EXPORT_ROWS", 12)
            with pytest.raises(AppError) as refused:
                await service.open_results_export(ResultsExportFilter(), ExportLayout.LONG)
            assert refused.value.status_code == 422
            assert "Narrow the filter" in refused.value.message
        finally:
            await database.shutdown()

    asyncio.run(_run())


def test_the_export_never_selects_a_payload_column(tmp_path) -> None:
    async def _run() -> list[str]:
        database, service = make_service(tmp_path)
        await seed_exam(database)
        statements: list[str] = []
        try:
            event.listen(database.engine.sync_engine, "before_cursor_execute",
                         lambda conn, cursor, statement, *args: statements.append(statement))
            for layout in ExportLayout:
                await export_text(service, layout, root_session_ids=("rec1",), date_from=date(2026, 1, 1))
            return statements
        finally:
            await database.shutdown()

    selects = [sql for sql in asyncio.run(_run()) if sql.lstrip().upper().startswith("SELECT")]
    assert selects
    assert all("payload" not in sql.lower() for sql in selects), selects


# ------------------------------------------------------------------------ route


@pytest.fixture
def client(tmp_path):
    test_client = build_test_client(tmp_path)
    test_client.app.include_router(analytics.router, prefix="/api")
    container = test_client.app.state.container
    asyncio.run(seed_exam(container.orm_database))
    token = test_client.post("/api/auth/login", json={"username": "admin", "password": "admin"}).json()["token"]
    test_client.headers["Authorization"] = f"Bearer {token}"
    return test_client


def test_the_route_serves_a_csv_attachment(client) -> None:
    response = client.get("/api/analytics/export")
    assert response.status_code == 200
    assert response.headers["content-type"] == "text/csv; charset=utf-8"
    disposition = response.headers["content-disposition"]
    today = datetime.now(UTC).date().isoformat()
    assert disposition == f'attachment; filename="osce-criteria-{today}.csv"'
    assert response.content.startswith(CSV_BOM.encode("utf-8") + b"Recording,")
    assert len(parse(response.text)) == 14


def test_the_route_passes_every_filter_through(client) -> None:
    response = client.get(
        "/api/analytics/export",
        params=[
            ("format", "wide"), ("resultType", "communication"), ("rootSessionIds", "rec1"), ("rootSessionIds", "s3"),
            ("sessionIds", "c2"), ("sessionIds", "s3"), ("studentIds", "stu-Ben"), ("from", "2026-01-11"), ("to", "2026-01-11"),
        ],
    )
    table = parse(response.text)
    assert [(row[2], row[4]) for row in table[1:]] == [("c2", "Communication")]


def test_the_route_requires_a_signed_in_account(client) -> None:
    client.headers.pop("Authorization")
    assert client.get("/api/analytics/export").status_code == 401


@pytest.mark.parametrize(
    "params",
    [
        {"from": "2026-13-01"}, {"from": "yesterday"}, {"to": "2026-02-30"}, {"from": "20260101"}, {"to": ""},
        {"from": "2026-02-01", "to": "2026-01-01"},
        {"format": "pivot"}, {"resultType": "audio_professionalism"}, {"resultType": "bogus"},
    ],
)
def test_the_route_rejects_a_bad_request_with_422(client, params) -> None:
    response = client.get("/api/analytics/export", params=params)
    assert response.status_code == 422
    assert response.json()["error"]


def test_the_route_enforces_the_id_list_limits(client) -> None:
    def status(name: str, count: int) -> int:
        return client.get("/api/analytics/export", params=[(name, f"id-{i}") for i in range(count)]).status_code

    assert status("rootSessionIds", 500) == 200
    assert status("rootSessionIds", 501) == 422
    assert status("sessionIds", 2000) == 200
    assert status("sessionIds", 2001) == 422
    assert status("studentIds", 2001) == 422
    assert client.get("/api/analytics/export", params={"studentIds": " "}).status_code == 422


def test_an_over_cap_request_answers_422_with_a_message_and_no_csv(client, monkeypatch) -> None:
    monkeypatch.setattr(assessment_service, "MAX_EXPORT_ROWS", 3)
    response = client.get("/api/analytics/export")
    assert response.status_code == 422
    assert "Narrow the filter" in response.json()["error"]
    assert CSV_BOM not in response.text
