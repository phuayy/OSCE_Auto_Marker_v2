"""Chooses and runs the transcription engine for a session.

The pipeline asks this service for a transcript; it decides *which* engine
produces one. The selection is a global setting read live from the database on
every run — the same mechanism the LLM preprocess toggle uses — so a change in
the settings screen applies to the next run in every process, including clip
children and the Hatchet worker, with no restart.

Two rules keep a bad selection from costing a run:

* a stored selection naming an engine this build does not ship falls back to
  the default engine and says so in the run log;
* stored options that no longer validate are dropped rather than failing the
  run — the engine's own defaults are always a valid configuration;
* an engine that cannot run on this host at all — a checkpoint too large for
  the machine's memory — hands the run to the default engine once, loudly,
  rather than failing a session over a hardware limit the recording had
  nothing to do with.
"""
from __future__ import annotations

import logging
from dataclasses import replace
from pathlib import Path
from typing import Any

from app.core.config import Settings
from app.core.exceptions import TranscriptionResourceError
from app.pipeline.transcription import registry
from app.pipeline.transcription.base import (
    ProgressCallback,
    TranscriptionEngine,
    TranscriptionOptionError,
    TranscriptionRequest,
    TranscriptionResult,
)
from app.repositories.app_settings_repository import AppSettingsRepository
from app.services.event_service import EventService

logger = logging.getLogger(__name__)


