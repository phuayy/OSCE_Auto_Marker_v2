"""Read-only pre/post-deploy report for the operator deploy script.

This intentionally never migrates and never writes to the database — it is
the thing you run *before* deciding whether it is safe to deploy, and *after*
a deploy to confirm the database landed where the code expects. Building an
``OrmDatabase`` and calling ``initialize()`` would run "alembic upgrade head"
as a side effect of asking a question, so this script opens its own plain
SQLAlchemy engine instead, reusing the same URL-normalising and sync-driver
helpers the application already has (``OrmDatabase._normalize_url`` /
``migration_runner.to_sync_url``) so it can never disagree with the app about
which database it means.

Exit codes: 0 ok; 2 database unreachable or a usage error; 3 with
``--require-drained`` and something is in flight; 4 with
``--require-up-to-date`` and the database is behind (or ahead of) head.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
BACKEND_DIR = ROOT_DIR / "fastapi_backend"
sys.path.insert(0, str(BACKEND_DIR))

from app.core.asyncio_compat import configure_windows_selector_event_loop_policy  # noqa: E402
from app.core.config import settings  # noqa: E402
from app.database.db_url import DatabaseUrlError  # noqa: E402
from app.database.migration_runner import ALEMBIC_SCRIPTS, to_sync_url  # noqa: E402
from app.database.orm import OrmDatabase  # noqa: E402
from app.domain.jobs import ACTIVE_JOB_STATUSES  # noqa: E402
from app.domain.sessions import IN_FLIGHT_STATUSES  # noqa: E402

from sqlalchemy import create_engine, inspect, text  # noqa: E402

from check_database import redact_database_url  # noqa: E402

configure_windows_selector_event_loop_policy()

SCHEMA_VERSION = 1


class DeployCheckError(RuntimeError):
    """A usage or connectivity error that should exit with code 2."""


def build_report(*, require_drained: bool, require_up_to_date: bool) -> tuple[dict, int]:
    try:
        # Resolving and normalising the configured URL can itself raise
        # (DatabaseUrlError, a ValueError subclass, for an unsupported
        # scheme) — inside the try, not before it, so that failure is
        # reported the same clean way as an unreachable database rather than
        # as an uncaught traceback main() has no handler for.
        source = settings.resolved_database_source
        normalized_url = OrmDatabase._normalize_url(source)
        backend = "postgres" if normalized_url.startswith("postgresql") else "sqlite"
        sync_url = to_sync_url(normalized_url)

        source_label = str(source) if backend == "sqlite" else redact_database_url(str(source))

        engine = create_engine(sync_url, future=True)
        try:
            with engine.connect() as connection:
                inspector = inspect(connection)
                table_names = set(inspector.get_table_names())

                current: list[str] = []
                if "alembic_version" in table_names:
                    rows = connection.execute(text("SELECT version_num FROM alembic_version")).fetchall()
                    current = sorted({row[0] for row in rows})

                heads = sorted(_script_directory_heads())
                # "unknown" means a revision this checkout's script directory
                # has never heard of — a DB ahead of the code (rolled forward
                # by a newer deploy, or hand-edited), which only a restore can
                # fix. It must NOT mean "not a head": a DB stamped at an
                # older-but-real revision (upgrade interrupted, or simply not
                # yet run) is behind, not unknown, and is exactly the ordinary
                # case Deploy-Release.ps1 runs `alembic upgrade head` to fix.
                # Comparing against every revision the script directory
                # contains (not just its heads) is what tells those two apart.
                known_revisions = _script_directory_all_revisions()
                unknown = sorted(set(current) - known_revisions)
                up_to_date = set(current) == set(heads)

                in_flight_count = 0
                in_flight_ids: list[str] = []
                if "sessions" in table_names:
                    placeholders = ", ".join(f":status_{i}" for i in range(len(IN_FLIGHT_STATUSES)))
                    params = {f"status_{i}": status for i, status in enumerate(sorted(IN_FLIGHT_STATUSES))}
                    count_row = connection.execute(
                        text(f"SELECT COUNT(*) FROM sessions WHERE status IN ({placeholders})"),
                        params,
                    ).scalar_one()
                    in_flight_count = int(count_row)
                    id_rows = connection.execute(
                        text(
                            f"SELECT id FROM sessions WHERE status IN ({placeholders}) "
                            "ORDER BY created_at ASC, id ASC LIMIT 20"
                        ),
                        params,
                    ).fetchall()
                    in_flight_ids = [row[0] for row in id_rows]

                active_jobs_count = 0
                if "jobs" in table_names:
                    job_placeholders = ", ".join(f":job_status_{i}" for i in range(len(ACTIVE_JOB_STATUSES)))
                    job_params = {
                        f"job_status_{i}": status for i, status in enumerate(sorted(ACTIVE_JOB_STATUSES))
                    }
                    active_jobs_row = connection.execute(
                        text(f"SELECT COUNT(*) FROM jobs WHERE status IN ({job_placeholders})"),
                        job_params,
                    ).scalar_one()
                    active_jobs_count = int(active_jobs_row)
        finally:
            engine.dispose()
    except DeployCheckError:
        raise
    except DatabaseUrlError as exc:
        # A distinct message from "unreachable": the database may well be up
        # and reachable — the configured URL just names a scheme this app
        # cannot use. exc's own message already names the scheme, the
        # supported ones, and (redacted) the URL it saw.
        raise DeployCheckError(f"Invalid database URL: {exc}") from exc
    except Exception as exc:  # noqa: BLE001 - reported to the caller, not swallowed
        raise DeployCheckError(f"Database unreachable: {exc}") from exc

    report = {
        "schema": SCHEMA_VERSION,
        "database": {"backend": backend, "source": source_label},
        "revision": {
            "current": current,
            "heads": heads,
            "upToDate": up_to_date,
            "unknown": unknown,
        },
        "inFlight": {
            "sessions": in_flight_count,
            "sessionIds": in_flight_ids,
            "activeJobs": active_jobs_count,
        },
    }

    exit_code = 0
    if require_drained and (in_flight_count > 0 or active_jobs_count > 0):
        exit_code = 3
    if require_up_to_date and not up_to_date:
        exit_code = 4 if exit_code == 0 else exit_code

    report["ok"] = exit_code == 0
    return report, exit_code


def _script_directory_heads() -> list[str]:
    from alembic.script import ScriptDirectory

    return list(ScriptDirectory(str(ALEMBIC_SCRIPTS)).get_heads())


def _script_directory_all_revisions() -> set[str]:
    """Every revision id this checkout's script directory contains, head or
    not — what "unknown" is judged against. ``get_heads()`` alone only names
    the tip(s) of the tree, so comparing a DB's *current* revision (which is
    ordinarily well behind head until the next upgrade) against heads alone
    misreports every not-yet-upgraded revision as unknown."""
    from alembic.script import ScriptDirectory

    script_dir = ScriptDirectory(str(ALEMBIC_SCRIPTS))
    return {revision.revision for revision in script_dir.walk_revisions()}


def _print_human(report: dict) -> None:
    db = report["database"]
    revision = report["revision"]
    in_flight = report["inFlight"]
    print(f"Database backend: {db['backend']}")
    print(f"Database source: {db['source']}")
    print(f"Current revision(s): {', '.join(revision['current']) or '(none)'}")
    print(f"Script directory head(s): {', '.join(revision['heads']) or '(none)'}")
    print(f"Up to date: {revision['upToDate']}")
    if revision["unknown"]:
        print(f"WARNING: database has unknown revision(s) not in this checkout: {', '.join(revision['unknown'])}")
    print(f"In-flight sessions: {in_flight['sessions']}")
    if in_flight["sessionIds"]:
        print(f"  ids (first {len(in_flight['sessionIds'])}): {', '.join(in_flight['sessionIds'])}")
    print(f"Active jobs: {in_flight['activeJobs']}")
    print(f"OK: {report['ok']}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="Print a single JSON object on stdout.")
    parser.add_argument(
        "--require-drained",
        action="store_true",
        help="Exit 3 if any session is in flight or any job is active.",
    )
    parser.add_argument(
        "--require-up-to-date",
        action="store_true",
        help="Exit 4 if the database's current revision(s) do not equal the script directory's head(s).",
    )
    args = parser.parse_args()

    try:
        report, exit_code = build_report(
            require_drained=args.require_drained,
            require_up_to_date=args.require_up_to_date,
        )
    except DeployCheckError as exc:
        message = str(exc)
        if args.json:
            print(json.dumps({"schema": SCHEMA_VERSION, "ok": False, "error": message}))
        else:
            print(f"Error: {message}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(report))
    else:
        _print_human(report)

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
