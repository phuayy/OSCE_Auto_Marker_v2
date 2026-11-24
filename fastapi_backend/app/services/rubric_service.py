from __future__ import annotations

import asyncio
import logging
import re
import shutil
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastapi import UploadFile

from app.core.config import Settings
from app.core.json_utils import read_json_file, write_json_file
from app.core.process import CommandRunner
from app.domain.communication_rubric import edited_provenance, rubric_fingerprint, validate_edited_rubric
from app.services.artifact_service import ArtifactService
from app.services.rubric_asset_service import RubricAssetService


if TYPE_CHECKING:
    from app.services.llm_settings_service import LLMSettingsService


logger = logging.getLogger(__name__)

DRAFT_TTL_SECONDS = 86400
_DRAFT_ID = re.compile(r"[0-9a-f]{32}")
_DRAFT_MODES = {"llm", "auto"}


class RubricParseError(RuntimeError):
    """The parser could not turn an uploaded PDF into a usable rubric.

    The message is the parser's own reason; the route reports it as a 422 so
    the admin learns what is wrong with *their file*, not that the server broke.
    """


class RubricDraftNotFound(LookupError):
    """No such draft: never existed, was discarded, expired, or the id is malformed."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class RubricService:
    def __init__(
        self,
        settings: Settings,
        runner: CommandRunner,
        artifacts: ArtifactService,
        rubric_assets: RubricAssetService,
        llm_settings: LLMSettingsService | None = None,
    ) -> None:
        self.settings = settings
        self.runner = runner
        self.artifacts = artifacts
        self.rubric_assets = rubric_assets
        # The parser's LLM fallback needs the deployment's routing. The container
        # builds this service before LLMSettingsService, so it may be attached
        # after construction; None means the parser runs with no LLM.
        self.llm_settings = llm_settings
        # One deployment, rare operations: every mutation of the active rubric
        # and every draft creation/activation takes this, so two admins cannot
        # interleave a swap of the PDF and JSON.
        self._lock = asyncio.Lock()

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

    async def parser_env(self) -> dict[str, str]:
        """Base env (carries ``RUBRIC_LLM_FALLBACK``) plus the deployment's LLM
        routing. The communication rubric is deployment-wide, so the routing is
        the default one, not any account's. A failure to resolve it is logged
        and skipped: the parser then simply has no LLM to fall back to."""
        env = self.settings.subprocess_env()
        if self.llm_settings is not None:
            try:
                env.update(await self.llm_settings.subprocess_env(None))
            except Exception:
                logger.exception("Failed to resolve LLM routing; the rubric parser will have no LLM fallback.")
        return env

    async def run_parser(
        self, pdf_path: Path, output_path: Path | None = None, *, force_llm: bool = False
    ) -> dict[str, Any] | None:
        if not self.settings.rubric_parser_script_path.exists():
            raise RuntimeError(f"Rubric parser script not found at {self.settings.rubric_parser_script_path}")
        if not pdf_path.exists():
            raise RuntimeError(f"Rubric PDF not found at {pdf_path}")
        self.settings.paths.auth_dir.mkdir(parents=True, exist_ok=True)
        target = output_path or self.settings.paths.communication_rubric_json_path
        args = [
            str(self.settings.rubric_parser_script_path),
            "--pdf",
            str(pdf_path),
            "--output",
            str(target),
        ]
        if force_llm:
            args.append("--force-llm")
        await self.runner.run(
            self.settings.scorer_python_bin,
            args,
            "Communication rubric parser",
            env=await self.parser_env(),
        )
        return await asyncio.to_thread(read_json_file, target)

    async def parse_to_payload(self, pdf_path: Path, *, force_llm: bool = False) -> dict[str, Any]:
        """Parse ``pdf_path`` without touching the active rubric.

        The parser writes to a temporary file that is read and then removed, so
        a failure (or a success the caller later abandons) leaves the current
        JSON exactly as it was. Raises ``RubricParseError`` with the parser's
        reason.
        """
        temp_path = self.settings.paths.communication_rubric_json_path.with_name(
            f".communication_rubric.{uuid.uuid4().hex}.tmp"
        )
        try:
            try:
                parsed = await self.run_parser(pdf_path, temp_path, force_llm=force_llm)
            except RuntimeError as error:
                raise RubricParseError(_parser_reason(str(error))) from error
            if not parsed:
                raise RubricParseError("Rubric parsed but JSON output could not be loaded. Check server logs.")
            return parsed
        finally:
            await self.artifacts.unlink_if_exists(temp_path)

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
            parsed = await self.parse_to_payload(source_pdf)
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
        async with self._lock:
            return await self._upload_locked(upload)

    async def _upload_locked(self, upload: UploadFile) -> dict[str, Any]:
        self.artifacts.validate_pdf_upload(
            upload,
            field_name="rubric",
            message="Only PDF files are accepted for the communication rubric.",
        )
        uploaded_path = await self.artifacts.save_rubric_upload(upload)
        # Parse first, into a temporary file: a PDF the parser rejects must not
        # replace the active PDF (leaving the old JSON paired with a new PDF).
        try:
            parsed = await self.parse_to_payload(uploaded_path)
        except Exception:
            await self.artifacts.unlink_if_exists(uploaded_path)
            raise
        registration = await self._activate_pdf(
            uploaded_path, original_name=upload.filename or uploaded_path.name, mime_type=upload.content_type
        )
        parsed["source_pdf"] = str(registration.asset["absolutePath"])
        # Atomic write: the new JSON replaces the old in one step.
        parsed = await self._annotate_parsed(parsed, registration.asset)
        pdf_meta = await self.read_pdf_meta()
        if pdf_meta:
            pdf_meta["rubricAssetId"] = registration.asset["id"]
            pdf_meta["contentSha256"] = registration.asset["contentSha256"]
            pdf_meta["deduplicated"] = registration.is_duplicate
        return {"rubric": parsed, "pdf": pdf_meta}

    async def _activate_pdf(self, path: Path, *, original_name: str, mime_type: str | None) -> Any:
        """Register ``path`` (a file under ``input_rubrics_dir`` this service
        owns) as the communication rubric asset and make it the active PDF.
        A duplicate of an already-registered file is dropped in favour of the
        registered copy. Returns the registration."""
        registration = await self.rubric_assets.register_communication_rubric(
            path=path, original_name=original_name, mime_type=mime_type
        )
        source_path = Path(str(registration.asset["absolutePath"]))
        if registration.is_duplicate and source_path != path:
            await self.artifacts.unlink_if_exists(path)
        await self.artifacts.copy_file(source_path, self.settings.paths.communication_rubric_pdf_path)
        return registration

    async def reset(self) -> dict[str, Any]:
        async with self._lock:
            return await self._reset_locked()

    async def _reset_locked(self) -> dict[str, Any]:
        default_pdf = self.settings.paths.default_rubric_source_pdf
        if not default_pdf.exists():
            raise FileNotFoundError("Default communication rubric PDF is missing on this installation.")
        # Parse before deleting anything, so a failure keeps the current rubric.
        parsed = await self.parse_to_payload(default_pdf)
        await self.artifacts.unlink_if_exists(self.settings.paths.communication_rubric_pdf_path)
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
        next_payload["rubric_revision"] = rubric_fingerprint(next_payload)
        # write_json_file is atomic (temp file + replace).
        await asyncio.to_thread(write_json_file, self.settings.paths.communication_rubric_json_path, next_payload)
        return next_payload

    # --- drafts ---------------------------------------------------------------

    @property
    def drafts_dir(self) -> Path:
        return self.settings.paths.auth_dir / "communication_rubric_drafts"

    def _draft_dir(self, draft_id: Any) -> Path:
        if not isinstance(draft_id, str) or not _DRAFT_ID.fullmatch(draft_id):
            raise RubricDraftNotFound("That extraction has expired or was discarded.")
        return self.drafts_dir / draft_id

    def _load_draft(self, draft_id: Any) -> tuple[Path, dict[str, Any]]:
        directory = self._draft_dir(draft_id)
        document = read_json_file(directory / "draft.json") if directory.is_dir() else None
        if not isinstance(document, dict) or not (directory / "source.pdf").is_file():
            raise RubricDraftNotFound("That extraction has expired or was discarded.")
        age = time.time() - (directory / "draft.json").stat().st_mtime
        if age > DRAFT_TTL_SECONDS:
            shutil.rmtree(directory, ignore_errors=True)
            raise RubricDraftNotFound("That extraction has expired or was discarded.")
        return directory, document

    def _sweep_drafts(self) -> None:
        root = self.drafts_dir
        if not root.is_dir():
            return
        cutoff = time.time() - DRAFT_TTL_SECONDS
        for entry in root.iterdir():
            try:
                if not entry.is_dir() or not _DRAFT_ID.fullmatch(entry.name):
                    continue
                marker = entry / "draft.json"
                stamp = marker.stat().st_mtime if marker.exists() else entry.stat().st_mtime
                if stamp < cutoff:
                    shutil.rmtree(entry, ignore_errors=True)
            except Exception:
                logger.exception("Failed to sweep rubric draft %s.", entry)

    async def extract_draft(
        self, upload: UploadFile, *, mode: str = "llm", actor: Any = None
    ) -> dict[str, Any]:
        """Parse an uploaded PDF into a draft for review. The active rubric
        (JSON and PDF) is never touched; ``save_edited(draft_id=...)`` activates it."""
        if mode not in _DRAFT_MODES:
            raise ValueError('mode must be "llm" or "auto".')
        self.artifacts.validate_pdf_upload(
            upload,
            field_name="rubric",
            message="Only PDF files are accepted for the communication rubric.",
        )
        # Only the sweep takes the lock: a draft is private to its own
        # directory, so the (possibly minute-long) AI extraction below must not
        # hold up a save or an upload of the active rubric.
        async with self._lock:
            try:
                await asyncio.to_thread(self._sweep_drafts)
            except Exception:
                logger.exception("Rubric draft sweep failed.")
        uploaded_path = await self.artifacts.save_rubric_upload(upload)
        draft_id = uuid.uuid4().hex
        directory = self._draft_dir(draft_id)
        try:
            directory.mkdir(parents=True, exist_ok=True)
            await asyncio.to_thread(shutil.move, str(uploaded_path), str(directory / "source.pdf"))
            parsed = await self.parse_to_payload(directory / "source.pdf", force_llm=(mode == "llm"))
            created_at = _now_iso()
            document = {
                "draftId": draft_id,
                "createdAt": created_at,
                "createdBy": actor.to_provenance() if actor is not None else None,
                "originalName": upload.filename or "communication-rubric.pdf",
                "mimeType": upload.content_type or "application/pdf",
                "rubric": parsed,
            }
            await asyncio.to_thread(write_json_file, directory / "draft.json", document)
        except BaseException:
            await asyncio.to_thread(shutil.rmtree, directory, True)
            await self.artifacts.unlink_if_exists(uploaded_path)
            raise
        return {
            "draft": {"draftId": draft_id, "createdAt": created_at, "originalName": document["originalName"]},
            "rubric": parsed,
        }

    async def discard_draft(self, draft_id: str) -> None:
        async with self._lock:
            directory = self._draft_dir(draft_id)
            if not directory.is_dir():
                raise RubricDraftNotFound("That extraction has expired or was discarded.")
            await asyncio.to_thread(shutil.rmtree, directory, True)

    # --- editing --------------------------------------------------------------

    async def save_edited(
        self, rubric_input: dict[str, Any], *, draft_id: str | None = None, actor: Any = None
    ) -> dict[str, Any]:
        payload = validate_edited_rubric(rubric_input)
        async with self._lock:
            if draft_id is not None:
                directory, draft = await asyncio.to_thread(self._load_draft, draft_id)
                previous = (draft.get("rubric") or {}).get("extraction")
                # Register a copy that outlives the draft directory.
                staged = self.settings.paths.input_rubrics_dir / f"{int(time.time() * 1000)}-{uuid.uuid4().hex[:8]}.pdf"
                await self.artifacts.copy_file(directory / "source.pdf", staged)
                try:
                    registration = await self._activate_pdf(
                        staged,
                        original_name=str(draft.get("originalName") or "communication-rubric.pdf"),
                        mime_type=draft.get("mimeType"),
                    )
                except BaseException:
                    await self.artifacts.unlink_if_exists(staged)
                    raise
                payload["rubric_asset_id"] = registration.asset["id"]
                payload["rubric_content_sha256"] = registration.asset["contentSha256"]
                payload["source_pdf"] = str(registration.asset["absolutePath"])
            else:
                current = await self.read_json() or await self.ensure_parsed()
                if not current:
                    raise RubricParseError("There is no active communication rubric to edit.")
                previous = current.get("extraction")
                for key in ("rubric_asset_id", "rubric_content_sha256", "source_pdf"):
                    if key in current:
                        payload[key] = current[key]
            payload["extraction"] = edited_provenance(
                previous,
                edited_by=actor.to_provenance() if actor is not None else None,
                edited_at=_now_iso(),
            )
            payload["rubric_revision"] = rubric_fingerprint(payload)
            await asyncio.to_thread(write_json_file, self.settings.paths.communication_rubric_json_path, payload)
            if draft_id is not None:
                await asyncio.to_thread(shutil.rmtree, directory, True)
        return await self.current()


def _parser_reason(message: str) -> str:
    """The parser prints ``Error: <reason>``; the runner wraps it with an exit
    code and the whole stderr. Keep just the reason for an admin to read."""
    matches = re.findall(r"^Error:\s*(.+)$", message, flags=re.MULTILINE)
    return matches[-1].strip() if matches else message
