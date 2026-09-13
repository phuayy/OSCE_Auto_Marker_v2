"""Panel marking: several markers in parallel, one adjudicator for the split.

Each marker is the same assessor subprocess single mode runs, pointed at its
own model and its own output file under ``scores/panel/<session>/``. When
they are done the adjudicator script reconciles the sheets into
``scores/<session>.json`` — the path single mode writes, so nothing downstream
knows which mode produced the sheet unless it looks at the ``panel`` block.

Two properties this strategy keeps:

* **A panel never fails an assessment single mode would have passed.** A
  marker that dies after its own retries is reported and the run goes on with
  the rest; with one marker left the final sheet is that marker's, labelled as
  a degraded panel. Only every marker failing fails the step.
* **Nothing is paid for twice, and nothing is reused across inputs.** A marker
  sheet already on disk that passes the reuse predicate — well-formed, written
  by the model this marker names, and marked from the very transcript and case
  study this run is handing over — is adopted rather than re-marked, so a retry
  after one marker's failure re-runs that marker alone, and a restart mid-panel
  loses at most the call in flight (the assessor's own checkpoint covers the
  rest).
"""
from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.core.artifacts import artifact_metadata
from app.core.config import SCORES_PANEL_SUBDIRECTORY, Settings
from app.core.json_utils import extract_json_object, write_json_file
from app.core.process import CommandRunner
from app.llm.panel import MIN_PANEL_MARKERS, MarkingMode
from app.pipeline.marking.base import (
    SCORES_MEDIA_DIRECTORY,
    ContentMarkerRunner,
    MarkerAssignment,
    MarkingPlan,
    ProgressCallback,
)
from app.pipeline.marking.fingerprint import input_signature
from app.pipeline.marking.reconciliation import MarkerSheet, degraded_panel_sheet
from app.pipeline.marking.sheets import marker_sheet_needs_refresh
from app.services.event_service import EventService

logger = logging.getLogger(__name__)

# Sub-directory of the scores directory holding each session's marker sheets
# and adjudication record. Served by the existing /media/scores mount.
# The name itself lives in the storage layout because session teardown has to
# find this directory without importing this module (which would pull the LLM
# stack and app.services.event_service into a delete).
PANEL_SUBDIRECTORY = SCORES_PANEL_SUBDIRECTORY
ADJUDICATION_FILE_NAME = "adjudication.json"

# How the step's progress bar is divided: the markers share the first part of
# the bar, the adjudication takes the rest. A marker is network-bound and
# takes minutes; the adjudicator asks about a few criteria and takes seconds.
MARKERS_PROGRESS_SHARE = 80.0


@dataclass(frozen=True)
class MarkerOutcome:
    """One marker's result: its sheet on disk, and whether it was re-used."""

    assignment: MarkerAssignment
    path: Path
    payload: dict[str, Any]
    reused: bool


class PanelAdjudicatorRunner:
    """Spawns the adjudicator script once.

    Handed every path it reads, like every other scorer subprocess, and an
    environment naming only the adjudicator's target and carrying only its
    key. ``without_adjudicator`` runs the same script with no model at all,
    which is how disputes fall to the tie-break when no adjudicator can run
    here.
    """

    def __init__(
        self,
        settings: Settings,
        runner: CommandRunner,
        events: EventService,
        marker: ContentMarkerRunner,
    ) -> None:
        self.settings = settings
        self.runner = runner
        self.events = events
        # The subprocess environment base (Python settings, in-memory keys) is
        # the marker runner's; the adjudicator is a scorer like any other.
        self.marker = marker

    async def run(
        self,
        session_id: str,
        *,
        transcript_path: Path,
        case_study_path: Path,
        marker_paths: Sequence[Path],
        output_path: Path,
        adjudication_output_path: Path,
        tie_break: str,
        llm_env: dict[str, str],
        without_adjudicator: bool,
        warnings: Sequence[str] = (),
        log_source: str = "adjudicator",
    ) -> None:
        script = self.settings.panel_adjudicator_script_path
        if not script.exists():
            raise RuntimeError(f"Panel adjudicator script not found at {script}")
        args = [
            str(script),
            "--session-id",
            session_id,
            "--transcript",
            str(transcript_path),
            "--case-study",
            str(case_study_path),
            "--output",
            str(output_path),
            "--adjudication-output",
            str(adjudication_output_path),
            "--tie-break",
            str(tie_break),
        ]
        for path in marker_paths:
            args.extend(["--marker", str(path)])
        if without_adjudicator:
            args.append("--without-adjudicator")
        for warning in warnings:
            args.extend(["--warning", str(warning)])
        await self.events.publish(
            session_id,
            "log",
            {"source": log_source, "message": f"{self.settings.scorer_python_bin} {' '.join(args)}"},
        )
        await self.runner.run(
            self.settings.scorer_python_bin,
            args,
            "Panel adjudication",
            env=self.marker.python_env(llm_env),
            on_output=self.events.log_sink(session_id, log_source),
        )
        if not output_path.exists():
            raise RuntimeError("The panel adjudicator did not produce a final sheet.")


