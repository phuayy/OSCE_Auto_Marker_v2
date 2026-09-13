"""LLM transcript preprocessing (cleanup) between transcription and scoring.

The Nemotron call runs as a subprocess (scripts/nemotron_transcript_preprocessor.py,
same convention as the scorers). The script only ever sees a minimal
[{id, speaker, text}] projection and returns corrected {id, text} pairs, so
timestamps, speaker labels, and segmentation are structurally immutable — this
module owns the merge/validation that applies the corrected texts by id.
"""
from __future__ import annotations

import asyncio
import logging
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from app.core.config import Settings
from app.core.json_utils import extract_json_object
from app.core.process import CommandRunner
from app.services.auth_service import AuthService
from app.services.event_service import EventService


logger = logging.getLogger(__name__)

LLM_PREPROCESS_SCHEMA = "llm-preprocess-v1"


def merge_corrected_segments(
    transcript: dict[str, Any],
    corrected_segments: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Apply LLM-corrected texts onto the transcript's segments in place.

    Only `text` can change. Segments match strictly by id: a segment the LLM
    dropped keeps its original text, ids the LLM invented are ignored, and an
    empty corrected text is treated as "no change". Returns the substitution
    log in the same {segmentId, original, corrected} shape corpus corrections
    use. Raises ValueError when the payload contains nothing usable, so the
    caller can mark the step failed instead of silently applying nothing.
    """
    corrected_by_id: dict[str, str] = {}
    for item in corrected_segments or []:
        if isinstance(item, dict) and item.get("id") is not None:
            corrected_by_id[str(item["id"])] = str(item.get("text") or "")
    if not corrected_by_id:
        # A transcript with no speech legitimately yields an empty reply; an
        # empty reply for a transcript WITH text is a failed preprocess.
        has_text = any(str(seg.get("text") or "").strip() for seg in transcript.get("segments") or [])
        if has_text:
            raise ValueError("LLM preprocess returned no usable segments.")
        return []

    changes: list[dict[str, Any]] = []
    for segment in transcript.get("segments") or []:
        original = str(segment.get("text") or "")
        corrected = corrected_by_id.get(str(segment.get("id")))
        if corrected is None:
            continue
        corrected = corrected.strip()
        if not corrected or corrected == original:
            continue
        segment["text"] = corrected
        changes.append({"segmentId": segment.get("id"), "original": original, "corrected": corrected})
    return changes


def diff_replacements(original: str, corrected: str) -> list[dict[str, str]]:
    """Word-level substitution pairs between an original and corrected segment
    text, in the shape apply_replacements_to_file consumes for SRT/VTT
    patching. Pure insertions/deletions have no reliable subtitle anchor and
    are skipped; sub-4-char originals are filtered downstream anyway."""
    original_words = original.split()
    corrected_words = corrected.split()
    matcher = SequenceMatcher(None, original_words, corrected_words, autojunk=False)
    replacements: list[dict[str, str]] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag != "replace":
            continue
        replacements.append(
            {
                "original": " ".join(original_words[i1:i2]),
                "corrected": " ".join(corrected_words[j1:j2]),
            }
        )
    return replacements


class TranscriptPreprocessor:
    """Subprocess wrapper for the Nemotron transcript preprocessor script."""

    def __init__(
        self,
        settings: Settings,
        runner: CommandRunner,
        events: EventService,
        auth: AuthService,
        llm_settings: Any | None = None,
    ) -> None:
        self.settings = settings
        self.runner = runner
        self.events = events
        self.auth = auth
        # Same routing the scorers get, so a model chosen in Settings applies to
        # the cleanup pass too rather than only to marking.
        self.llm_settings = llm_settings

    def python_env(self) -> dict[str, str]:
        env = {"PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"}
        if self.auth.runtime.nvidia_api_key:
            env["NVIDIA_API_KEY"] = self.auth.runtime.nvidia_api_key
        return env

    async def preprocess_env(self) -> dict[str, str]:
        env = self.python_env()
        if self.llm_settings is None:
            return env
        try:
            env.update(await self.llm_settings.subprocess_env())
        except Exception:
            logger.exception(
                "Failed to resolve LLM routing for transcript preprocess; using environment defaults."
            )
        return env

    async def run(self, session: dict[str, Any], transcript_path: Path) -> dict[str, Any]:
        if not self.settings.llm_preprocess_script_path.exists():
            raise RuntimeError(
                f"Transcript preprocessor script not found at {self.settings.llm_preprocess_script_path}"
            )
        output_path = self.settings.paths.output_llm_preprocess_dir / f"{session['id']}.json"
        args = [
            str(self.settings.llm_preprocess_script_path),
            "--transcript",
            str(transcript_path),
            "--output",
            str(output_path),
        ]
        await self.events.publish(
            str(session["id"]),
            "log",
            {"source": "llm-preprocess", "message": f"{self.settings.scorer_python_bin} {' '.join(args)}"},
        )
        await self.runner.run(
            self.settings.scorer_python_bin,
            args,
            "LLM transcript preprocess",
            env=await self.preprocess_env(),
            on_output=self.events.log_sink(str(session["id"]), "llm-preprocess"),
        )
        if not output_path.exists():
            raise RuntimeError("Transcript preprocessor did not produce an output file.")
        payload = await asyncio.to_thread(lambda: extract_json_object(output_path.read_text(encoding="utf-8")))
        if str(payload.get("schema") or "") != LLM_PREPROCESS_SCHEMA:
            raise RuntimeError("Transcript preprocessor output had an unexpected schema.")
        return payload
