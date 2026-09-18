from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from app.database.migration_runner import migrate_engine, verify_database_revision


# Without an explicit timeout the engine falls back to sqlite3's 5 s default,
# and a writer that has to wait longer than that surfaces a transient
# "database is locked" instead of waiting its turn. SQLite serialises writers,
# and this process has several — the pipeline, the queue, an export job and a
# user renaming things in the browser — so waiting is the normal case, not a
# sign of trouble. (This used to also have to cover a second connection layer
# for the jobs queue, which set the same PRAGMA of its own; that layer is gone.)
_SQLITE_BUSY_TIMEOUT_SECONDS = 30


def configure_sqlite_connection(dbapi_connection, _connection_record) -> None:
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA journal_mode=WAL")
        mode = cursor.fetchone()[0]
        if mode not in {"wal", "memory"}:
            raise RuntimeError(f"SQLite could not enable WAL (journal_mode={mode}).")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA foreign_keys")
        if cursor.fetchone()[0] != 1:
            raise RuntimeError("SQLite foreign-key enforcement could not be enabled.")
    finally:
        cursor.close()


class OrmDatabase:
    def __init__(self, database_url_or_path: Path | str, *, auto_migrate: bool = True) -> None:
        self.url = self._normalize_url(database_url_or_path)
        self.engine: AsyncEngine = create_async_engine(
            self.url,
            pool_pre_ping=True,
            connect_args=self._connect_args(self.url),
            future=True,
        )
        if self.engine.dialect.name == "sqlite":
            event.listen(self.engine.sync_engine, "connect", configure_sqlite_connection)
        self._auto_migrate = auto_migrate
        self._initialize_lock = asyncio.Lock()
        self.session_factory = async_sessionmaker(
            self.engine,
            expire_on_commit=False,
            autoflush=False,
        )
        self._initialized = False

    async def initialize(self, *, migrate: bool | None = None) -> None:
        if self._initialized:
            return
        async with self._initialize_lock:
            if self._initialized:
                return
            should_migrate = self._auto_migrate if migrate is None else migrate
            if should_migrate:
                await migrate_engine(self.engine)
            else:
                await verify_database_revision(self.engine)
            self._initialized = True

    async def shutdown(self) -> None:
        await self.engine.dispose()

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        await self.initialize()
        async with self.session_factory() as session:
            try:
                yield session
            finally:
                await session.close()

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[AsyncSession]:
        await self.initialize()
        async with self.session_factory() as session:
            try:
                async with session.begin():
                    yield session
            finally:
                await session.close()

    @classmethod
    def _normalize_url(cls, database_url_or_path: Path | str) -> str:
        if isinstance(database_url_or_path, Path):
            return cls._sqlite_url(database_url_or_path)

        raw = str(database_url_or_path).strip()
        if not raw:
            raise ValueError("Database URL must not be empty.")

        parsed = urlparse(raw)
        if parsed.scheme in {"postgres", "postgresql"}:
            return raw.replace(f"{parsed.scheme}://", "postgresql+psycopg://", 1)
        if parsed.scheme == "postgresql+psycopg":
            return raw
        if parsed.scheme == "sqlite":
            return raw.replace("sqlite://", "sqlite+aiosqlite://", 1)
        if parsed.scheme == "sqlite+aiosqlite":
            return raw
        return cls._sqlite_url(Path(raw).expanduser())

    @staticmethod
    def _connect_args(url: str) -> dict[str, object]:
        if url.startswith("postgresql+psycopg://"):
            return {"prepare_threshold": None}
        if url.startswith("sqlite"):
            return {"timeout": _SQLITE_BUSY_TIMEOUT_SECONDS}
        return {}

    @staticmethod
    def _sqlite_url(path: Path) -> str:
        resolved = path.expanduser().resolve()
        return f"sqlite+aiosqlite:///{resolved.as_posix()}"
