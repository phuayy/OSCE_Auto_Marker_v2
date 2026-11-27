from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from app.core.config import Settings


# Output categories derived only from the source video (see SessionArtifacts).
SOURCE_DERIVED_CATEGORIES = ("playback",)


class SessionArtifacts:
    """Which on-disk paths a session owns.

    Two kinds of path are owned, and they have different lifetimes. *Run
    outputs* (audio, transcripts, scores, clips...) are produced by a run and
    are discarded when it is re-run. *Source artefacts* belong to the source
    video, which a re-run does not change: the upload records and the artefacts
    derived from the video alone — the browser playback copy and the poster
    frame, which ``files.video`` keeps pointing at. Both go when the session is
    deleted; only the run outputs go on a re-run (``keep_source=True``).
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    @staticmethod
    def valid_id(value: str) -> bool:
        return bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", value))

    def owned_paths(self, session: dict[str, Any], *, keep_clips: bool = False,
                    keep_source: bool = False) -> set[Path]:
        session_id = str(session.get("id") or "")
        if not self.valid_id(session_id):
            return set()
        paths = self.settings.paths
        output = paths.storage_root / "output"
        owned: set[Path] = set()
        if output.is_dir():
            for category in output.iterdir():
                if not category.is_dir() or category.is_symlink():
                    continue
                if category == paths.output_case_study_rubrics_dir:
                    continue
                if keep_clips and category == paths.output_clips_dir:
                    continue
                if keep_source and category.name in SOURCE_DERIVED_CATEGORIES:
                    continue
                owned.add(category / session_id)
                for item in category.iterdir():
                    if item.name.startswith(f"{session_id}.") or item.name.startswith(f".{session_id}."):
                        owned.add(item)
        owned.add(paths.output_scores_panel_dir / session_id)
        if not keep_source:
            owned.add(paths.uploads_dir / f"{session_id}.json")
            upload_id = str((session.get("upload") or {}).get("id") or "")
            if self.valid_id(upload_id):
                owned.add(paths.uploads_dir / f"{upload_id}.json")
                owned.add(paths.uploads_dir / f"{upload_id}.json.migrated")
                owned.add(self.settings.object_storage_staging_root / upload_id)
        return {path for path in owned if not any(parent.is_symlink() for parent in path.parents)}
