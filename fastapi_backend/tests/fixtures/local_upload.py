"""Shared plumbing for tests that push a real upload through the local backend."""

from __future__ import annotations

import asyncio
from pathlib import Path

from app.schemas.uploads import InitiateUploadRequest

VIDEO = b"v" * 64
PDF = b"%PDF-1.7\n1 0 obj\n<<>>\nendobj\n"


async def local_upload(container, case_study: bytes, *, case_name: str = "case.pdf") -> tuple[str, str]:
    initiated = await container.async_uploads.initiate(
        InitiateUploadRequest(
            files=[
                {"kind": "video", "originalName": "station.mp4", "mimeType": "video/mp4", "sizeBytes": len(VIDEO)},
                {"kind": "caseStudy", "originalName": case_name, "mimeType": "application/pdf",
                 "sizeBytes": len(case_study)},
            ],
            autoProcess=True,
        )
    )
    upload_id = initiated["uploadId"]
    upload = await container.async_uploads.repository.read(upload_id)
    for record, body in zip(sorted(upload["files"], key=lambda r: r["kind"] != "video"), (VIDEO, case_study)):
        await container.async_uploads.put_part(upload_id, record["fileId"], 1, body)
    return upload_id, initiated["session"]["id"]


async def settled(container, session_id: str) -> dict:
    for _ in range(300):
        session = await container.sessions.read(session_id)
        if session["status"] in {"uploaded", "queued", "failed"}:
            return session
        await asyncio.sleep(0.02)
    raise AssertionError("upload never settled")


def stub_ffprobe(container, *, fail: bool = False) -> None:
    async def duration(_path):
        if fail:
            raise RuntimeError("ffprobe could not parse the file")
        return 12.0

    container.media.get_video_duration_seconds = duration


def committed_files(container) -> list[Path]:
    root = container.settings.object_storage_root
    return [p for p in root.rglob("*") if p.is_file()]
