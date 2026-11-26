"""Audit M29: a case study that is not a PDF is refused at assembly, before any job."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.core.exceptions import AppError
from app.schemas.uploads import CompleteUploadRequest
from app.services.artifact_service import ensure_pdf_file
from app.services.container import create_container
from tests.test_session_maintenance import _init, _settings
from tests.fixtures.local_upload import PDF, committed_files, local_upload, settled, stub_ffprobe


# --- M29: the validator itself ------------------------------------------------


@pytest.mark.parametrize("name", ["case.pdf", "CASE.PDF"])
def test_a_real_pdf_passes_whatever_the_name_case(tmp_path: Path, name: str) -> None:
    path = tmp_path / name
    path.write_bytes(PDF)
    ensure_pdf_file(path)


def test_garbage_named_pdf_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "case.pdf"
    path.write_bytes(b"<html>definitely not a pdf</html>")
    with pytest.raises(AppError) as caught:
        ensure_pdf_file(path)
    assert caught.value.status_code == 400
    assert "PDF" in str(caught.value)


def test_an_empty_file_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "case.pdf"
    path.write_bytes(b"")
    with pytest.raises(AppError) as caught:
        ensure_pdf_file(path)
    assert "empty" in str(caught.value).lower()


def test_the_header_may_follow_a_short_preamble(tmp_path: Path) -> None:
    path = tmp_path / "case.pdf"
    path.write_bytes(b"\n\n  junk\n" + PDF)
    ensure_pdf_file(path)


def test_a_garbage_case_study_is_refused_before_any_job_and_leaves_no_object(tmp_path) -> None:
    async def _run() -> None:
        container = create_container(_settings(tmp_path))
        await _init(container)
        stub_ffprobe(container)
        upload_id, session_id = await local_upload(container, b"this is plain text, not a pdf")
        await container.async_uploads.complete(upload_id, CompleteUploadRequest())
        session = await settled(container, session_id)

        assert session["status"] == "failed"
        assert "PDF" in session["error"]
        # Jobs are reserved at initiate; none may have been released to run.
        statuses = {job["status"] for job in await container.jobs.list_jobs(session_id)}
        assert statuses <= {"waiting_for_upload"}
        await container.shutdown()

    asyncio.run(_run())


def test_a_real_pdf_with_an_uppercase_name_is_accepted_and_kept(tmp_path) -> None:
    async def _run() -> None:
        container = create_container(_settings(tmp_path))
        await _init(container)
        stub_ffprobe(container)
        upload_id, session_id = await local_upload(container, PDF, case_name="CASE.PDF")
        await container.async_uploads.complete(upload_id, CompleteUploadRequest())
        session = await settled(container, session_id)

        assert session["status"] == "queued", session.get("error")
        assert len(committed_files(container)) == 2
        await container.shutdown()

    asyncio.run(_run())


