from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.core.artifacts import ArtifactMetadata, artifact_metadata
from app.core.config import Settings
from app.core.exceptions import AppError
from app.core.json_utils import extract_json_object
from app.core.process import CommandRunner
from app.llm.panel import MarkingMode
from app.pipeline.marking.base import ContentMarkerRunner, MarkingPlan, ProgressCallback
from app.pipeline.marking.panel import PanelAdjudicatorRunner, PanelMarking
from app.pipeline.marking.sheets import final_sheet_needs_refresh, sheet_needs_refresh
from app.pipeline.marking.single import SingleModelMarking
from app.services.auth_service import AuthService
from app.services.event_service import EventService

logger = logging.getLogger(__name__)


def _require_input(raw_path: Any, description: str) -> Path:
    """The path of an input a scorer must be handed, or a non-retryable failure.

    The scorers used to find their own inputs when the API did not pass one —
    the raw WhisperX ``.srt`` instead of the normalised transcript, or the
    newest PDF in the upload folder instead of *this* session's case study.
    Both are wrong answers that look like right ones. The API knows every path,
    so it hands them over explicitly, and a missing one fails the step here with
    the path named rather than letting a subprocess guess. Not retryable: the
    file will be just as absent on the next attempt.
    """
    path = Path(str(raw_path or ""))
    if not raw_path or not path.is_file():
        raise AppError(
            f"{description} is missing ({path or 'no path recorded'}); the step cannot run without it.",
            status_code=422,
            retryable=False,
        )
    return path


@dataclass
class ContentMarkingRun:
    """One session's content marking, with its plan resolved.

    Built by :meth:`ScoringPipeline.prepare_content_marking` and consumed by
    the pipeline service, which asks it three things in order: does the sheet
    on disk still count (``needs_refresh``), run if not (``run``), and what to
    record on the step when it is done (``step_metadata``). The plan is fixed
    at construction: a toggle flipped between the cache check and the run
    cannot make them disagree about the mode.
    """

    session: dict[str, Any]
    plan: MarkingPlan
    pipeline: "ScoringPipeline"
    result: dict[str, Any] | None = field(default=None, repr=False)

    def needs_refresh(self, payload: Any) -> bool:
        return final_sheet_needs_refresh(payload, self.plan)

    async def run(self, on_progress: ProgressCallback | None = None) -> ArtifactMetadata:
        artifact = await self.pipeline.run_content_marking(self.session, self.plan, on_progress=on_progress)
        self.result = await self.pipeline.read_sheet_summary(artifact)
        return artifact

    def step_metadata(self) -> dict[str, Any]:
        """What the ``content_scoring`` step records: the mode that ran and,
        for a panel, how the markers agreed. Credential-free by construction —
        the plan's ``describe`` never includes an environment."""
        metadata: dict[str, Any] = {
            "markingMode": str(self.plan.mode),
            "selectedMarkingMode": str(self.plan.selected_mode),
        }
        if self.plan.warnings:
            metadata["markingWarnings"] = list(self.plan.warnings)
        if self.plan.mode is MarkingMode.PANEL:
            metadata["markers"] = [assignment.describe() for assignment in self.plan.markers]
            metadata["adjudicator"] = self.plan.adjudicator.describe() if self.plan.adjudicator else None
        if self.result:
            metadata["panel"] = self.result
        return metadata


