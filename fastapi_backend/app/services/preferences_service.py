"""Two-tier settings resolution: deployment defaults, a marker's own overrides.

``AppSettingsRepository`` is still the one deployment-wide document — the
provider catalogue and its keys, the corpus, the rubric, and the admin's own
defaults for the seven keys a marker may personalise. ``UserSettingsRepository``
holds each account's overrides of exactly those seven
(``app.domain.settings_scope.USER_SCOPED_KEYS``). This service is where the
two meet: every accessor the pipeline reads takes the *session owner's* user
id (``None`` for the deployment default alone — a system-initiated caller with
no owner, or the deliberately unscoped read) and answers with the deployment
document, the owner's overrides applied on top.

Reading is live, the same contract ``AppSettingsRepository`` already
documents: a preference saved in the settings screen applies to that
account's next run in every process, with no restart — the merge itself costs
nothing extra because both repositories are already cached snapshots kept
fresh by the change feed.
"""

from __future__ import annotations

import logging
from typing import Any

from app.domain.settings_scope import is_user_scoped
from app.repositories.app_settings_repository import (
    LLM_FALLBACKS_KEY,
    LLM_MARKING_MODE_KEY,
    LLM_PANEL_KEY,
    LLM_PREPROCESS_KEY,
    LLM_PRIMARY_KEY,
    TRANSCRIPTION_ENGINE_KEY,
    TRANSCRIPTION_OPTIONS_KEY,
    AppSettingsRepository,
)
from app.repositories.user_settings_repository import UserSettingsRepository

logger = logging.getLogger(__name__)


class PreferencesService:
    def __init__(self, app_settings: AppSettingsRepository, user_settings: UserSettingsRepository) -> None:
        self.app_settings = app_settings
        self.user_settings = user_settings

    # --- documents -----------------------------------------------------------

    async def defaults(self) -> dict[str, Any]:
        """The deployment's own settings document, admin's to set."""
        return await self.app_settings.get_all()

    async def overrides_for(self, user_id: str | None) -> dict[str, Any]:
        """This account's stored overrides, or ``{}`` for no account / none saved."""
        if not user_id:
            return {}
        return await self.user_settings.overrides_for(user_id)

    async def effective_for(self, user_id: str | None) -> dict[str, Any]:
        """The document a run for ``user_id`` actually sees: the deployment
        defaults with this account's *user-scoped* overrides layered on top.

        A stray deployment-scoped key in the overrides table (there should
        never be one — the write path enforces the boundary) is ignored here
        too, so a bug on the write side cannot let a marker's row reach past
        what they are allowed to change.
        """
        document = await self.defaults()
        for key, value in (await self.overrides_for(user_id)).items():
            if is_user_scoped(key):
                document[key] = value
        return document

    async def set_overrides(self, user_id: str, values: dict[str, Any]) -> dict[str, Any]:
        """Store ``user_id``'s overrides for the given keys. Every key must be
        user-scoped — the route checks that before calling this, so the 403
        can name the offending key rather than this method silently dropping it."""
        return await self.user_settings.set_overrides(user_id, values)

    async def clear_override(self, user_id: str, key: str) -> dict[str, Any]:
        """Revert one key to the deployment default."""
        return await self.user_settings.set_overrides(user_id, {key: None})

    async def forget(self, user_id: str) -> None:
        await self.user_settings.forget(user_id)

    # --- typed accessors -------------------------------------------------
    #
    # Same shapes AppSettingsRepository always returned, now resolved for one
    # account. user_id=None reads the deployment default alone — the prefetch
    # task and any other caller with no session behind it.

    async def transcription_selection(self, user_id: str | None) -> tuple[str, dict[str, Any]]:
        document = await self.effective_for(user_id)
        engine_id = str(document.get(TRANSCRIPTION_ENGINE_KEY) or "")
        stored_options = document.get(TRANSCRIPTION_OPTIONS_KEY)
        if not isinstance(stored_options, dict):
            stored_options = {}
        return engine_id, dict(stored_options)

    async def llm_routing_selection(self, user_id: str | None) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        document = await self.effective_for(user_id)
        primary = document.get(LLM_PRIMARY_KEY)
        fallbacks = document.get(LLM_FALLBACKS_KEY)
        if not isinstance(primary, dict):
            primary = {}
        if not isinstance(fallbacks, list):
            fallbacks = []
        return dict(primary), [dict(item) for item in fallbacks if isinstance(item, dict)]

    async def marking_selection(self, user_id: str | None) -> tuple[str, dict[str, Any]]:
        document = await self.effective_for(user_id)
        mode = str(document.get(LLM_MARKING_MODE_KEY) or "single")
        panel = document.get(LLM_PANEL_KEY)
        if not isinstance(panel, dict):
            panel = {}
        return mode, dict(panel)

    async def llm_preprocess_enabled(self, user_id: str | None) -> bool:
        """A read failure means 'off' — a settings lookup must never fail a
        scoring run, the same contract AppSettingsRepository held alone."""
        try:
            document = await self.effective_for(user_id)
            return bool(document.get(LLM_PREPROCESS_KEY))
        except Exception:
            logger.exception("Failed to read %s preference; treating as disabled.", LLM_PREPROCESS_KEY)
            return False
