"""Consistent database backup and restore for the operator deploy script.

SQLite uses the sqlite3 online backup API (``Connection.backup``), which is
safe against a live WAL writer — a plain file copy is not (see
docs/deployment-vm.md "Backup and data retention"). PostgreSQL shells out to
``pg_dump`` / ``pg_restore``.

Exit codes:
  0  success
  1  backup produced but failed integrity_check (SQLite only)
  2  usage error (missing --confirm on restore, missing pg_dump/pg_restore binary)
  5  SQLite restore refused because the API port is currently serving (use --force)
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urlsplit, urlunsplit

ROOT_DIR = Path(__file__).resolve().parents[1]
BACKEND_DIR = ROOT_DIR / "fastapi_backend"
sys.path.insert(0, str(BACKEND_DIR))

from app.core.asyncio_compat import configure_windows_selector_event_loop_policy  # noqa: E402
from app.core.config import server_bind_from_env, settings  # noqa: E402
from app.database.migration_runner import to_sync_url  # noqa: E402
from app.database.orm import OrmDatabase  # noqa: E402

from sqlalchemy import create_engine, inspect, text  # noqa: E402

configure_windows_selector_event_loop_policy()

try:
    from run_api import port_already_serving
except Exception:  # noqa: BLE001 - fall back to a small local probe

    def port_already_serving(host: str, port: int) -> bool:
        import socket

        probe_host = "127.0.0.1" if host in {"0.0.0.0", ""} else host
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.settimeout(1.0)
            try:
                return probe.connect_ex((probe_host, port)) == 0
            except OSError:
                return False


SCHEMA_VERSION = 1
FILENAME_PREFIX = "osce-db-"


class BackupError(RuntimeError):
    """Usage error; the CLI translates this to exit code 2."""


class MissingBinaryError(RuntimeError):
    """A required Postgres CLI tool is not on PATH; exit code 2."""


def _timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _resolve_backend_and_url() -> tuple[str, str]:
    source = settings.resolved_database_source
    normalized_url = OrmDatabase._normalize_url(source)
    backend = "postgres" if normalized_url.startswith("postgresql") else "sqlite"
    return backend, normalized_url


def _current_revision(sync_url: str) -> list[str]:
    engine = create_engine(sync_url, future=True)
    try:
        with engine.connect() as connection:
            inspector = inspect(connection)
            if "alembic_version" not in set(inspector.get_table_names()):
                return []
            rows = connection.execute(text("SELECT version_num FROM alembic_version")).fetchall()
            return sorted({row[0] for row in rows})
    finally:
        engine.dispose()


def _revision_label(revisions: list[str]) -> str:
    return "+".join(revisions) if revisions else "norev"


def libpq_url_and_password(normalized_url: str) -> tuple[str, str | None]:
    """Strip the ``+driver`` suffix SQLAlchemy needs and pull the password out.

    ``pg_dump`` / ``pg_restore`` speak libpq URLs (``postgresql://...``), not
    SQLAlchemy's ``postgresql+psycopg://`` spelling, and the password must
    travel through the ``PGPASSWORD`` environment variable rather than the
    command line, where it would show up in a process listing and in shell
    history.

    ``urlsplit``'s ``.password`` (like ``.username`` and ``.hostname``) is
    deliberately NOT percent-decoded by Python's own urllib.parse — reading it
    returns the raw substring between the colon and ``@``. That is exactly
    right for the username, which stays in the URL string this function
    builds below and reaches libpq as part of it: libpq's own URI parser
    percent-decodes each component itself, so an encoded ``@`` or ``:`` in a
    username survives the round trip correctly as long as it is left alone
    here. The password gets no such second pass — it never goes back into a
    URL, only into ``PGPASSWORD`` — so it is decoded once, here, or a
    password containing a reserved URL character (``p%40ss`` for ``p@ss``)
    reaches ``PGPASSWORD`` still percent-encoded and authentication fails.
    """
    parsed = urlsplit(normalized_url)
    scheme = parsed.scheme.split("+", 1)[0]
    password = unquote(parsed.password) if parsed.password is not None else None
    username = parsed.username or ""
    hostname = parsed.hostname or ""
    port = f":{parsed.port}" if parsed.port else ""
    userinfo = username
    if userinfo:
        userinfo = f"{userinfo}@"
    netloc = f"{userinfo}{hostname}{port}"
    libpq_url = urlunsplit((scheme, netloc, parsed.path, parsed.query, parsed.fragment))
    return libpq_url, password


def _sqlite_path(normalized_url: str) -> Path:
    sync_url = to_sync_url(normalized_url)
    prefix = "sqlite:///"
    if not sync_url.startswith(prefix):
        raise BackupError(f"Expected a sqlite:/// URL, got: {sync_url}")
    return Path(sync_url[len(prefix) :])


def backup_sqlite(dest_dir: Path) -> dict:
    normalized_url = OrmDatabase._normalize_url(settings.resolved_database_source)
    live_path = _sqlite_path(normalized_url)
    if not live_path.exists():
        raise BackupError(f"SQLite database does not exist: {live_path}")

    sync_url = to_sync_url(normalized_url)
    revision = _current_revision(sync_url)
    dest_dir.mkdir(parents=True, exist_ok=True)
    filename = f"{FILENAME_PREFIX}{_timestamp()}-{_revision_label(revision)}.sqlite3"
    dest_path = dest_dir / filename

    src_conn = sqlite3.connect(str(live_path))
    try:
        dst_conn = sqlite3.connect(str(dest_path))
        try:
            src_conn.backup(dst_conn)
        finally:
            dst_conn.close()
    finally:
        src_conn.close()

    _assert_integrity_ok(dest_path)

    return {
        "schema": SCHEMA_VERSION,
        "backend": "sqlite",
        "path": str(dest_path),
        "sizeBytes": dest_path.stat().st_size,
        "revision": revision,
        "createdAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def _assert_integrity_ok(sqlite_path: Path) -> None:
    conn = sqlite3.connect(str(sqlite_path))
    try:
        result = conn.execute("PRAGMA integrity_check").fetchone()
    finally:
        conn.close()
    verdict = result[0] if result else None
    if verdict != "ok":
        raise RuntimeError(f"Backup failed integrity_check: {verdict!r}")


def backup_postgres(dest_dir: Path) -> dict:
    pg_dump_bin = os.environ.get("PG_DUMP_BIN", "").strip() or "pg_dump"
    resolved_bin = shutil.which(pg_dump_bin)
    if resolved_bin is None:
        raise MissingBinaryError(
            f"pg_dump binary not found ({pg_dump_bin!r}); set PG_DUMP_BIN or add it to PATH."
        )

    normalized_url = OrmDatabase._normalize_url(settings.resolved_database_source)
    sync_url = to_sync_url(normalized_url)
    revision = _current_revision(sync_url)
    libpq_url, password = libpq_url_and_password(normalized_url)

    dest_dir.mkdir(parents=True, exist_ok=True)
    filename = f"{FILENAME_PREFIX}{_timestamp()}-{_revision_label(revision)}.dump"
    dest_path = dest_dir / filename

    env = dict(os.environ)
    if password:
        env["PGPASSWORD"] = password
    else:
        env.pop("PGPASSWORD", None)

    subprocess.run(
        [resolved_bin, "--format=custom", "--no-owner", f"--file={dest_path}", libpq_url],
        check=True,
        env=env,
    )

    return {
        "schema": SCHEMA_VERSION,
        "backend": "postgres",
        "path": str(dest_path),
        "sizeBytes": dest_path.stat().st_size,
        "revision": revision,
        "createdAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def do_backup(dest_dir: Path) -> dict:
    backend, _ = _resolve_backend_and_url()
    if backend == "sqlite":
        return backup_sqlite(dest_dir)
    return backup_postgres(dest_dir)


def restore_sqlite(backup_file: Path, *, force: bool) -> None:
    if not backup_file.exists():
        raise BackupError(f"Backup file does not exist: {backup_file}")

    host, port = server_bind_from_env()
    if port_already_serving(host, port) and not force:
        raise PortServingError(
            f"The API appears to be serving on {host}:{port}. Stop it before restoring, "
            "or pass --force to restore anyway."
        )

    normalized_url = OrmDatabase._normalize_url(settings.resolved_database_source)
    live_path = _sqlite_path(normalized_url)
    live_path.parent.mkdir(parents=True, exist_ok=True)

    backup_conn = sqlite3.connect(str(backup_file))
    try:
        # Connecting to the live path creates it if absent, which is fine —
        # ``backup()`` below overwrites whatever schema is there.
        live_conn = sqlite3.connect(str(live_path))
        try:
            backup_conn.backup(live_conn)
        finally:
            live_conn.close()
    finally:
        backup_conn.close()

    # Stale WAL/SHM sidecars from the pre-restore database describe pages that
    # no longer match the file we just overwrote; drop them now that both
    # connections above are closed so nothing reopens them mid-restore.
    for suffix in ("-wal", "-shm"):
        sidecar = live_path.with_name(live_path.name + suffix)
        if sidecar.exists():
            sidecar.unlink()

    _assert_integrity_ok(live_path)


def restore_postgres(backup_file: Path) -> None:
    if not backup_file.exists():
        raise BackupError(f"Backup file does not exist: {backup_file}")

    pg_restore_bin = os.environ.get("PG_RESTORE_BIN", "").strip() or "pg_restore"
    resolved_bin = shutil.which(pg_restore_bin)
    if resolved_bin is None:
        raise MissingBinaryError(
            f"pg_restore binary not found ({pg_restore_bin!r}); set PG_RESTORE_BIN or add it to PATH."
        )

    normalized_url = OrmDatabase._normalize_url(settings.resolved_database_source)
    libpq_url, password = libpq_url_and_password(normalized_url)

    env = dict(os.environ)
    if password:
        env["PGPASSWORD"] = password
    else:
        env.pop("PGPASSWORD", None)

    subprocess.run(
        [resolved_bin, "--clean", "--if-exists", "--no-owner", f"--dbname={libpq_url}", str(backup_file)],
        check=True,
        env=env,
    )


class PortServingError(RuntimeError):
    """Exit code 5: a live API is holding the SQLite file."""


def do_restore(backup_file: Path, *, force: bool) -> None:
    backend, _ = _resolve_backend_and_url()
    if backend == "sqlite":
        restore_sqlite(backup_file, force=force)
    else:
        restore_postgres(backup_file)


def prune(dest_dir: Path, days: int) -> list[Path]:
    if not dest_dir.exists():
        return []
    import time

    cutoff = time.time() - days * 86400
    removed: list[Path] = []
    for entry in sorted(dest_dir.glob(f"{FILENAME_PREFIX}*")):
        if not entry.is_file():
            continue
        if entry.stat().st_mtime < cutoff:
            entry.unlink()
            removed.append(entry)
    return removed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dest", type=Path, help="Directory to write the backup file into.")
    parser.add_argument("--restore", type=Path, help="Backup file to restore into the live database.")
    parser.add_argument("--confirm", action="store_true", help="Required alongside --restore.")
    parser.add_argument(
        "--force",
        action="store_true",
        help="SQLite restore only: proceed even if the API appears to be serving.",
    )
    parser.add_argument(
        "--prune-days",
        type=int,
        default=None,
        help="With --dest: delete backup files in dest older than this many days.",
    )
    args = parser.parse_args()

    if args.restore is not None:
        if not args.confirm:
            print(
                "Refusing to restore without --confirm. This overwrites the live database; "
                "pass --confirm to proceed (and --force if the API is still serving a SQLite database).",
                file=sys.stderr,
            )
            return 2
        try:
            do_restore(args.restore, force=args.force)
        except PortServingError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 5
        except MissingBinaryError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 2
        except BackupError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 2
        print(json.dumps({"schema": SCHEMA_VERSION, "ok": True, "restored": str(args.restore)}))
        return 0

    if args.dest is None:
        print("Error: --dest is required unless --restore is given.", file=sys.stderr)
        return 2

    try:
        result = do_backup(args.dest)
    except MissingBinaryError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    except BackupError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    except RuntimeError as exc:
        # Backup produced but failed integrity_check.
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(result))

    if args.prune_days is not None:
        removed = prune(args.dest, args.prune_days)
        for entry in removed:
            print(f"Pruned: {entry}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
