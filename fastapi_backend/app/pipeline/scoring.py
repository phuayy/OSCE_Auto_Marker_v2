from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

from app.core.config import Settings
from app.core.json_utils import extract_json_object, write_json_file
from app.core.process import CommandRunner
from app.services.auth_service import AuthService
from app.services.event_service import EventService


class ScoringPipeline:
    def __init__(
        self,
        settings: Settings,
        runner: CommandRunner,
        events: EventService,
        auth: AuthService,
        rubric_service: Any,
    ) -> None:
        self.settings = settings
        self.runner = runner
        self.events = events
        self.auth = auth
        self.rubric_service = rubric_service

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
        env = {"PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"}
        if self.auth.runtime.nvidia_api_key:
            env["NVIDIA_API_KEY"] = self.auth.runtime.nvidia_api_key
        env.update(extra_env or {})
        return env

    async def run_content_scoring(self, session: dict[str, Any]) -> dict[str, Any]:
        if not self.settings.scorer_script_path.exists():
            raise RuntimeError(f"Scorer script not found at {self.settings.scorer_script_path}")
        output_path = self.settings.paths.output_scores_dir / f"{session['id']}.json"
        args = [str(self.settings.scorer_script_path), "--session-id", str(session["id"]), "--output", str(output_path)]
        case_study = (session.get("files") or {}).get("caseStudy") or {}
        if case_study.get("absolutePath") and Path(str(case_study["absolutePath"])).exists():
            args.extend(["--case-study", str(case_study["absolutePath"])])
        await self.events.publish(
            str(session["id"]),
            "log",
            {"source": "scorer", "message": f"{self.settings.scorer_python_bin} {' '.join(args)}"},
        )
        result = await self.runner.run(
            self.settings.scorer_python_bin,
            args,
            "OpenRouter scoring",
            env=self.python_env(),
            on_output=lambda stream, text: self.events.publish(
                str(session["id"]),
                "log",
                {"source": f"scorer-{stream}", "message": text},
            ),
        )
        if output_path.exists():
            payload = await asyncio.to_thread(lambda: extract_json_object(output_path.read_text(encoding="utf-8")))
        else:
            payload = extract_json_object(result.stdout)
            await asyncio.to_thread(write_json_file, output_path, payload)
        stats = output_path.stat()
        return {
            "fileName": output_path.name,
            "absolutePath": str(output_path),
            "url": f"/media/scores/{output_path.name}",
            "sizeBytes": stats.st_size,
            "payload": payload,
        }

    async def run_audio_professionalism(self, session: dict[str, Any]) -> dict[str, Any]:
        if not self.settings.audio_professionalism_script_path.exists():
            raise RuntimeError(f"Audio professionalism script not found at {self.settings.audio_professionalism_script_path}")
        outputs = session.get("outputs") or {}
        audio_path = Path(str((outputs.get("audio") or {}).get("absolutePath") or ""))
        transcript_path = Path(str((outputs.get("transcript") or {}).get("absolutePath") or ""))
        if not audio_path.exists():
            raise RuntimeError("Audio professionalism requires an extracted audio file.")
        if not transcript_path.exists():
            raise RuntimeError("Audio professionalism requires a normalized transcript.")
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
            env=self.python_env({"FFMPEG_BIN": self.settings.ffmpeg_bin}),
            on_output=lambda stream, text: self.events.publish(
                str(session["id"]),
                "log",
                {"source": f"audio-prof-{stream}", "message": text},
            ),
        )
        if not output_path.exists():
            raise RuntimeError("Audio professionalism extractor did not produce an output file.")
        payload = await asyncio.to_thread(lambda: extract_json_object(output_path.read_text(encoding="utf-8")))
        stats = output_path.stat()
        return {
            "fileName": output_path.name,
            "absolutePath": str(output_path),
            "url": f"/media/audio-professionalism/{output_path.name}",
            "sizeBytes": stats.st_size,
            "payload": payload,
        }

    async def run_communication_scoring(
        self,
        session: dict[str, Any],
        audio_professionalism: dict[str, Any] | None,
    ) -> dict[str, Any]:
        if not self.settings.communication_scorer_script_path.exists():
            raise RuntimeError(f"Communication scorer script not found at {self.settings.communication_scorer_script_path}")
        outputs = session.get("outputs") or {}
        transcript_path = Path(str((outputs.get("transcript") or {}).get("absolutePath") or ""))
        if not transcript_path.exists():
            raise RuntimeError("Communication scoring requires a normalized transcript.")
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
            env=self.python_env(),
            on_output=lambda stream, text: self.events.publish(
                str(session["id"]),
                "log",
                {"source": f"communication-scorer-{stream}", "message": text},
            ),
        )
        if not output_path.exists():
            raise RuntimeError("Communication scorer did not produce an output file.")
        payload = await asyncio.to_thread(lambda: extract_json_object(output_path.read_text(encoding="utf-8")))
        stats = output_path.stat()
        return {
            "fileName": output_path.name,
            "absolutePath": str(output_path),
            "url": f"/media/communication-scores/{output_path.name}",
            "sizeBytes": stats.st_size,
            "payload": payload,
        }
