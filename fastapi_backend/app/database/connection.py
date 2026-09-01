from __future__ import annotations

import asyncio
import contextlib
import logging
import sqlite3
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeVar
from urllib.parse import urlparse

from app.database.schema import POSTGRES_SCHEMA_STATEMENTS, SQLITE_SCHEMA_STATEMENTS, SCHEMA_VERSION


logger = logging.getLogger(__name__)

T = TypeVar("T")

# Pool bounds for the raw-SQL jobs layer. Small on purpose: this layer issues
# short single-statement operations, and the SQLAlchemy engine keeps its own
# separate pool for the ORM side.
_DEFAULT_POOL_MIN_SIZE = 1
_DEFAULT_POOL_MAX_SIZE = 8


class Database:
    def __init__(
        self,
        database_url_or_path: Path | str,
        *,
        pool_min_size: int = _DEFAULT_POOL_MIN_SIZE,
        pool_max_size: int = _DEFAULT_POOL_MAX_SIZE,
    ) -> None:
        self.database_url_or_path = database_url_or_path
        self.pool_min_size = max(0, pool_min_size)
        self.pool_max_size = max(1, pool_max_size)
        self._pool: Any | None = None
        self._pool_unavailable = False
        self._pool_lock = threading.Lock()
        self.database_url = self._normalize_url(database_url_or_path)
        self.backend = self._detect_backend(database_url_or_path)
        self.database_path = self._sqlite_path(database_url_or_path) if self.backend == "sqlite" else None
        self._initialized = False
        self._init_lock = asyncio.Lock()

    async def initialize(self) -> None:
        async with self._init_lock:
            if self._initialized:
                return
            await asyncio.to_thread(self._initialize_sync)
            self._initialized = True

    async def run(self, operation: Callable[[Any], T], *, write: bool = False) -> T:
        await self.initialize()

        def _run() -> T:
            if self.backend == "postgres":
                return self._run_postgres(operation)
            return self._run_sqlite(operation, write=write)

        return await asyncio.to_thread(_run)

    def _initialize_sync(self) -> None:
        if self.backend == "postgres":
            self._initialize_postgres_sync()
            return
        self._initialize_sqlite_sync()

    def _connect(self) -> sqlite3.Connection:
        if self.database_path is None:
            raise RuntimeError("SQLite database path is not configured.")
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    def _initialize_sqlite_sync(self) -> None:
        if self.database_path is None:
            raise RuntimeError("SQLite database path is not configured.")
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            for statement in SQLITE_SCHEMA_STATEMENTS:
                connection.execute(statement)
            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def _initialize_postgres_sync(self) -> None:
        psycopg, dict_row = self._load_psycopg()
        with psycopg.connect(self.database_url, row_factory=dict_row) as connection:
            for statement in POSTGRES_SCHEMA_STATEMENTS:
                connection.execute(statement)
            connection.execute(
                """
                INSERT INTO app_metadata (key, value)
                VALUES ('schema_version', %s)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = CURRENT_TIMESTAMP
                """,
                (str(SCHEMA_VERSION),),
            )

    def _run_sqlite(self, operation: Callable[[sqlite3.Connection], T], *, write: bool) -> T:
        connection = self._connect()
        try:
            # sqlite3's context manager commits or rolls back — it does **not**
            # close. Relying on it alone leaked a file handle (and the three
            # PRAGMAs' worth of setup) per operation, reclaimed only whenever the
            # garbage collector happened to run.
            with connection:
                if write:
                    connection.execute("BEGIN IMMEDIATE")
                return operation(connection)
        finally:
            connection.close()

    def _run_postgres(self, operation: Callable[[Any], T]) -> T:
        pool = self._postgres_pool()
        if pool is None:
            # No pool available: one connection per operation. Correct, but each
            # call pays a full TCP + TLS + auth handshake, so install
            # ``psycopg[pool]`` for any deployment under real load.
            psycopg, dict_row = self._load_psycopg()
            with psycopg.connect(self.database_url, row_factory=dict_row) as connection:
                return operation(PostgresConnectionAdapter(connection))
        with pool.connection() as connection:
            with connection.transaction():
                return operation(PostgresConnectionAdapter(connection))

    def _postgres_pool(self) -> Any | None:
        """A lazily-created connection pool, or None when pooling is unavailable.

        The jobs layer runs a query per claim, per event append and per status
        transition, so connecting per operation makes the handshake the dominant
        cost and walks the server's ``max_connections`` up under concurrency.
        Pooling is therefore the production path; the unpooled fallback exists so
        a deployment without ``psycopg_pool`` still works rather than failing to
        start.
        """
        if self._pool is not None or self._pool_unavailable:
            return self._pool
        with self._pool_lock:
            if self._pool is not None or self._pool_unavailable:
                return self._pool
            try:
                from psycopg.rows import dict_row
                from psycopg_pool import ConnectionPool

                self._pool = ConnectionPool(
                    self.database_url,
                    min_size=self.pool_min_size,
                    max_size=self.pool_max_size,
                    kwargs={"row_factory": dict_row},
                    open=True,
                )
            except Exception:
                self._pool_unavailable = True
                logger.warning(
                    "psycopg_pool is unavailable; opening a new PostgreSQL connection per "
                    "operation. Install 'psycopg[binary,pool]' for pooled connections.",
                    exc_info=True,
                )
                return None
        return self._pool

    def close(self) -> None:
        """Release the pool. Safe to call when one was never created."""
        pool = self._pool
        self._pool = None
        if pool is not None:
            with contextlib.suppress(Exception):
                pool.close()

    @staticmethod
    def _detect_backend(database_url_or_path: Path | str) -> str:
        if isinstance(database_url_or_path, Path):
            return "sqlite"
        parsed = urlparse(str(database_url_or_path))
        if parsed.scheme in {"postgres", "postgresql", "postgresql+psycopg"}:
            return "postgres"
        return "sqlite"

    @staticmethod
    def _normalize_url(database_url_or_path: Path | str) -> str:
        raw = str(database_url_or_path).strip()
        parsed = urlparse(raw)
        if parsed.scheme == "postgresql+psycopg":
            return raw.replace("postgresql+psycopg://", "postgresql://", 1)
        if parsed.scheme == "postgres":
            return raw.replace("postgres://", "postgresql://", 1)
        return raw

    @staticmethod
    def _sqlite_path(database_url_or_path: Path | str) -> Path:
        if isinstance(database_url_or_path, Path):
            return database_url_or_path
        raw = str(database_url_or_path).strip()
        if raw.startswith("sqlite:///"):
            return Path(raw.removeprefix("sqlite:///")).expanduser()
        if raw.startswith("sqlite://"):
            return Path(raw.removeprefix("sqlite://")).expanduser()
        return Path(raw).expanduser()

    @staticmethod
    def _load_psycopg() -> tuple[Any, Any]:
        try:
            import psycopg
            from psycopg.rows import dict_row
        except Exception as error:
            raise RuntimeError(
                "PostgreSQL database support requires psycopg. "
                "Install project dependencies from requirements.txt."
            ) from error
        return psycopg, dict_row


class PostgresConnectionAdapter:
    def __init__(self, connection: Any) -> None:
        self.connection = connection

    def execute(self, sql: str, parameters: tuple[Any, ...] | list[Any] | None = None) -> Any:
        return self.connection.execute(self._translate_sql(sql), parameters)

    @classmethod
    def _translate_sql(cls, sql: str) -> str:
        return cls._replace_sqlite_placeholders(sql)

    @staticmethod
    def _replace_sqlite_placeholders(sql: str) -> str:
        output: list[str] = []
        in_single_quote = False
        in_double_quote = False
        index = 0
        while index < len(sql):
            char = sql[index]
            if char == "'" and not in_double_quote:
                in_single_quote = not in_single_quote
                output.append(char)
            elif char == '"' and not in_single_quote:
                in_double_quote = not in_double_quote
                output.append(char)
            elif char == "?" and not in_single_quote and not in_double_quote:
                output.append("%s")
            else:
                output.append(char)
            index += 1
        return "".join(output)
