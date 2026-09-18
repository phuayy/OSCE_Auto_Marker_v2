from __future__ import annotations

from typing import Iterable

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.database.models import TableVersionRecord


# Postgres NOTIFY channel carrying committed-change announcements. One channel
# for every tracked table; the payload names which one changed.
CHANGE_CHANNEL = "osce_changes"

# Tables whose writes invalidate a cached API projection, or that a browser
# needs to hear about. Keep this list short: every entry costs a trigger on the
# write path.
#
#   sessions             -> /api/sessions index (written by API *and* the Hatchet
#                           worker process, which is why the counter must live in
#                           the database rather than in memory)
#   assessment_results   -> analytics + clip summaries
#   jobs                 -> job status shown on session cards
#   notifications        -> the notification feed and unread badge
#   provider_credentials -> the decrypted LLM API keys cached in every process
#   app_settings         -> the model/engine selection cached in every process
#
# ``notifications`` is tracked for a reason worth spelling out. A notification is
# normally pushed straight to subscribers by
# ``NotificationService._publish`` -> ``ChangeFeedService.publish_event``, which
# is an *in-process* fan-out. Under ``JOB_QUEUE_BACKEND=hatchet`` the pipeline
# runs in the worker process, so that push reaches the worker's own (empty)
# subscriber set and the browser — connected to the API process — never sees it.
# The trigger closes that gap: the insert bumps a counter and fires pg_notify,
# the API process turns that into a ``change`` event, and the browser refetches.
# The direct push remains the fast path when both live in one process.
#
# ``provider_credentials`` is tracked for the inverse reason: nothing polls it,
# and without an announcement nothing ever would. Decrypting an API key on every
# scoring call is wasted work, so each process caches the decrypted set — but a
# key rotated in the settings screen has to reach the Hatchet worker's cache too,
# and the worker has no reason to re-read a table that changes twice a year. The
# trigger is what turns a rotation into an eviction everywhere, which is what
# makes caching a credential safe: a revoked key stops being used within
# milliseconds rather than at the next restart.
#
# ``app_settings`` is the same story for the other half of the scoring hot path.
# The model selection, the transcription engine and the preprocess toggle are
# read on every run in every process and written a few times a year; the trigger
# is what lets ``AppSettingsRepository`` cache them while keeping the promise
# that a change in the settings screen applies to the next run everywhere.
TRACKED_TABLES: tuple[str, ...] = (
    "sessions",
    "assessment_results",
    "jobs",
    "notifications",
    "provider_credentials",
    "app_settings",
    # Operator-defined providers are the third value on the scoring hot path,
    # and the one with the widest blast radius when stale: a catalogue that has
    # not learned about an edited endpoint would keep sending this deployment's
    # key to the address the definition used to name.
    "llm_providers",
    # Accounts are read on *every* authenticated request: the bearer token
    # names a user and a token version, and the row decides whether that user
    # is still active and still on that version. Cached per process
    # (``UserDirectory``) for the same reason the credentials are, and tracked
    # for the same reason too — an admin disabling a marker has to reach every
    # API process on the marker's next request, not at their next restart.
    "users",
)


async def verify_change_tracking(
    engine: AsyncEngine,
    tables: Iterable[str] = TRACKED_TABLES,
) -> bool:
    """Validate migration-owned counters and triggers without issuing DDL.

    Missing counters/triggers are fatal: polling an unchanged counter cannot
    invalidate a cache. PostgreSQL must have enabled statement-level triggers
    invoking the expected function. SQLite needs all three row-level triggers.
    Return whether PostgreSQL LISTEN/NOTIFY is supported, not overall health.
    """
    table_list = tuple(tables)
    async with engine.connect() as connection:
        await connection.execute(select(TableVersionRecord).limit(0))
        if engine.dialect.name == "postgresql":
            rows = await connection.execute(text(
                "SELECT c.relname, t.tgname FROM pg_catalog.pg_trigger t "
                "JOIN pg_catalog.pg_class c ON c.oid = t.tgrelid "
                "JOIN pg_catalog.pg_proc p ON p.oid = t.tgfoid "
                "WHERE NOT t.tgisinternal AND t.tgenabled IN ('O', 'A') "
                "AND t.tgtype = 28 AND p.proname = 'osce_bump_table_version' "
                "AND pg_catalog.pg_table_is_visible(c.oid)"
            ))
            expected = {(table, f"trg_{table}_change") for table in table_list}
        elif engine.dialect.name == "sqlite":
            rows = await connection.execute(text(
                "SELECT tbl_name, name FROM sqlite_master WHERE type = 'trigger'"
            ))
            expected = {
                (table, f"trg_{table}_change_{operation}")
                for table in table_list for operation in ("insert", "update", "delete")
            }
        else:
            raise RuntimeError(f"Unsupported change-tracking dialect: {engine.dialect.name}")
        missing = expected - set(rows.all())
        if missing:
            names = ", ".join(sorted(name for _, name in missing))
            raise RuntimeError(f"Change tracking is incomplete: {names}. Restore the migration-owned triggers before startup.")
    return engine.dialect.name == "postgresql" and bool(table_list)


async def install_change_tracking(
    engine: AsyncEngine,
    tables: Iterable[str] = TRACKED_TABLES,
) -> bool:
    """Compatibility entry point; only Alembic may install tracking now."""
    return await verify_change_tracking(engine, tables)
