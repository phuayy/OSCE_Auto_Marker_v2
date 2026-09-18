from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from app.core.config import Settings


class SessionArtifacts:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    @staticmethod
    def valid_id(value: str) -> bool:
        return bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", value))

    def owned_paths(self, session: dict[str, Any], *, keep_clips: bool = False,
                    keep_upload: bool = False) -> set[Path]:
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
                owned.add(category / session_id)
                for item in category.iterdir():
                    if item.name.startswith(f"{session_id}.") or item.name.startswith(f".{session_id}."):
                        owned.add(item)
        owned.add(paths.output_scores_panel_dir / session_id)
        if not keep_upload:
            owned.add(paths.uploads_dir / f"{session_id}.json")
            upload_id = str((session.get("upload") or {}).get("id") or "")
            if self.valid_id(upload_id):
                owned.add(paths.uploads_dir / f"{upload_id}.json")
                owned.add(paths.uploads_dir / f"{upload_id}.json.migrated")
                owned.add(self.settings.object_storage_root / ".uploads" / upload_id)
        return {path for path in owned if not any(parent.is_symlink() for parent in path.parents)}
