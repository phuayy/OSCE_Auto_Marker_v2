"""Run the Alembic migrations from inside the application at startup.

The schema used to be produced entirely at boot: ``Base.metadata.create_all``
for the ORM tables, ``CREATE TABLE IF NOT EXISTS`` for the raw-SQL jobs tables,
``apply_additive_migrations`` for columns added after the fact, and
``install_change_tracking`` for the trigger-maintained version counters. That
works, but it has no record of *what* has been applied -- so nothing can be
rolled back, nothing can be reviewed in a diff, and a column drop or a type
change has no way to happen at all.

Alembic now owns the schema; this module is the bridge that keeps
``python scripts/run_api.py`` a single command rather than "remember to run
``alembic upgrade head`` first". Operators who prefer to migrate deliberately
can set ``DB_AUTO_MIGRATE=false`` and run the CLI themselves.

Three cases, distinguished by what is already in the database:

* **alembic_version present** -- an Alembic-managed database. Upgrade to head.
* **application tables present, no alembic_version** -- a database built by the
  old ``create_all`` path. Stamp it at ``0001`` (the baseline that describes
  what it already has) and then upgrade, so any revision after the baseline
  still gets applied. Stamping straight at ``head`` would skip them silently,
  which is the classic way a pre-Alembic database ends up permanently missing a
  column.
* **empty database** -- upgrade from scratch.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from sqlalchemy import create_engine, inspect


logger = logging.getLogger(__name__)


# fastapi_backend/ — holds alembic.ini and the alembic/ script directory.
BACKEND_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI = BACKEND_ROOT / "alembic.ini"
ALEMBIC_SCRIPTS = BACKEND_ROOT / "alembic"

# The revision describing the schema the pre-Alembic startup path produced.
BASELINE_REVISION = "0001"

# Any one of these existing means the database predates Alembic rather than
# being empty. ``sessions`` is the table every deployment has.
_SENTINEL_TABLE = "sessions"
_VERSION_TABLE = "alembic_version"


def to_sync_url(url: str) -> str:
    """Swap the async driver for its blocking equivalent.

    Alembic migrates over a synchronous connection. ``sqlite+aiosqlite`` has to
    become plain ``sqlite``; ``postgresql+psycopg`` needs no change, because
    psycopg 3 is both the sync and the async driver.
    """
    if url.startswith("sqlite+aiosqlite://"):
        return url.replace("sqlite+aiosqlite://", "sqlite://", 1)
    return url


def _build_config(sync_url: str):
    from alembic.config import Config

    config = Config(str(ALEMBIC_INI))
    # Absolute, because the app's working directory is the repository root while
    # alembic.ini's relative script_location is written for fastapi_backend/.
    config.set_main_option("script_location", str(ALEMBIC_SCRIPTS))
    config.set_main_option("sqlalchemy.url", sync_url)
    # Keep env.py's fileConfig() out of the way. It reconfigures the root logger
    # from alembic.ini, which would undo the levels and handlers the app has
    # already installed — and, with logging's default, switch off every logger
    # the ini file does not name.
    config.attributes["configure_logger"] = False
    return config


def _upgrade_sync(sync_url: str) -> str:
    from alembic import command

    config = _build_config(sync_url)

    engine = create_engine(sync_url, future=True)
    try:
        with engine.connect() as connection:
            inspector = inspect(connection)
            has_version_table = inspector.has_table(_VERSION_TABLE)
            has_application_tables = inspector.has_table(_SENTINEL_TABLE)
    finally:
        engine.dispose()

    if not has_version_table and has_application_tables:
        logger.info(
            "Database predates Alembic; stamping it at the %s baseline before upgrading.",
            BASELINE_REVISION,
        )
        command.stamp(config, BASELINE_REVISION)
        action = "adopted"
    elif has_version_table:
        action = "upgraded"
    else:
        action = "created"

    command.upgrade(config, "head")
    return action


async def run_database_migrations(database_source: Path | str) -> str | None:
    """Bring ``database_source`` up to the head revision.

    ``database_source`` is whatever ``Settings.resolved_database_source`` gives:
    a SQLite path or a PostgreSQL URL. It is normalised through ``OrmDatabase``
    so the migration and the application can never disagree about which database
    they mean.

    Returns the action taken (``"created"`` / ``"adopted"`` / ``"upgraded"``), or
    ``None`` when Alembic is not installed.

    A migration failure is re-raised. Unlike change tracking -- where the
    fallback is merely slower -- an unmigrated schema makes requests fail at
    run time, and the one moment an operator is watching is boot.
    """
    try:
        import alembic  # noqa: F401
    except ImportError:
        logger.warning(
            "Alembic is not installed, so migrations were skipped; the schema will "
            "fall back to create_all plus the additive-migration pass. Install it "
            "with 'uv sync' to manage the schema properly."
        )
        return None

    from app.database.orm import OrmDatabase

    sync_url = to_sync_url(OrmDatabase._normalize_url(database_source))
    action = await asyncio.to_thread(_upgrade_sync, sync_url)
    logger.info("Database schema %s via Alembic (head revision applied).", action)
    return action


__all__ = [
    "ALEMBIC_INI",
    "ALEMBIC_SCRIPTS",
    "BASELINE_REVISION",
    "run_database_migrations",
    "to_sync_url",
]
