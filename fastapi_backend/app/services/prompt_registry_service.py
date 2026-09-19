"""The prompt-version audit ledger: syncs scripts/prompt_catalog.py into the
database and answers the admin-only read routes.

The prompt wording in ``scripts/*.py`` stays the sole source of truth for what
actually runs — this service never feeds anything back into a scoring
subprocess. It exists only so a prompt edit is traceable and diffable against
what shipped before. ``sync_from_scripts`` is best-effort: a failure here must
never block API startup or scoring, since the prompts run fine from code
either way (see ``AppContainer.startup``, where the call is wrapped in a
try/except).
"""
from __future__ import annotations

import json
import logging
from typing import Any

from app.core.config import Settings
from app.core.process import CommandRunner
from app.repositories.prompt_version_repository import (
    PromptCatalogEntry,
    PromptVersionRepository,
    PromptVersionSyncResult,
)


logger = logging.getLogger(__name__)


class PromptRegistryService:
    def __init__(
        self,
        repository: PromptVersionRepository,
        runner: CommandRunner,
        settings: Settings,
    ) -> None:
        self.repository = repository
        self.runner = runner
        self.settings = settings

    async def sync_from_scripts(self) -> PromptVersionSyncResult:
        if not self.settings.prompt_catalog_script_path.exists():
            raise RuntimeError(
                f"Prompt catalog script not found at {self.settings.prompt_catalog_script_path}"
            )
        result = await self.runner.run(
            self.settings.scorer_python_bin,
            [str(self.settings.prompt_catalog_script_path)],
            "Prompt catalog sync",
            env=self.settings.subprocess_env({}),
            timeout_seconds=60,
        )
        payload = json.loads(result.stdout)
        entries = [
            PromptCatalogEntry(
                key=str(item["key"]),
                version=str(item["version"]),
                text=str(item["text"]),
                source_script=str(item["sourceScript"]),
            )
            for item in payload.get("entries") or []
        ]
        sync_result = await self.repository.record_if_new(entries)
        logger.info(
            "Prompt-version ledger synced: %d inserted, %d unchanged, %d drifted (wording "
            "changed without a version bump)%s.",
            len(sync_result.inserted),
            len(sync_result.unchanged),
            len(sync_result.drifted),
            f" — drifted: {', '.join(sync_result.drifted)}" if sync_result.drifted else "",
        )
        return sync_result

    async def list_versions(self, prompt_key: str | None = None) -> list[dict[str, Any]]:
        return await self.repository.list_versions(prompt_key)

    async def get(self, record_id: str) -> dict[str, Any] | None:
        return await self.repository.get(record_id)
