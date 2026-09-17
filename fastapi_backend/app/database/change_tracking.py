from __future__ import annotations

import logging
from typing import Iterable

from sqlalchemy.ext.asyncio import AsyncEngine


logger = logging.getLogger(__name__)


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

_VERSION_TABLE = "table_versions"

# ---------------------------------------------------------------------------
# PostgreSQL
#
# One statement-level trigger per table (NOT row-level): a run that inserts 40
# assessment_criteria rows should announce one change, not forty. The function
# bumps the counter and fires pg_notify in the same statement; Postgres holds
# the notification until COMMIT, so a listener never observes a change before
# the data backing it is visible.
# ---------------------------------------------------------------------------

_PG_FUNCTION = f"""
CREATE OR REPLACE FUNCTION osce_bump_table_version() RETURNS trigger AS $osce$
DECLARE
    next_version BIGINT;
BEGIN
    INSERT INTO {_VERSION_TABLE} (table_name, version, updated_at)
    VALUES (TG_TABLE_NAME, 1, now())
    ON CONFLICT (table_name)
    DO UPDATE SET version = {_VERSION_TABLE}.version + 1, updated_at = now()
    RETURNING version INTO next_version;

    PERFORM pg_notify(
        '{CHANGE_CHANNEL}',
        json_build_object('table', TG_TABLE_NAME, 'version', next_version, 'op', TG_OP)::text
    );
    RETURN NULL;
END;
$osce$ LANGUAGE plpgsql;
"""


def _pg_trigger_statements(table: str) -> list[str]:
    trigger = f"trg_{table}_change"
    return [
        f"DROP TRIGGER IF EXISTS {trigger} ON {table}",
        (
            f"CREATE TRIGGER {trigger} "
            f"AFTER INSERT OR UPDATE OR DELETE ON {table} "
            f"FOR EACH STATEMENT EXECUTE FUNCTION osce_bump_table_version()"
        ),
    ]


# ---------------------------------------------------------------------------
# SQLite (tests + the no-Postgres fallback)
#
# SQLite has no statement-level triggers and no NOTIFY, so this degrades to
# row-level counter bumps. Consumers then learn about changes by reading the
# counter instead of being pushed to — same correctness, poll-bound latency.
# ---------------------------------------------------------------------------

_SQLITE_BUMP = f"""
    INSERT INTO {_VERSION_TABLE} (table_name, version, updated_at)
    VALUES ('{{table}}', 1, CURRENT_TIMESTAMP)
    ON CONFLICT(table_name)
    DO UPDATE SET version = {_VERSION_TABLE}.version + 1, updated_at = CURRENT_TIMESTAMP;
"""


def _sqlite_trigger_statements(table: str) -> list[str]:
    statements: list[str] = []
    for operation in ("INSERT", "UPDATE", "DELETE"):
        trigger = f"trg_{table}_change_{operation.lower()}"
        statements.append(f"DROP TRIGGER IF EXISTS {trigger}")
        statements.append(
            f"CREATE TRIGGER {trigger} AFTER {operation} ON {table} "
            f"BEGIN {_SQLITE_BUMP.format(table=table)} END"
        )
    return statements


async def install_change_tracking(
    engine: AsyncEngine,
    tables: Iterable[str] = TRACKED_TABLES,
) -> bool:
    """Create (or refresh) the change-tracking triggers on ``tables``.

    Must run *after* both schema initialisers, because it attaches triggers to
    tables owned by two different layers: ``sessions``/``assessment_results``/
    ``notifications`` come from the SQLAlchemy metadata, ``jobs`` from the
    raw-SQL jobs schema.

    Idempotent — safe to run on every boot. Returns True when the Postgres path
    (counter + LISTEN/NOTIFY) is active, False for the SQLite counter-only path.

    Each table is installed in its **own** transaction. Batching them was a trap:
    one absent table (``jobs``, when the raw-SQL initialiser has not run) aborted
    the single enclosing transaction and rolled back the triggers for every other
    table too — so change tracking silently switched off wholesale, leaving one
    log line as the only evidence and the browser back on polling. Isolating them
    means a partial schema costs only the tables actually missing.

    Failure is never fatal: an untracked table's consumers fall back to polling,
    which is the behaviour that predates this module.
    """
    is_postgres = engine.dialect.name == "postgresql"
    table_list = list(tables)

    if is_postgres:
        # The shared trigger function must exist before any trigger references
        # it; without it nothing can be installed, so this failure is terminal.
        try:
            async with engine.begin() as connection:
                # exec_driver_sql bypasses SQLAlchemy's bind-parameter parsing,
                # which would otherwise choke on plpgsql's dollar-quoting.
                await connection.exec_driver_sql(_PG_FUNCTION)
        except Exception:
            logger.exception(
                "Could not create the change-tracking trigger function; "
                "falling back to polled change detection."
            )
            return False

    installed: list[str] = []
    failed: list[str] = []
    for table in table_list:
        statements = (
            _pg_trigger_statements(table) if is_postgres else _sqlite_trigger_statements(table)
        )
        try:
            async with engine.begin() as connection:
                for statement in statements:
                    await connection.exec_driver_sql(statement)
            installed.append(table)
        except Exception as error:
            failed.append(table)
            logger.warning(
                "Could not install change tracking on '%s' (%s); consumers of that "
                "table fall back to polling.",
                table,
                error,
            )

    if failed:
        logger.warning(
            "Change tracking is partial: active on %s; missing on %s.",
            ", ".join(installed) or "nothing",
            ", ".join(failed),
        )
    elif installed:
        logger.info(
            "Change tracking installed on %s (%s).",
            ", ".join(installed),
            "postgres LISTEN/NOTIFY" if is_postgres else "sqlite counters",
        )

    # Push is only meaningful when at least one table can actually announce.
    return is_postgres and bool(installed)
