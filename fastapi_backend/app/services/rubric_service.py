from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from fastapi import UploadFile

from app.core.config import Settings
from app.core.json_utils import read_json_file, write_json_file
from app.core.process import CommandRunner
from app.services.artifact_service import ArtifactService
from app.services.rubric_asset_service import RubricAssetService


logger = logging.getLogger(__name__)


class RubricService:
    def __init__(
        self,
        settings: Settings,
        runner: CommandRunner,
        artifacts: ArtifactService,
        rubric_assets: RubricAssetService,
    ) -> None:
        self.settings = settings
        self.runner = runner
        self.artifacts = artifacts
        self.rubric_assets = rubric_assets

    async def read_json(self) -> dict[str, Any] | None:
        return await asyncio.to_thread(read_json_file, self.settings.paths.communication_rubric_json_path)

    async def read_pdf_meta(self) -> dict[str, Any] | None:
        def _read() -> dict[str, Any] | None:
            pdf_path = self.settings.paths.communication_rubric_pdf_path
            is_custom = True
            if not pdf_path.exists():
                if not self.settings.paths.default_rubric_source_pdf.exists():
                    return None
                pdf_path = self.settings.paths.default_rubric_source_pdf
                is_custom = False
            stats = pdf_path.stat()
            from datetime import datetime, timezone

            return {
                "absolutePath": str(pdf_path),
                "sizeBytes": stats.st_size,
                "updatedAt": datetime.fromtimestamp(stats.st_mtime, timezone.utc).isoformat().replace("+00:00", "Z"),
                "isCustomUpload": is_custom,
                "fileName": pdf_path.name,
            }

        return await asyncio.to_thread(_read)

    async def run_parser(self, pdf_path: Path) -> dict[str, Any] | None:
        if not self.settings.rubric_parser_script_path.exists():
            raise RuntimeError(f"Rubric parser script not found at {self.settings.rubric_parser_script_path}")
        if not pdf_path.exists():
            raise RuntimeError(f"Rubric PDF not found at {pdf_path}")
        self.settings.paths.auth_dir.mkdir(parents=True, exist_ok=True)
        args = [
            str(self.settings.rubric_parser_script_path),
            "--pdf",
            str(pdf_path),
            "--output",
            str(self.settings.paths.communication_rubric_json_path),
        ]
        await self.runner.run(
            self.settings.scorer_python_bin,
            args,
            "Communication rubric parser",
            env={"PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"},
        )
        return await self.read_json()

    async def ensure_parsed(self) -> dict[str, Any] | None:
        existing = await self.read_json()
        if existing:
            return existing
        source_pdf = self.settings.paths.communication_rubric_pdf_path
        if not source_pdf.exists():
            source_pdf = self.settings.paths.default_rubric_source_pdf
        if not source_pdf.exists():
            return None
        try:
            parsed = await self.run_parser(source_pdf)
            registration = await self.rubric_assets.register_communication_rubric(
                path=source_pdf,
                original_name=source_pdf.name,
                mime_type="application/pdf",
            )
            return await self._annotate_parsed(parsed, registration.asset)
        except Exception:
            logger.exception("Failed to parse or register communication rubric from %s.", source_pdf)
            return None

    async def current(self) -> dict[str, Any]:
        parsed = await self.read_json() or await self.ensure_parsed()
        return {
            "rubric": parsed,
            "pdf": await self.read_pdf_meta(),
            "defaultSourcePdf": str(self.settings.paths.default_rubric_source_pdf)
            if self.settings.paths.default_rubric_source_pdf.exists()
            else None,
        }

    async def upload(self, upload: UploadFile) -> dict[str, Any]:
        self.artifacts.validate_pdf_upload(
            upload,
            field_name="rubric",
            message="Only PDF files are accepted for the communication rubric.",
        )
        uploaded_path = await self.artifacts.save_rubric_upload(upload)
        registration = await self.rubric_assets.register_communication_rubric(
            path=uploaded_path,
            original_name=upload.filename or uploaded_path.name,
            mime_type=upload.content_type,
        )
        source_path = Path(str(registration.asset["absolutePath"]))
        if registration.is_duplicate and source_path != uploaded_path:
            await self.artifacts.unlink_if_exists(uploaded_path)
        await self.artifacts.copy_file(source_path, self.settings.paths.communication_rubric_pdf_path)
        parsed = await self.run_parser(source_path)
        if not parsed:
            raise RuntimeError("Rubric parsed but JSON output could not be loaded. Check server logs.")
        parsed = await self._annotate_parsed(parsed, registration.asset)
        pdf_meta = await self.read_pdf_meta()
        if pdf_meta:
            pdf_meta["rubricAssetId"] = registration.asset["id"]
            pdf_meta["contentSha256"] = registration.asset["contentSha256"]
            pdf_meta["deduplicated"] = registration.is_duplicate
        return {"rubric": parsed, "pdf": pdf_meta}

    async def reset(self) -> dict[str, Any]:
        default_pdf = self.settings.paths.default_rubric_source_pdf
        if not default_pdf.exists():
            raise FileNotFoundError("Default communication rubric PDF is missing on this installation.")
        await self.artifacts.unlink_if_exists(self.settings.paths.communication_rubric_pdf_path)
        await self.artifacts.unlink_if_exists(self.settings.paths.communication_rubric_json_path)
        parsed = await self.run_parser(default_pdf)
        registration = await self.rubric_assets.register_communication_rubric(
            path=default_pdf,
            original_name=default_pdf.name,
            mime_type="application/pdf",
        )
        parsed = await self._annotate_parsed(parsed, registration.asset)
        return {"rubric": parsed, "pdf": await self.read_pdf_meta()}

    async def _annotate_parsed(self, parsed: dict[str, Any] | None, asset: dict[str, Any]) -> dict[str, Any] | None:
        if not parsed:
            return parsed
        next_payload = dict(parsed)
        next_payload["rubric_asset_id"] = asset["id"]
        next_payload["rubric_content_sha256"] = asset["contentSha256"]
        await asyncio.to_thread(write_json_file, self.settings.paths.communication_rubric_json_path, next_payload)
        return next_payload
