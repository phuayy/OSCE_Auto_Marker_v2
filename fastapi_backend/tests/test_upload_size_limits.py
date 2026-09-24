"""F3: the declared-size caps on the two files an upload carries.

``/api/uploads/initiate`` takes the client's *word* for how big each file is,
and that word is the only bound either storage backend ever applies to the
transfer itself: the local one holds received bytes to the declaration
(``storage/local.py``'s cumulative guard) and GCS opens a resumable session of
exactly that length. After initiate both backends only check "actual ==
declared", so an unbounded declaration is an unbounded write and the
declaration is the one place a cap can land for both.

The video slot was always capped. The caseStudy slot was not — the setting
existed, ``.env.example`` advertised it, and nothing read it, so any
authenticated marker could declare an arbitrarily large PDF and then write it.
These tests pin both slots, and pin the admin rubric upload to the same cap so
that "how big may a PDF be" keeps exactly one answer.

Note that ``sizeBytes`` is a *declaration*: an over-cap case costs no bytes and
no disk, which is precisely why it has to be rejected before any are accepted.
"""

from __future__ import annotations

import asyncio
import io
from pathlib import Path

import pytest
from fastapi import UploadFile

from app.core.config import Settings
from app.services.artifact_service import ArtifactService
from tests.test_routes import build_test_client

MEGABYTE = 1024 * 1024


def _initiate(client, *, video_bytes: int, case_study_bytes: int):
    token = client.post("/api/auth/login", json={"username": "admin", "password": "admin"}).json()["token"]
    return client.post(
        "/api/uploads/initiate",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "workflow": "standard",
            "autoProcess": False,
            "files": [
                {
                    "kind": "video",
                    "originalName": "station.mp4",
                    "mimeType": "video/mp4",
                    "sizeBytes": video_bytes,
                },
                {
                    "kind": "caseStudy",
                    "originalName": "case.pdf",
                    "mimeType": "application/pdf",
                    "sizeBytes": case_study_bytes,
                },
            ],
        },
    )


def test_case_study_declaration_over_the_cap_is_refused(tmp_path) -> None:
    client = build_test_client(tmp_path, max_pdf_upload_mb=2)
    response = _initiate(client, video_bytes=1024, case_study_bytes=3 * MEGABYTE)
    assert response.status_code == 413, response.text
    # The message names the configured number, not a literal baked into the
    # check — the bug being regressed was a cap that existed only in config.
    # Asserted against the raw body: routes convert through `http_error`, so
    # the envelope key depends on which handler the app registers, and this
    # test is about the message, not that shape.
    assert "2 MB" in response.text


def test_case_study_declaration_within_the_cap_is_accepted(tmp_path) -> None:
    """The cap must not be so eager that a legitimate PDF is refused — an
    exactly-at-the-limit declaration is still a valid upload."""
    client = build_test_client(tmp_path, max_pdf_upload_mb=2)
    response = _initiate(client, video_bytes=1024, case_study_bytes=2 * MEGABYTE)
    assert response.status_code == 201, response.text


def test_video_declaration_over_the_cap_is_refused(tmp_path) -> None:
    """The sibling check, which was already correct and previously untested —
    so a refactor of this branch cannot quietly drop it the way the caseStudy
    branch never had it."""
    client = build_test_client(tmp_path, max_video_upload_mb=1)
    response = _initiate(client, video_bytes=2 * MEGABYTE, case_study_bytes=1024)
    assert response.status_code == 413, response.text
    assert "1 MB" in response.text


def test_the_two_caps_are_independent(tmp_path) -> None:
    """A generous video allowance must not silently widen the PDF slot: these
    bound different resources (the media disk, the worker's PDF cache)."""
    client = build_test_client(tmp_path, max_video_upload_mb=4096, max_pdf_upload_mb=2)
    response = _initiate(client, video_bytes=8 * MEGABYTE, case_study_bytes=64 * MEGABYTE)
    assert response.status_code == 413, response.text
    assert "caseStudy" in response.json()["error"]


def test_pdf_cap_derives_from_the_configured_megabytes() -> None:
    settings = Settings(
        ffmpeg_bin="ffmpeg", ffprobe_bin="ffprobe", scorer_python_bin="python", max_pdf_upload_mb=7
    )
    assert settings.max_pdf_upload_bytes == 7 * MEGABYTE
    # Nonsense must not disable the cap outright (the floor mirrors
    # max_video_upload_bytes).
    zeroed = Settings(
        ffmpeg_bin="ffmpeg", ffprobe_bin="ffprobe", scorer_python_bin="python", max_pdf_upload_mb=0
    )
    assert zeroed.max_pdf_upload_bytes == MEGABYTE


def _artifacts(tmp_path: Path, *, max_pdf_upload_mb: int) -> ArtifactService:
    return ArtifactService(
        Settings(
            root_dir=tmp_path,
            backend_root=tmp_path,
            ffmpeg_bin="ffmpeg",
            ffprobe_bin="ffprobe",
            scorer_python_bin="python",
            max_pdf_upload_mb=max_pdf_upload_mb,
        )
    )


def _pdf_upload(size_bytes: int) -> UploadFile:
    return UploadFile(filename="communication-rubric.pdf", file=io.BytesIO(b"%PDF-1.4" + b"x" * size_bytes))


def test_admin_rubric_upload_reads_the_same_pdf_cap(tmp_path) -> None:
    """The rubric PDF used to carry its own hardcoded 10 MB. One knob for both
    paths is the point: an operator who sizes the PDF allowance sizes every
    PDF this deployment accepts."""
    artifacts = _artifacts(tmp_path, max_pdf_upload_mb=1)
    with pytest.raises(ValueError, match="1 MB"):
        asyncio.run(artifacts.save_rubric_upload(_pdf_upload(2 * MEGABYTE)))


def test_a_refused_rubric_upload_leaves_nothing_behind(tmp_path) -> None:
    """The cap is enforced while streaming, so it necessarily trips partway
    through writing; the partial file must not survive as disk the cap was
    supposed to deny."""
    artifacts = _artifacts(tmp_path, max_pdf_upload_mb=1)
    with pytest.raises(ValueError):
        asyncio.run(artifacts.save_rubric_upload(_pdf_upload(4 * MEGABYTE)))
    rubrics_dir = artifacts.settings.paths.input_rubrics_dir
    leftovers = list(rubrics_dir.glob("*")) if rubrics_dir.is_dir() else []
    assert leftovers == []


def test_a_rubric_upload_within_the_cap_is_written(tmp_path) -> None:
    artifacts = _artifacts(tmp_path, max_pdf_upload_mb=4)
    saved = asyncio.run(artifacts.save_rubric_upload(_pdf_upload(MEGABYTE)))
    assert saved.is_file()
    assert saved.stat().st_size == MEGABYTE + len(b"%PDF-1.4")
