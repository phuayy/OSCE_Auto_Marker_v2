from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit


ROOT_DIR = Path(__file__).resolve().parents[1]
BACKEND_DIR = ROOT_DIR / "fastapi_backend"
sys.path.insert(0, str(BACKEND_DIR))

from app.core.asyncio_compat import configure_windows_selector_event_loop_policy  # noqa: E402
from app.core.config import settings  # noqa: E402
from app.database import Database  # noqa: E402
from app.database.orm import OrmDatabase  # noqa: E402


configure_windows_selector_event_loop_policy()


def redact_database_url(url: str) -> str:
    parsed = urlsplit(url)
    if not parsed.password:
        return url

    username = parsed.username or ""
    hostname = parsed.hostname or ""
    passwordless_auth = f"{username}:***@" if username else ""
    port = f":{parsed.port}" if parsed.port else ""
    netloc = f"{passwordless_auth}{hostname}{port}"
    return urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment))


async def main() -> None:
    source = settings.resolved_database_source
    database = Database(source)
    orm_database = OrmDatabase(source)

    try:
        await database.initialize()
        await orm_database.initialize()
    finally:
        await orm_database.shutdown()

    source_label = str(source) if database.backend == "sqlite" else redact_database_url(str(source))
    print(f"Database backend: {database.backend}")
    print(f"Database source: {source_label}")
    print("Database initialization: ok")


if __name__ == "__main__":
    asyncio.run(main())
