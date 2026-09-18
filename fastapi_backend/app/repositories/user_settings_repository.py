from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from app.core.snapshot_cache import SnapshotCache
from app.database.models import UserSettingRecord, utc_now
from app.database.orm import OrmDatabase

if TYPE_CHECKING:  # pragma: no cover - import cycle guard
    from app.services.change_feed_service import ChangeFeedService


logger = logging.getLogger(__name__)

# Physical table name, as the change trigger reports it.
SETTINGS_TABLE = "user_settings"


class UserSettingsRepository:
    """Reads and writes each account's overrides of the user-scoped settings
    keys (``app.domain.settings_scope.USER_SCOPED_KEYS``), and owns the cache
    in front of the table.

    One row per user, the whole overlay as one JSON document — mirroring
    ``AppSettingsRepository``, just keyed on the account. The table is small
    and rarely written (an operator's own settings screen, a few times a
    session at most), so the whole thing is cached as one snapshot exactly the
    way ``AppSettingsRepository`` caches ``app_settings``: a write anywhere
    evicts this process's copy directly, and the change feed evicts every
    other process's, so a preference saved in one tab applies to that
    account's next run everywhere without a restart.

    Without a change feed the cache disables itself and every read goes to the
    database, exactly as before.
    """

    def __init__(self, database: OrmDatabase, *, changes: "ChangeFeedService | None" = None) -> None:
        self.database = database
        self._cache: SnapshotCache[dict[str, dict[str, Any]]] = SnapshotCache(
            "user-settings",
            token_provider=(
                (lambda: changes.token((SETTINGS_TABLE,))) if changes is not None else None
            ),
        )
        if changes is not None:
            changes.add_change_observer(self._cache.observer_for(SETTINGS_TABLE))

    async def _snapshot(self) -> dict[str, dict[str, Any]]:
        """Every account's overrides, keyed by user id. One query serves every
        accessor below."""
        return await self._cache.get(self._read_all)

    async def _read_all(self) -> dict[str, dict[str, Any]]:
        async with self.database.session() as db:
            records = (await db.scalars(select(UserSettingRecord))).all()
        return {record.user_id: dict(record.values or {}) for record in records}

    def invalidate(self, reason: str = "") -> None:
        self._cache.invalidate(reason)

    def cache_stats(self) -> dict[str, Any]:
        return self._cache.stats()

    async def overrides_for(self, user_id: str) -> dict[str, Any]:
        """This account's stored overrides, or ``{}`` when it has none."""
        snapshot = await self._snapshot()
        return dict(snapshot.get(user_id) or {})

    async def set_overrides(self, user_id: str, values: dict[str, Any]) -> dict[str, Any]:
        """Merge ``values`` into this account's stored overrides and return the
        result. A key mapped to ``None`` is dropped — the way a caller clears
        one preference back to the deployment default without touching the
        rest."""
        async with self.database.transaction() as db:
            record = await db.get(UserSettingRecord, user_id)
            current = dict(record.values or {}) if record is not None else {}
            for key, value in values.items():
                if value is None:
                    current.pop(key, None)
                else:
                    current[key] = value
            if record is None:
                db.add(UserSettingRecord(user_id=user_id, values=current))
            else:
                record.values = current
                record.updated_at = utc_now()
        # Evicted here rather than left to the trigger's announcement — see
        # AppSettingsRepository.set_values for why: the response to this very
        # call must be built from the snapshot it just wrote, not the one it
        # replaced.
        self.invalidate("user settings written")
        return dict(current)

    async def forget(self, user_id: str) -> None:
        """Remove every stored override for an account. Belt and braces beside
        the table's ``ON DELETE CASCADE`` — deleting the account already takes
        the row with it; this is for the caller that wants to reset an account
        in place without deleting it."""
        async with self.database.transaction() as db:
            record = await db.get(UserSettingRecord, user_id)
            if record is not None:
                await db.delete(record)
        self.invalidate("user settings cleared")
