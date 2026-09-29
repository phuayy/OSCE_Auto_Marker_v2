from __future__ import annotations

import asyncio
import sys
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
BACKEND_DIR = ROOT_DIR / "fastapi_backend"
sys.path.insert(0, str(BACKEND_DIR))

from app.core.asyncio_compat import configure_windows_selector_event_loop_policy  # noqa: E402
from app.core.config import settings  # noqa: E402
from app.database.db_url import redact_database_url  # noqa: E402, F401 -- re-exported for scripts/deploy_check.py
from app.database.orm import OrmDatabase  # noqa: E402


configure_windows_selector_event_loop_policy()


async def main() -> None:
    source = settings.resolved_database_source
    orm_database = OrmDatabase(source)
    backend = "postgres" if orm_database.url.startswith("postgresql") else "sqlite"

    try:
        await orm_database.initialize()
    finally:
        await orm_database.shutdown()

    source_label = str(source) if backend == "sqlite" else redact_database_url(str(source))
    print(f"Database backend: {backend}")
    print(f"Database source: {source_label}")
    print("Database initialization: ok")


if __name__ == "__main__":
    asyncio.run(main())
