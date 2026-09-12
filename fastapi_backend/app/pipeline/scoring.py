from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any

from app.core.artifacts import artifact_metadata
from app.core.config import Settings
from app.core.exceptions import AppError
from app.core.json_utils import extract_json_object
from app.core.process import CommandRunner
from app.pipeline.marking.base import ContentMarkerRunner, MarkingPlan
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

    @staticmethod
    def should_refresh_score_payload(payload: Any) -> bool:
        if not isinstance(payload, dict):
            return True
        criteria = payload.get("criteria")
        if not isinstance(criteria, list) or len(criteria) < 2:
            return True
        if not all(isinstance(item, dict) and "is_critical" in item for item in criteria):
            return True
        summary = payload.get("scoring_summary")
        if not isinstance(summary, dict) or not summary.get("pass_fail"):
            return True
        critical_count = len([item for item in criteria if item.get("is_critical") is True])
        if int(summary.get("total_criteria") or -1) != len(criteria):
            return True
        if int(summary.get("critical_total") or -1) != critical_count:
            return True
        if str(payload.get("rubric_file") or "") != "embedded_in_case_study_pdf":
            return True
        if not payload.get("rubric_source"):
            return True
        if "transcript_quality_notes" in payload:
            return True
        return not all(isinstance(item.get("timestamp"), str) and item.get("timestamp") for item in criteria if isinstance(item, dict))

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

    async def run_content_scoring(self, session: dict[str, Any]) -> dict[str, Any]:
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
        plan = await self.content_marking_plan()
        return await self.single_marking.run(
            str(session["id"]),
            transcript_path=transcript_path,
            case_study_path=case_study_path,
            plan=plan,
        )

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
            on_output=lambda stream, text: self.events.publish(
                str(session["id"]),
                "log",
                {"source": f"audio-prof-{stream}", "message": text},
            ),
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
        if isinstance(parsed_rubric, dict) and parsed_rubric.get("rubric_asset_id"):
            session["communicationRubricAssetId"] = parsed_rubric["rubric_asset_id"]
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
            on_output=lambda stream, text: self.events.publish(
                str(session["id"]),
                "log",
                {"source": f"communication-scorer-{stream}", "message": text},
            ),
        )
        if not output_path.exists():
            raise RuntimeError("Communication scorer did not produce an output file.")
        await asyncio.to_thread(lambda: extract_json_object(output_path.read_text(encoding="utf-8")))
        return artifact_metadata(output_path, "/media/communication-scores")
