"""Single-model marking: one assessor, one sheet.

The default strategy and the behaviour this project shipped with. The marker
runs against the operator's primary model with its fallback chain, straight to
``scores/<id>.json``.
"""
from __future__ import annotations

from pathlib import Path

from app.core.artifacts import ArtifactMetadata
from app.core.config import Settings
from app.pipeline.marking.base import (
    SCORES_MEDIA_DIRECTORY,
    ContentMarkerRunner,
    MarkingPlan,
    ProgressCallback,
)


class SingleModelMarking:
    def __init__(self, marker: ContentMarkerRunner, settings: Settings) -> None:
        self.marker = marker
        self.settings = settings

    def output_path(self, session_id: str) -> Path:
        return self.settings.paths.output_scores_dir / f"{session_id}.json"

    async def run(
        self,
        session_id: str,
        *,
        transcript_path: Path,
        case_study_path: Path,
        plan: MarkingPlan,
        on_progress: ProgressCallback | None = None,
    ) -> ArtifactMetadata:
        # One subprocess, one call: there is no intermediate progress to
        # report, so the callback is accepted for interface parity and unused.
        del on_progress
        return await self.marker.run(
            session_id,
            transcript_path=transcript_path,
            case_study_path=case_study_path,
            output_path=self.output_path(session_id),
            llm_env=plan.single_env,
            media_directory=SCORES_MEDIA_DIRECTORY,
        )


__all__ = ["SingleModelMarking"]
