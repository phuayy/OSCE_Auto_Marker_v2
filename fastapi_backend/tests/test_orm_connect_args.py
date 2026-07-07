from __future__ import annotations

from app.database.orm import OrmDatabase, _SQLITE_BUSY_TIMEOUT_SECONDS


def test_sqlite_connect_args_set_busy_timeout() -> None:
    args = OrmDatabase._connect_args("sqlite+aiosqlite:///C:/tmp/x.sqlite3")
    assert args == {"timeout": _SQLITE_BUSY_TIMEOUT_SECONDS}


def test_postgres_connect_args_disable_prepared_statements() -> None:
    args = OrmDatabase._connect_args("postgresql+psycopg://user:pw@localhost/db")
    assert args == {"prepare_threshold": None}