class PanelMarking:
    def __init__(
        self,
        marker: ContentMarkerRunner,
        adjudicator: PanelAdjudicatorRunner,
        settings: Settings,
        events: EventService,
    ) -> None:
        self.marker = marker
        self.adjudicator = adjudicator
        self.settings = settings
        self.events = events

    # --- layout ------------------------------------------------------------

    def panel_dir(self, session_id: str) -> Path:
        return self.settings.paths.output_scores_panel_dir / session_id

    def marker_output_path(self, session_id: str, key: str) -> Path:
        return self.panel_dir(session_id) / f"{key}.json"

    def adjudication_output_path(self, session_id: str) -> Path:
        return self.panel_dir(session_id) / ADJUDICATION_FILE_NAME

    def final_output_path(self, session_id: str) -> Path:
        return self.settings.paths.output_scores_dir / f"{session_id}.json"

    @staticmethod
    def media_directory(session_id: str) -> str:
        return f"{SCORES_MEDIA_DIRECTORY}/{PANEL_SUBDIRECTORY}/{session_id}"

    # --- execution ---------------------------------------------------------

    async def run(
        self,
        session_id: str,
        *,
        transcript_path: Path,
        case_study_path: Path,
        plan: MarkingPlan,
        on_progress: ProgressCallback | None = None,
    ) -> dict[str, Any]:
        """The final sheet's artefact metadata, plus ``panelArtifacts``: where
        each marker's own sheet and the adjudication record are served from.
        The URLs are the API's to know — the sheet itself names markers by key
        and stays ignorant of how this deployment mounts its storage."""
        if plan.mode is not MarkingMode.PANEL or len(plan.markers) < MIN_PANEL_MARKERS:
            raise ValueError("PanelMarking needs a plan in panel mode with at least two markers.")
        self.panel_dir(session_id).mkdir(parents=True, exist_ok=True)

        # Hashed once per run and shared by every marker: a sheet on disk is
        # adopted only if it was marked from these exact bytes.
        inputs = await asyncio.to_thread(
            input_signature, transcript_path=transcript_path, case_study_path=case_study_path
        )

        completed = 0
        progress_lock = asyncio.Lock()

        async def marker_done() -> None:
            nonlocal completed
            async with progress_lock:
                completed += 1
                fraction = completed / len(plan.markers)
            await self._report(on_progress, MARKERS_PROGRESS_SHARE * fraction)

        async def mark(assignment: MarkerAssignment) -> MarkerOutcome:
            try:
                return await self._mark(
                    session_id,
                    assignment,
                    transcript_path=transcript_path,
                    case_study_path=case_study_path,
                    inputs=inputs,
                )
            finally:
                await marker_done()

        results = await asyncio.gather(*(mark(assignment) for assignment in plan.markers), return_exceptions=True)

        outcomes: list[MarkerOutcome] = []
        failures: list[tuple[MarkerAssignment, BaseException]] = []
        for assignment, result in zip(plan.markers, results, strict=True):
            if isinstance(result, BaseException):
                if isinstance(result, asyncio.CancelledError):
                    raise result
                failures.append((assignment, result))
                logger.warning(
                    "Panel marker %s failed for session %s: %s", assignment.key, session_id, result
                )
            else:
                outcomes.append(result)

        if not outcomes:
            details = "; ".join(f"{assignment.key}: {error}" for assignment, error in failures)
            raise RuntimeError(f"Every panel marker failed: {details}") from failures[0][1]

        warnings = [
            *plan.warnings,
            *(f"Marker {assignment.key} failed and was left out: {error}" for assignment, error in failures),
        ]
        final_path = self.final_output_path(session_id)
        reconciled = len(outcomes) >= MIN_PANEL_MARKERS

        if not reconciled:
            await self._write_degraded_sheet(session_id, outcomes[0], plan, warnings, final_path)
        else:
            await self.adjudicator.run(
                session_id,
                transcript_path=transcript_path,
                case_study_path=case_study_path,
                marker_paths=[outcome.path for outcome in outcomes],
                output_path=final_path,
                adjudication_output_path=self.adjudication_output_path(session_id),
                tie_break=str(plan.tie_break),
                llm_env=dict(plan.adjudicator.llm_env) if plan.adjudicator is not None else {},
                without_adjudicator=plan.adjudicator is None,
                warnings=warnings,
            )
        await self._report(on_progress, 100.0)
        media_directory = self.media_directory(session_id)
        adjudication_path = self.adjudication_output_path(session_id)
        panel_artifacts: dict[str, Any] = {
            "markers": {
                outcome.assignment.key: artifact_metadata(outcome.path, media_directory) for outcome in outcomes
            },
            # Only a record this run wrote: a degraded run leaves an earlier
            # run's record on disk, and linking it would misattribute it.
            "adjudication": (
                artifact_metadata(adjudication_path, media_directory)
                if reconciled and adjudication_path.exists()
                else None
            ),
        }
        return {**artifact_metadata(final_path, SCORES_MEDIA_DIRECTORY), "panelArtifacts": panel_artifacts}

    async def _mark(
        self,
        session_id: str,
        assignment: MarkerAssignment,
        *,
        transcript_path: Path,
        case_study_path: Path,
        inputs: Mapping[str, Any],
    ) -> MarkerOutcome:
        path = self.marker_output_path(session_id, assignment.key)
        existing = await self._read_sheet(path)
        if existing is not None and not marker_sheet_needs_refresh(existing, assignment, inputs):
            await self.events.publish(
                session_id,
                "log",
                {
                    "source": f"scorer-{assignment.key}",
                    "message": f"Reusing the existing sheet from {assignment.target.key} at {path.name}.",
                },
            )
            return MarkerOutcome(assignment=assignment, path=path, payload=existing, reused=True)

        await self.marker.run(
            session_id,
            transcript_path=transcript_path,
            case_study_path=case_study_path,
            output_path=path,
            llm_env=assignment.llm_env,
            inputs=inputs,
            media_directory=self.media_directory(session_id),
            label=f"Content scoring ({assignment.target.key})",
            log_source=f"scorer-{assignment.key}",
        )
        payload = await self._read_sheet(path)
        if payload is None or marker_sheet_needs_refresh(payload, assignment, inputs):
            # A sheet the reuse predicate would reject next time is not a
            # result — most often a fallback answered instead of the marker,
            # which a panel must not silently accept.
            raise RuntimeError(
                f"Marker {assignment.target.key} did not produce a usable sheet attributable to it."
            )
        return MarkerOutcome(assignment=assignment, path=path, payload=payload, reused=False)

    async def _write_degraded_sheet(
        self,
        session_id: str,
        outcome: MarkerOutcome,
        plan: MarkingPlan,
        warnings: Sequence[str],
        final_path: Path,
    ) -> None:
        """One marker survived: its sheet becomes the final sheet, labelled."""
        failed = [warning for warning in warnings if warning.startswith("Marker ")]
        reason = (
            "Only one marker produced a sheet; the panel could not be reconciled. "
            + (" ".join(failed) if failed else "")
        ).strip()
        sheet = MarkerSheet.from_payload(outcome.assignment.key, outcome.payload)
        final = degraded_panel_sheet(
            outcome.payload,
            sheet,
            reason=reason,
            tie_break=plan.tie_break,
            warnings=warnings,
        )
        await asyncio.to_thread(write_json_file, final_path, final)
        await self.events.publish(
            session_id,
            "log",
            {"source": "adjudicator", "message": f"Panel degraded to a single marker ({sheet.key}): {reason}"},
        )

    @staticmethod
    async def _read_sheet(path: Path) -> dict[str, Any] | None:
        if not path.exists():
            return None
        try:
            payload = await asyncio.to_thread(lambda: extract_json_object(path.read_text(encoding="utf-8")))
        except (OSError, ValueError, json.JSONDecodeError):
            logger.debug("Could not read marker sheet %s; treating as absent.", path, exc_info=True)
            return None
        return payload if isinstance(payload, dict) else None

    @staticmethod
    async def _report(on_progress: ProgressCallback | None, percent: float) -> None:
        if on_progress is None:
            return
        try:
            await on_progress(percent)
        except Exception:  # pragma: no cover - progress is best effort
            logger.debug("Progress callback failed.", exc_info=True)


__all__ = [
    "ADJUDICATION_FILE_NAME",
    "PANEL_SUBDIRECTORY",
    "MarkerOutcome",
    "PanelAdjudicatorRunner",
    "PanelMarking",
]
