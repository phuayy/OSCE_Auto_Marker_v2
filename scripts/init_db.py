#!/usr/bin/env python3
"""Initialize all database tables for the OSCE AI Marker application.

Creates tables for both the raw-SQL layer (jobs, job_attempts, job_events,
app_metadata) and the ORM layer (sessions, rubric_assets, assessment_sessions,
assessment_results, assessment_criteria, students, examiners).

Safe to re-run: uses CREATE TABLE IF NOT EXISTS / create_all, so existing
tables and data are never dropped or modified.

Usage:
    python scripts/init_db.py
    python scripts/init_db.py --url postgresql://user:pass@host:5432/dbname
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
BACKEND_DIR = ROOT_DIR / "fastapi_backend"
sys.path.insert(0, str(BACKEND_DIR))

from app.core.asyncio_compat import configure_windows_selector_event_loop_policy  # noqa: E402

configure_windows_selector_event_loop_policy()


async def run(database_url: str | None) -> None:
    from app.core.config import Settings
    from app.database import Database
    from app.database.orm import OrmDatabase

    settings = Settings.load()
    url = database_url or settings.resolved_database_source
    backend = "postgresql" if str(url).startswith(("postgres", "postgresql")) else "sqlite"

    print(f"Target: {backend.upper()}")
    if backend == "sqlite":
        print(f"  path: {url}")
    else:
        import re
        safe = re.sub(r":([^@/]+)@", ":***@", str(url))
        print(f"  url:  {safe}")

    print()
    print("Initializing raw-SQL schema (jobs, job_attempts, job_events, app_metadata)...")
    db = Database(url)
    await db.initialize()
    print("  done.")

    print("Initializing ORM schema (sessions, rubric_assets, assessment_*, students, examiners)...")
    orm = OrmDatabase(url)
    await orm.initialize()
    await orm.shutdown()
    print("  done.")

    print()
    print("All tables are ready.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Initialize OSCE AI Marker database tables.")
    parser.add_argument(
        "--url",
        default=None,
        help="Database URL (overrides APP_DATABASE_URL / DATABASE_URL from .env).",
    )
    args = parser.parse_args()
    asyncio.run(run(args.url))


if __name__ == "__main__":
    main()