class TranscriptionRouter:
    def __init__(
        self,
        settings: Settings,
        events: EventService,
        dependencies: registry.EngineDependencies,
        app_settings: AppSettingsRepository | None = None,
    ) -> None:
        self.settings = settings
        self.events = events
        self.app_settings = app_settings
        self.engines: dict[str, TranscriptionEngine] = registry.build_all(dependencies)

    # --- selection ---------------------------------------------------------

    def default_engine_id(self) -> str:
        """The configured fallback, validated against what this build ships."""
        configured = str(self.settings.transcription_engine or "").strip()
        return configured if configured in self.engines else registry.DEFAULT_ENGINE_ID

    def resolve_engine_id(self, requested: Any) -> str:
        requested_id = str(requested or "").strip()
        if requested_id in self.engines:
            return requested_id
        if requested_id:
            logger.warning(
                "Unknown transcription engine '%s' selected; falling back to '%s'.",
                requested_id,
                self.default_engine_id(),
            )
        return self.default_engine_id()

    def engine(self, engine_id: str) -> TranscriptionEngine:
        return self.engines[self.resolve_engine_id(engine_id)]

    async def selection(self) -> tuple[str, dict[str, Any]]:
        """The engine that will run, and its option bag, live from the database.

        Options are keyed per engine and picked *after* the id is resolved. That
        ordering matters: a stored id that is empty (meaning "use the deployment
        default") or names an engine this build dropped still resolves to a real
        engine, and that engine's saved tuning must come with it. Reading the bag
        against the unresolved id silently ran the default engine on defaults.
        """
        if self.app_settings is None:
            return self.default_engine_id(), {}
        try:
            stored_id, options_by_engine = await self.app_settings.transcription_selection()
        except Exception:
            logger.exception("Failed to read the transcription engine setting; using the default engine.")
            return self.default_engine_id(), {}

        engine_id = self.resolve_engine_id(stored_id)
        if not isinstance(options_by_engine, dict):
            return engine_id, {}
        options = options_by_engine.get(engine_id)
        return engine_id, options if isinstance(options, dict) else {}

    async def stored_options(self, engine_id: str) -> dict[str, Any]:
        """The saved option bag for one engine, whether or not it is selected.

        The fallback path needs this: an engine standing in for a failed one
        should still run with the tuning the operator saved for it.
        """
        if self.app_settings is None:
            return {}
        try:
            _, options_by_engine = await self.app_settings.transcription_selection()
        except Exception:
            logger.exception("Failed to read stored options for engine '%s'; using its defaults.", engine_id)
            return {}
        if not isinstance(options_by_engine, dict):
            return {}
        options = options_by_engine.get(engine_id)
        return options if isinstance(options, dict) else {}

    def safe_options(self, engine: TranscriptionEngine, stored: dict[str, Any] | None) -> dict[str, Any]:
        """Stored overrides layered on the engine defaults, ignoring any that
        no longer validate — an option removed or narrowed in a later release
        must not strand a deployment on a transcription it cannot run."""
        try:
            return engine.resolve_options(stored)
        except TranscriptionOptionError as error:
            logger.warning(
                "Stored options for engine '%s' are invalid (%s); using engine defaults.",
                engine.descriptor.id,
                error.message,
            )
            return engine.resolve_options({})

    # --- description (settings screen) -------------------------------------

    async def describe(self) -> dict[str, Any]:
        """Every engine, its schema, its defaults here, and whether it can run."""
        selected_id, stored_options = await self.selection()
        engines: list[dict[str, Any]] = []
        for engine_id, engine in self.engines.items():
            availability = await engine.availability()
            payload = engine.descriptor.to_public()
            payload["availability"] = availability.to_public()
            payload["defaults"] = engine.default_options()
            engines.append(payload)
        return {
            "engines": engines,
            "selected": {"engineId": selected_id, "options": stored_options},
            "defaultEngineId": self.default_engine_id(),
        }

    # --- model prefetch ----------------------------------------------------

    async def prefetch_selected_engine(self) -> None:
        """Cache the selected engine's weights, logging what happened.

        Startup calls this in the background: it can take many minutes on a
        cold machine, and nothing in the API depends on its outcome — a run
        that starts before the download finishes simply downloads then. It
        never raises, so a boot on an offline host is unaffected.
        """
        if not self.settings.transcription_prefetch_models:
            return
        try:
            engine_id, _ = await self.selection()
            engine = self.engines[engine_id]
            result = await engine.prefetch()
        except Exception:
            logger.exception("Transcription model prefetch failed.")
            return
        if result.ready:
            logger.info("Transcription model ready for '%s'. %s", engine_id, result.detail)
        else:
            logger.warning(
                "Transcription model for '%s' is not cached: %s", engine_id, result.detail
            )

    # --- running -----------------------------------------------------------

    async def transcribe(
        self,
        session: dict[str, Any],
        audio_info: dict[str, Any],
        *,
        on_progress: ProgressCallback | None = None,
    ) -> TranscriptionResult:
        session_id = str(session["id"])
        engine_id, stored_options = await self.selection()
        engine = self.engines[engine_id]
        options = self.safe_options(engine, stored_options)

        request = TranscriptionRequest(
            session_id=session_id,
            audio_path=Path(str(audio_info["absolutePath"])),
            audio_file_name=str(audio_info["fileName"]),
            output_dir=self.settings.paths.output_whisperx_dir / session_id,
            language=self.settings.whisperx_language,
            options=options,
            corpus_terms=[str(term) for term in ((session.get("corpus") or {}).get("terms") or [])],
            min_speakers=self.settings.whisperx_min_speakers,
            max_speakers=self.settings.whisperx_max_speakers,
            on_progress=on_progress,
        )
        await self.events.publish(
            session_id,
            "log",
            {
                "source": "transcription",
                "message": f"Transcribing with {engine.descriptor.label} ({engine_id}).",
            },
        )
        try:
            result = await engine.transcribe(request)
        except TranscriptionResourceError as error:
            result = await self._transcribe_with_fallback_engine(engine_id, request, error)
        if not result.diarized:
            # Loud, because unlabelled dialogue changes what the scorers can
            # conclude — not a silent quality regression.
            await self.events.publish(
                session_id,
                "log",
                {
                    "source": "transcription",
                    "message": (
                        "This transcript has no speaker labels. Scoring will read the dialogue "
                        "without knowing who spoke each line."
                    ),
                },
            )
        return result

    async def _transcribe_with_fallback_engine(
        self,
        failed_engine_id: str,
        request: TranscriptionRequest,
        error: TranscriptionResourceError,
    ) -> TranscriptionResult:
        """Run the default engine when the selected one cannot run on this host.

        A resource failure says nothing about the recording — the machine is too
        small for that model, and will be just as small on every retry. The
        deployment default (WhisperX) loads a far smaller model, so trying it
        turns a dead session into a transcript instead of an error the operator
        only sees the next morning.

        Deliberately narrow: only this one error class, only the default engine,
        only when that engine reports itself runnable, and never a second hop.
        The substitution is announced in the run log and recorded on the step
        metadata, because a transcript produced by an engine nobody selected
        must never look like the selected engine's work. If nothing can stand
        in, the original failure is raised untouched.
        """
        session_id = request.session_id
        fallback_id = self.default_engine_id()
        await self.events.publish(
            session_id,
            "log",
            {"source": "transcription", "message": f"{failed_engine_id} could not run here: {error.message}"},
        )
        if fallback_id == failed_engine_id:
            raise error

        fallback = self.engines[fallback_id]
        availability = await fallback.availability()
        if not availability.available:
            logger.error(
                "Engine '%s' failed on resources and the fallback '%s' is unavailable: %s",
                failed_engine_id,
                fallback_id,
                availability.reason,
            )
            raise error

        logger.warning(
            "Engine '%s' ran out of memory; transcribing with '%s' instead.", failed_engine_id, fallback_id
        )
        await self.events.publish(
            session_id,
            "log",
            {
                "source": "transcription",
                "message": (
                    f"Falling back to {fallback.descriptor.label} ({fallback_id}) for this run. "
                    "The engine selection in Settings is unchanged."
                ),
            },
        )
        fallback_request = replace(
            request, options=self.safe_options(fallback, await self.stored_options(fallback_id))
        )
        result = await fallback.transcribe(fallback_request)
        result.metadata = {
            **result.metadata,
            "fallbackFrom": failed_engine_id,
            "fallbackReason": error.message,
        }
        return result

    def describe_result(self, result: TranscriptionResult) -> dict[str, Any]:
        """Provenance for a finished run, with the engine's display label."""
        engine = self.engines.get(result.engine_id)
        label = engine.descriptor.label if engine is not None else result.engine_id
        return self.result_metadata(result, label)

    @staticmethod
    def result_metadata(result: TranscriptionResult, engine_label: str) -> dict[str, Any]:
        """Step metadata recorded on the session, so a completed run always
        shows which engine and model produced its transcript."""
        return {
            "engineId": result.engine_id,
            "engineLabel": engine_label,
            "model": result.model,
            "diarized": result.diarized,
            **result.metadata,
        }
