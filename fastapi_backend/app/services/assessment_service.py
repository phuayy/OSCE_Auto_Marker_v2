from __future__ import annotations

import json
from typing import Any

from app.core.artifacts import read_artifact_payload
from app.domain.enums import AssessmentResultStatus
from app.repositories.assessment_repository import AssessmentRepository


class AssessmentService:
    def __init__(self, repository: AssessmentRepository) -> None:
        self.repository = repository

    async def record_session_results(self, session: dict[str, Any]) -> None:
        results: list[dict[str, Any]] = []
        for output_key, result_type in (
            ("scores", "content"),
            ("communicationScores", "communication"),
            ("audioProfessionalism", "audio_professionalism"),
        ):
            output = (session.get("outputs") or {}).get(output_key)
            payload = await self._load_payload(output)
            if payload is None:
                continue
            results.append(self._result_payload(result_type, output, payload))

        if results:
            await self.repository.upsert_assessment(session=session, results=results)

    async def list_result_rows(self) -> list[dict[str, Any]]:
        return await self.repository.list_result_rows()

    async def delete_session_results(self, session_id: str) -> bool:
        return await self.repository.delete_for_session(session_id)

    async def _load_payload(self, output: Any) -> dict[str, Any] | None:
        if not isinstance(output, dict):
            return None
        return await read_artifact_payload(output, prefer_legacy=True)

    def _result_payload(self, result_type: str, output: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
        summary = payload.get("scoring_summary") if isinstance(payload.get("scoring_summary"), dict) else {}
        criteria = payload.get("criteria") if isinstance(payload.get("criteria"), list) else []
        # The communication scorer's per-criterion max lives only in the
        # top-level scoring_scale ({"All": 3, ...}); propagate it so each
        # criterion row carries max_score for normalised analytics.
        scale = payload.get("scoring_scale")
        default_max_score = (
            max(v for v in scale.values() if isinstance(v, (int, float)))
            if isinstance(scale, dict) and any(isinstance(v, (int, float)) for v in scale.values())
            else None
        )
        return {
            "resultType": result_type,
            "status": AssessmentResultStatus.COMPLETED,
            # "yes_count" is the achieved score for the content scorer's Yes/No
            # checklist summary, which emits no total_score key.
            "scoreTotal": self._first_value(summary, "total_score", "score", "achieved_score", "yes_count"),
            "scoreMax": self._first_value(summary, "max_score", "total_criteria", "maximum_score"),
            "passFail": self._first_value(summary, "pass_fail", "result", "status"),
            "outputPath": output.get("absolutePath"),
            "payload": payload,
            "criteria": [
                self._criterion_payload(item, default_max_score)
                for item in criteria
                if isinstance(item, dict)
            ],
        }

    @classmethod
    def _criterion_payload(cls, item: dict[str, Any], default_max_score: float | None = None) -> dict[str, Any]:
        score = cls._first_value(item, "score", "points")
        max_score = cls._first_value(item, "max_score", "max_points")
        # "value" carries the content scorer's Yes/No checklist verdict; the
        # repository's _bool_or_none maps yes/no strings to booleans.
        passed = cls._first_value(item, "passed", "met", "is_met", "value")
        # Yes/No checklist criteria have no numeric score key — store 1/0 out
        # of 1 so criterion rows aggregate uniformly across result types.
        if score is None and passed is not None:
            token = str(passed).strip().lower()
            if token in {"true", "yes", "y", "1", "pass", "passed"}:
                score, max_score = 1.0, max_score or 1.0
            elif token in {"false", "no", "n", "0", "fail", "failed"}:
                score, max_score = 0.0, max_score or 1.0
        return {
            "criterionKey": cls._first_value(item, "id", "criterion_id", "key", "number"),
            "label": cls._first_value(item, "criterion", "label", "name", "description"),
            "score": score,
            "maxScore": max_score if max_score is not None else default_max_score,
            "passed": passed,
            "isCritical": cls._first_value(item, "is_critical", "critical"),
            "scoreLabel": cls._first_value(item, "score_label", "label_score"),
            "timestamp": cls._first_value(item, "timestamp", "time"),
            "evidence": cls._first_value(item, "evidence", "rationale", "reasoning", "feedback", "reason"),
            "payload": json.loads(json.dumps(item, ensure_ascii=False)),
        }

    @staticmethod
    def _first_value(payload: dict[str, Any], *keys: str) -> Any:
        for key in keys:
            value = payload.get(key)
            if value is not None and value != "":
                return value
        return None