class ScoringPipeline:
    def __init__(
        self,
        settings: Settings,
        runner: CommandRunner,
        events: EventService,
        auth: AuthService,
        rubric_service: Any,
        llm_settings: Any | None = None,
    ) -> None:
        self.settings = settings
        self.runner = runner
        self.events = events
        self.auth = auth
        self.rubric_service = rubric_service
        # Resolves the operator's primary/fallback model choice into the
        # environment the scorer subprocesses read. Optional so tests can build
        # a ScoringPipeline without a database.
        self.llm_settings = llm_settings
        # One marker subprocess; the strategy decides how many times it runs.
        self.marker = ContentMarkerRunner(settings, runner, events, auth)
        self.single_marking = SingleModelMarking(self.marker, settings)
        self.panel_marking = PanelMarking(
            self.marker,
            PanelAdjudicatorRunner(settings, runner, events, self.marker),
            settings,
            events,
        )

    @staticmethod
    def should_refresh_score_payload(payload: Any) -> bool:
        """Is this content sheet well-formed? Mode-agnostic; the mode-aware
        check lives on :class:`ContentMarkingRun`, which knows the plan."""
        return sheet_needs_refresh(payload)

    @staticmethod
    def should_refresh_audio_professionalism_payload(payload: Any) -> bool:
        if not isinstance(payload, dict):
            return True
        if str(payload.get("schema") or "") != "audio-professionalism-v1":
            return True
        return not isinstance(payload.get("metrics"), dict)

    @staticmethod
    def should_refresh_communication_payload(payload: Any) -> bool:
        if not isinstance(payload, dict):
            return True
        if str(payload.get("schema") or "") != "communication-scoring-v2":
            return True
        criteria = payload.get("criteria")
        if not isinstance(criteria, list) or len(criteria) < 2:
            return True
        valid_labels = {"None", "Some", "Most", "All"}
        if not all(isinstance(item, dict) and str(item.get("score_label") or "").strip() in valid_labels for item in criteria):
            return True
        summary = payload.get("scoring_summary")
        if not isinstance(summary, dict):
            return True
        return not all(isinstance(summary.get(key), (int, float)) for key in ("total_score", "max_score", "pass_threshold"))

    def python_env(self, extra_env: dict[str, str] | None = None) -> dict[str, str]:
        return self.marker.python_env(extra_env)

    async def scoring_env(self, extra_env: dict[str, str] | None = None) -> dict[str, str]:
        """``python_env`` plus the resolved LLM routing for this run.

        Read per run, not per process: the operator can change the primary model
        while a long job queue is draining, and the next scorer to start must
        pick it up. A failure to resolve routing is logged and skipped rather
        than raised — the subprocess then falls back to its own environment
        variables, which is exactly the pre-router behaviour.
        """
        env = self.python_env(extra_env)
        if self.llm_settings is None:
            return env
        try:
            env.update(await self.llm_settings.subprocess_env())
        except Exception:
            logger.exception("Failed to resolve LLM routing; the scorer will use its environment defaults.")
        return env

    async def content_marking_plan(self) -> MarkingPlan:
        """The marking plan for this run, resolved once.

        Same contract as ``scoring_env``: read per run, never per process, and
        a failure to resolve is logged and replaced by a plan that lets the
        subprocess fall back to its own environment variables rather than
        failing the step.
        """
        if self.llm_settings is None:
            return MarkingPlan.single_only()
        try:
            return await self.llm_settings.marking_plan()
        except Exception:
            logger.exception("Failed to resolve the marking plan; the scorer will use its environment defaults.")
            return MarkingPlan.single_only()

    async def prepare_content_marking(self, session: dict[str, Any]) -> ContentMarkingRun:
        """Resolve the plan for this session's content marking, once.

        The pipeline service calls this before it looks at the cached sheet, so
        the same plan decides both whether that sheet still counts and what
        runs if it does not.
        """
        return ContentMarkingRun(session=session, plan=await self.content_marking_plan(), pipeline=self)

    async def run_content_scoring(self, session: dict[str, Any]) -> dict[str, Any]:
        """Mark content under whatever the operator selected, resolving the
        plan here. The one-call form; the pipeline service prefers
        :meth:`prepare_content_marking` so the cache check sees the plan too."""
        return await self.run_content_marking(session, await self.content_marking_plan())

    async def run_content_marking(
        self,
        session: dict[str, Any],
        plan: MarkingPlan,
        *,
        on_progress: ProgressCallback | None = None,
    ) -> ArtifactMetadata:
        outputs = session.get("outputs") or {}
        # The normalised transcript — the same document the communication
        # branch scores — never the raw engine .srt: hallucination drops and
        # speaker labels exist only here, and the two scorers must agree on
        # what was said.
        transcript_path = _require_input(
            (outputs.get("transcript") or {}).get("absolutePath"), "The normalised transcript"
        )
        case_study_path = _require_input(
            ((session.get("files") or {}).get("caseStudy") or {}).get("absolutePath"),
            "This session's case-study PDF",
        )
        strategy = self.panel_marking if plan.mode is MarkingMode.PANEL else self.single_marking
        return await strategy.run(
            str(session["id"]),
            transcript_path=transcript_path,
            case_study_path=case_study_path,
            plan=plan,
            on_progress=on_progress,
        )

    @staticmethod
    async def read_sheet_summary(artifact: dict[str, Any]) -> dict[str, Any] | None:
        """The part of a final sheet worth copying onto the pipeline step: the
        panel's agreement figures and whether it degraded. ``None`` for a
        single-model sheet, which has nothing of the kind to say."""
        path = Path(str(artifact.get("absolutePath") or ""))
        if not path.is_file():
            return None
        try:
            payload = await asyncio.to_thread(lambda: extract_json_object(path.read_text(encoding="utf-8")))
        except (OSError, ValueError, json.JSONDecodeError):
            return None
        panel = payload.get("panel") if isinstance(payload, dict) else None
        if not isinstance(panel, dict):
            return None
        return {
            "markers": [
                {"key": item.get("key"), "providerId": item.get("provider_id"), "model": item.get("model")}
                for item in (panel.get("markers") or [])
                if isinstance(item, dict)
            ],
            "agreement": panel.get("agreement"),
            "adjudicatorCalled": (panel.get("adjudicator") or {}).get("called") if isinstance(panel.get("adjudicator"), dict) else None,
            "degraded": panel.get("degraded"),
            "warnings": list(panel.get("warnings") or []),
        }

    async def run_audio_professionalism(self, session: dict[str, Any]) -> dict[str, Any]:
        if not self.settings.audio_professionalism_script_path.exists():
            raise RuntimeError(f"Audio professionalism script not found at {self.settings.audio_professionalism_script_path}")
        outputs = session.get("outputs") or {}
        audio_path = _require_input((outputs.get("audio") or {}).get("absolutePath"), "The extracted audio")
        transcript_path = _require_input(
            (outputs.get("transcript") or {}).get("absolutePath"), "The normalised transcript"
        )
        output_path = self.settings.paths.output_audio_professionalism_dir / f"{session['id']}.json"
        args = [
            str(self.settings.audio_professionalism_script_path),
            "--session-id",
            str(session["id"]),
            "--audio",
            str(audio_path),
            "--transcript",
            str(transcript_path),
            "--output",
            str(output_path),
        ]
        student_speaker = os.getenv("AUDIO_PROF_STUDENT_SPEAKER")
        if student_speaker:
            args.extend(["--student-speaker", student_speaker.strip()])
        await self.events.publish(
            str(session["id"]),
            "log",
            {"source": "audio-prof", "message": f"{self.settings.scorer_python_bin} {' '.join(args)}"},
        )
        await self.runner.run(
            self.settings.scorer_python_bin,
            args,
            "Audio professionalism extraction",
            env=await self.scoring_env({"FFMPEG_BIN": self.settings.ffmpeg_bin}),
            on_output=self.events.log_sink(str(session["id"]), "audio-prof"),
        )
        if not output_path.exists():
            raise RuntimeError("Audio professionalism extractor did not produce an output file.")
        await asyncio.to_thread(lambda: extract_json_object(output_path.read_text(encoding="utf-8")))
        return artifact_metadata(output_path, "/media/audio-professionalism")

    async def run_communication_scoring(
        self,
        session: dict[str, Any],
        audio_professionalism: dict[str, Any] | None,
    ) -> dict[str, Any]:
        if not self.settings.communication_scorer_script_path.exists():
            raise RuntimeError(f"Communication scorer script not found at {self.settings.communication_scorer_script_path}")
        outputs = session.get("outputs") or {}
        transcript_path = _require_input(
            (outputs.get("transcript") or {}).get("absolutePath"), "The normalised transcript"
        )
        output_path = self.settings.paths.output_communication_scores_dir / f"{session['id']}.json"
        args = [
            str(self.settings.communication_scorer_script_path),
            "--session-id",
            str(session["id"]),
            "--transcript",
            str(transcript_path),
            "--output",
            str(output_path),
        ]
        audio_prof_path = (audio_professionalism or {}).get("absolutePath")
        if audio_prof_path and Path(str(audio_prof_path)).exists():
            args.extend(["--audio-professionalism", str(audio_prof_path)])
        parsed_rubric = await self.rubric_service.ensure_parsed()
        rubric_asset_id = (
            parsed_rubric.get("rubric_asset_id") if isinstance(parsed_rubric, dict) else None
        )
        rubric_json_path = self.settings.paths.communication_rubric_json_path
        if rubric_json_path.exists():
            args.extend(["--parsed-rubric", str(rubric_json_path)])
        elif self.settings.paths.default_rubric_source_pdf.exists():
            args.extend(["--rubric", str(self.settings.paths.default_rubric_source_pdf)])
        await self.events.publish(
            str(session["id"]),
            "log",
            {"source": "communication-scorer", "message": f"{self.settings.scorer_python_bin} {' '.join(args)}"},
        )
        await self.runner.run(
            self.settings.scorer_python_bin,
            args,
            "Communication scoring",
            env=await self.scoring_env(),
            on_output=self.events.log_sink(str(session["id"]), "communication-scorer"),
        )
        if not output_path.exists():
            raise RuntimeError("Communication scorer did not produce an output file.")
        await asyncio.to_thread(lambda: extract_json_object(output_path.read_text(encoding="utf-8")))
        artifact: dict[str, Any] = dict(artifact_metadata(output_path, "/media/communication-scores"))
        if rubric_asset_id:
            # Which parsed rubric produced these marks. It rides the output
            # record because PipelineService stores that record through
            # _assign_output inside _commit: a value written onto the caller's
            # working dict instead would be wiped by the very next commit.
            artifact["rubricAssetId"] = str(rubric_asset_id)
        return artifact
