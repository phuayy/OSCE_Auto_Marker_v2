"""The per-request view of the accounts table, cached until the table changes.

``AuthService.verify_token`` has to answer three questions about the account a
bearer token names on *every* authenticated request: does it still exist, is
it still active, and is the token's version still the row's version. Asking
the database each time would put a query in front of every API call — the
session index, every poll, every media fetch. Asking nothing would mean an
admin's "disable" only took effect when the marker's eight-hour token expired.

This is the same shape as the settings and credential caches: one
``SnapshotCache`` over one small, rarely written table, evicted by the change
feed. With the PostgreSQL listener connected a hit costs no query; on SQLite
it costs one ``table_versions`` read. Either way a write to ``users`` anywhere
— this process or another — reaches the next verification here.

Only the fields verification needs are kept. The snapshot is not the admin
screen's listing (that reads the repository directly and is not cached), and
it never carries a password hash.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from app.core.snapshot_cache import SnapshotCache
from app.domain.users import UserRole, UserStatus
from app.repositories.user_repository import UserRepository

if TYPE_CHECKING:  # pragma: no cover - import cycle guard
    from app.services.change_feed_service import ChangeFeedService


# Physical table name, as the change trigger reports it.
USERS_TABLE = "users"


@dataclass(frozen=True)
class UserSnapshot:
    id: str
    username: str
    role: UserRole
    status: UserStatus
    token_version: int
    display_name: str = ""
    email: str = ""

    @property
    def active(self) -> bool:
        return self.status is UserStatus.ACTIVE


class UserDirectory:
    def __init__(self, repository: UserRepository, *, changes: "ChangeFeedService | None" = None) -> None:
        self.repository = repository
        self._cache: SnapshotCache[dict[str, UserSnapshot]] = SnapshotCache(
            "user-directory",
            token_provider=((lambda: changes.token((USERS_TABLE,))) if changes is not None else None),
        )
        if changes is not None:
            # How a disable or a role change in another process reaches this
            # one. Registered at construction because the observer list is only
            # read after start().
            changes.add_change_observer(self._cache.observer_for(USERS_TABLE))

    async def _snapshot(self) -> dict[str, UserSnapshot]:
        return await self._cache.get(self._read_all)

    async def _read_all(self) -> dict[str, UserSnapshot]:
        result: dict[str, UserSnapshot] = {}
        for record in await self.repository.list_all():
            try:
                role = UserRole(record.role)
                status = UserStatus(record.status)
            except ValueError:
                # A row this build cannot interpret is treated as absent: an
                # unknown role or status must never authenticate as anything.
                continue
            result[record.id] = UserSnapshot(
                id=record.id,
                username=record.username,
                role=role,
                status=status,
                token_version=int(record.token_version or 0),
                display_name=record.display_name or "",
                email=record.email or "",
            )
        return result

    async def get(self, user_id: str) -> UserSnapshot | None:
        return (await self._snapshot()).get(str(user_id or ""))

    def invalidate(self, reason: str = "") -> None:
        """Drop the cached snapshot. Every local write to ``users`` calls this:
        with the listener connected the token is served from memory, so this
        process's counter does not move until its own notification arrives —
        and the response to an admin's action is built from the very snapshot
        the action changed."""
        self._cache.invalidate(reason)

    def cache_stats(self) -> dict[str, Any]:
        return self._cache.stats()
