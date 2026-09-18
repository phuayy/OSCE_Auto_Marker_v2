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
  the machine's memory, or an optional dependency group that is not installed
  here — hands the run to the default engine once, loudly, rather than failing
  a session over a machine the recording had nothing to do with. The
  "not installed" case is asked *before* the engine runs, from the same
  availability probe the settings screen reads, so a stored selection that a
  ``uv sync`` without ``--group canary`` has since made unrunnable never costs
  a job its retry budget spawning an interpreter that exits immediately.
"""
from __future__ import annotations

import logging
from dataclasses import replace
from pathlib import Path
from typing import Any

from app.core.config import Settings
from app.core.exceptions import HOST_CANNOT_RUN_ENGINE_ERRORS, AppError, TranscriptionEngineUnavailableError
from app.core.resources import ResourceLease
from app.pipeline.transcription import registry
from app.pipeline.transcription.base import (
    ProgressCallback,
    TranscriptionEngine,
    TranscriptionOptionError,
    TranscriptionRequest,
    TranscriptionResult,
)
from app.domain.actors import provenance_user_id
from app.services.event_service import EventService
from app.services.preferences_service import PreferencesService

logger = logging.getLogger(__name__)


class TranscriptionRouter:
    def __init__(
        self,
        settings: Settings,
        events: EventService,
        dependencies: registry.EngineDependencies,
        preferences: PreferencesService | None = None,
        gpu: ResourceLease | None = None,
    ) -> None:
        self.settings = settings
        self.events = events
        self.preferences = preferences
        self.engines: dict[str, TranscriptionEngine] = registry.build_all(dependencies)
        # Every engine loads a model into the accelerator (WhisperX, Canary,
        # and the pyannote pass behind either), so the lease is taken here,
        # once, around whichever engine runs — engines stay lease-unaware.
        # Falls back to the media pipeline's lease so the two GPU consumers in
        # a process (transcription, person detection) share one bound.
        self.gpu = gpu or getattr(dependencies.media, "gpu", None) or ResourceLease.unbounded("gpu")

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

    async def selection(self, user_id: str | None = None) -> tuple[str, dict[str, Any]]:
        """The engine that will run, and its option bag, live from the database
        — this account's own choice, falling back to the deployment default
        for whatever it has not personalised (``user_id=None`` reads the
        deployment default alone).

        Options are keyed per engine and picked *after* the id is resolved. That
        ordering matters: a stored id that is empty (meaning "use the deployment
        default") or names an engine this build dropped still resolves to a real
        engine, and that engine's saved tuning must come with it. Reading the bag
        against the unresolved id silently ran the default engine on defaults.
        """
        if self.preferences is None:
            return self.default_engine_id(), {}
        try:
            stored_id, options_by_engine = await self.preferences.transcription_selection(user_id)
        except Exception:
            logger.exception("Failed to read the transcription engine setting; using the default engine.")
            return self.default_engine_id(), {}

        engine_id = self.resolve_engine_id(stored_id)
        if not isinstance(options_by_engine, dict):
            return engine_id, {}
        options = options_by_engine.get(engine_id)
        return engine_id, options if isinstance(options, dict) else {}

    async def stored_options(self, engine_id: str, user_id: str | None = None) -> dict[str, Any]:
        """The saved option bag for one engine, whether or not it is selected.

        The fallback path needs this: an engine standing in for a failed one
        should still run with the tuning this account saved for it.
        """
        if self.preferences is None:
            return {}
        try:
            _, options_by_engine = await self.preferences.transcription_selection(user_id)
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

    async def describe(self, user_id: str | None = None) -> dict[str, Any]:
        """Every engine, its schema, its defaults here, and whether it can run
        — ``selected`` is the requesting account's own choice."""
        selected_id, stored_options = await self.selection(user_id)
        engines: list[dict[str, Any]] = []
        for engine in self.engines.values():
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
        owner_id = provenance_user_id(session)
        engine_id, stored_options = await self.selection(owner_id)
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
        # Asked before the lease is taken: the probe loads no model, and an
        # engine that is not installed here would otherwise be discovered by
        # spawning its subprocess, which exits at once with a generic failure
        # the queue retries. The settings screen shows the same answer.
        unavailable = await self._unavailable_error(engine)
        # Held for the whole engine run, fallback included: the fallback engine
        # is a second model load on the same card, and releasing between the
        # two would let another job slip in and OOM both.
        async with self.gpu.hold("Transcription", session_id):
            if unavailable is not None:
                result = await self._transcribe_with_fallback_engine(engine_id, request, unavailable, owner_id)
            else:
                try:
                    result = await engine.transcribe(request)
                except HOST_CANNOT_RUN_ENGINE_ERRORS as error:
                    result = await self._transcribe_with_fallback_engine(engine_id, request, error, owner_id)
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

    async def _unavailable_error(self, engine: TranscriptionEngine) -> TranscriptionEngineUnavailableError | None:
        """The typed, non-retryable failure for an engine this deployment lacks,
        or ``None`` when the engine reports it can run."""
        availability = await engine.availability()
        if availability.available:
            return None
        descriptor = engine.descriptor
        requirements = str(descriptor.requirements or "").strip()
        return TranscriptionEngineUnavailableError(
            f"{descriptor.label} ({descriptor.id}) cannot run in this deployment: {availability.reason} "
            + (f"{requirements} " if requirements else "")
            + "Install it, or select another engine in Settings."
        )

    async def _transcribe_with_fallback_engine(
        self,
        failed_engine_id: str,
        request: TranscriptionRequest,
        error: AppError,
        user_id: str | None = None,
    ) -> TranscriptionResult:
        """Run the default engine when the selected one cannot run on this host.

        Neither failure this handles says anything about the recording: the
        machine is too small for that model, or the model's toolkit is not
        installed here, and both will be just as true on every retry. The
        deployment default (WhisperX) is a far smaller model with no optional
        dependencies, so trying it turns a dead session into a transcript
        instead of an error the operator only sees the next morning.

        Deliberately narrow: only the error classes in
        ``HOST_CANNOT_RUN_ENGINE_ERRORS``, only the default engine, only when
        that engine reports itself runnable, and never a second hop. The
        substitution is announced in the run log and recorded on the step
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
                "Engine '%s' cannot run here and the fallback '%s' is unavailable: %s",
                failed_engine_id,
                fallback_id,
                availability.reason,
            )
            raise error

        logger.warning(
            "Engine '%s' cannot run here (%s); transcribing with '%s' instead.",
            failed_engine_id,
            error.message,
            fallback_id,
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
            request, options=self.safe_options(fallback, await self.stored_options(fallback_id, user_id))
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
