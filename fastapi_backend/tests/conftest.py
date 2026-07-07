from __future__ import annotations

import asyncio
import sys


def pytest_configure(config) -> None:
    # psycopg async requires SelectorEventLoop on Windows; pytest defaults to ProactorEventLoop.
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
