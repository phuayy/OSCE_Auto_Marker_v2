#!/usr/bin/env python3
"""
Hard-reset the OSCE AI Marker databases and volatile storage for a fresh start.

What is cleared:
  - PostgreSQL: all rows in every ORM and raw-SQL table (TRUNCATE CASCADE)
  - SQLite:     the local database file is deleted
  - Storage:    sessions/, uploads/, jobs/ directories are emptied

What is preserved:
  - storage/auth/          (login credentials, secrets, rubric JSON)
  - storage/input/         (reference bell-sample audio)
  - storage/output/        (score JSON files - cleared only with --all)

Usage:
    python scripts/reset_db.py            # safe reset
    python scripts/reset_db.py --all      # also clear output scores
    python scripts/reset_db.py --yes      # skip confirmation prompt
"""
from __future__ import annotations

import argparse
import asyncio
import shutil
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
BACKEND_DIR = ROOT_DIR / "fastapi_backend"
sys.path.insert(0, str(BACKEND_DIR))

from app.core.asyncio_compat import configure_windows_selector_event_loop_policy  # noqa: E402

configure_windows_selector_event_loop_policy()

# Tables in dependency order (children before parents) so TRUNCATE CASCADE is clean.
PG_TABLES = [
    "assessment_criteria",
    "assessment_results",
    "assessment_sessions",
    "students",
    "examiners",
    "source_videos",
    "rubric_assets",
    "sessions",
    "job_events",
    "job_attempts",
    "jobs",
    "app_metadata",
]


async def reset_postgres(url: str) -> None:
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy import text

    pg_url = url.replace("postgresql://", "postgresql+psycopg://", 1)
    if not pg_url.startswith("postgresql+psycopg://"):
        pg_url = "postgresql+psycopg://" + pg_url.split("://", 1)[-1]

    engine = create_async_engine(pg_url, pool_pre_ping=True, connect_args={"prepare_threshold": None})
    async with engine.begin() as conn:
        # Postgres TRUNCATE has no IF EXISTS, so filter to tables that exist,
        # then truncate them in one statement (CASCADE handles FK order).
        existing: list[str] = []
        for table in PG_TABLES:
            found = await conn.scalar(text("SELECT to_regclass(:qualified)"), {"qualified": f"public.{table}"})
            if found is not None:
                existing.append(table)
        if existing:
            table_list = ", ".join(f'"{name}"' for name in existing)
            await conn.execute(text(f"TRUNCATE TABLE {table_list} RESTART IDENTITY CASCADE"))
    await engine.dispose()
    print(f"  PostgreSQL: truncated {len(existing)} of {len(PG_TABLES)} table(s).")


def reset_sqlite(path: Path) -> None:
    if path.exists():
        path.unlink()
        print(f"  SQLite: deleted {path.relative_to(ROOT_DIR)}")
    else:
        print("  SQLite: file not found, nothing to delete.")


def clear_directory(directory: Path, label: str) -> None:
    if not directory.exists():
        print(f"  {label}: directory not found, skipping.")
        return
    count = 0
    for item in directory.iterdir():
        if item.is_file():
            item.unlink()
            count += 1
        elif item.is_dir():
            shutil.rmtree(item)
            count += 1
    print(f"  {label}: removed {count} item(s) from {directory.relative_to(ROOT_DIR)}")


async def run(*, clear_all: bool) -> None:
    from app.core.config import Settings

    settings = Settings.load()
    db_source = settings.resolved_database_source

    is_postgres = isinstance(db_source, str) and str(db_source).startswith(("postgres", "postgresql"))
    sqlite_path = settings.paths.database_path

    print()
    print("Resetting databases...")

    if is_postgres:
        await reset_postgres(str(db_source))
    else:
        print("  PostgreSQL: not configured (APP_DATABASE_URL not set), skipping.")

    reset_sqlite(sqlite_path)

    print()
    print("Clearing volatile storage...")
    paths = settings.paths
    clear_directory(paths.sessions_dir, "sessions")
    clear_directory(paths.uploads_dir, "uploads")
    clear_directory(paths.jobs_dir, "jobs")

    if clear_all:
        print()
        print("Clearing output scores (--all)...")
        clear_directory(paths.output_scores_dir, "scores")
        clear_directory(paths.output_communication_scores_dir, "communication-scores")
        clear_directory(paths.output_whisperx_dir, "whisperx")
        clear_directory(paths.output_transcripts_dir, "transcripts")
        clear_directory(paths.output_audio_dir, "audio")
        clear_directory(paths.output_clips_dir, "clips")

    print()
    # Alembic owns the schema. The API applies migrations at startup unless
    # DB_AUTO_MIGRATE=false, in which case migrate as a deliberate step.
    print("Done. Start the API (npm run dev:api), or migrate explicitly:")
    print("  cd fastapi_backend && uv run alembic upgrade head")


def main() -> None:
    parser = argparse.ArgumentParser(description="Reset OSCE AI Marker databases for a fresh start.")
    parser.add_argument("--all", action="store_true", help="Also clear output score files.")
    parser.add_argument("--yes", action="store_true", help="Skip confirmation prompt.")
    args = parser.parse_args()

    if not args.yes:
        print("WARNING: This will permanently delete all sessions, jobs, and uploaded data.")
        confirm = input("Type 'yes' to continue: ").strip().lower()
        if confirm != "yes":
            print("Aborted.")
            sys.exit(0)

    asyncio.run(run(clear_all=args.all))


if __name__ == "__main__":
    main()
