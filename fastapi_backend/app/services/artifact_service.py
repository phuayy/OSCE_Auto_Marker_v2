from __future__ import annotations

import asyncio
import shutil
import time
from pathlib import Path
from uuid import uuid4

from fastapi import UploadFile

from app.core.config import Settings
from app.core.exceptions import AppError
from app.core.utils import sanitize_file_name

# A PDF reader accepts the ``%PDF-`` marker anywhere in the first kilobyte (a
# preamble some generators emit), so the content check does the same.
PDF_MAGIC = b"%PDF-"
PDF_HEADER_WINDOW_BYTES = 1024


def ensure_pdf_file(path: Path) -> None:
    """Refuse a file that is not a PDF by its *bytes*, whatever it is named.

    The upload's name and declared MIME type are the browser's claim; this is
    the one check made against what was actually stored. Raises a 400
    ``AppError`` (non-retryable, and its message is what the browser shows).
    """
    with path.open("rb") as source:
        head = source.read(PDF_HEADER_WINDOW_BYTES)
    if not head:
        raise AppError("The case study file is empty.", status_code=400)
    if PDF_MAGIC not in head:
        raise AppError(
            "The case study is not a valid PDF file. Upload the case study as a PDF.",
            status_code=400,
        )


class ArtifactService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def ensure_storage_layout(self) -> None:
        await asyncio.to_thread(self.settings.paths.ensure_layout)
        await asyncio.to_thread(self.settings.object_storage_root.mkdir, parents=True, exist_ok=True)

    def new_session_id(self) -> str:
        return str(uuid4())

    def validate_pdf_upload(self, upload: UploadFile | None, *, field_name: str, message: str | None = None) -> None:
        if upload is None:
            raise ValueError(message or f'{field_name} PDF file is required.')
        filename = str(upload.filename or "")
        is_pdf_by_mime = str(upload.content_type or "") == "application/pdf"
        is_pdf_by_name = filename.lower().endswith(".pdf")
        if not is_pdf_by_mime and not is_pdf_by_name:
            raise ValueError(message or f"The {field_name} field must contain a PDF file.")

    async def save_rubric_upload(self, upload: UploadFile) -> Path:
        safe_name = sanitize_file_name(upload.filename or "communication-rubric.pdf")
        target = self.settings.paths.input_rubrics_dir / f"{int(time.time() * 1000)}-{safe_name}"
        await self._copy_upload(upload, target, max_bytes=self.settings.max_pdf_upload_bytes)
        return target

    async def copy_file(self, source: Path, target: Path) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(shutil.copyfile, source, target)

    async def unlink_if_exists(self, path: Path) -> None:
        await asyncio.to_thread(lambda: path.unlink(missing_ok=True))

    async def _copy_upload(self, upload: UploadFile, target: Path, *, max_bytes: int) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)

        def _copy() -> None:
            upload.file.seek(0)
            copied = 0
            try:
                with target.open("wb") as buffer:
                    while True:
                        chunk = upload.file.read(1024 * 1024)
                        if not chunk:
                            break
                        copied += len(chunk)
                        if copied > max_bytes:
                            raise ValueError(f"Uploaded file exceeds the {max_bytes // (1024 * 1024)} MB limit.")
                        buffer.write(chunk)
            except Exception:
                target.unlink(missing_ok=True)
                raise

        await asyncio.to_thread(_copy)
