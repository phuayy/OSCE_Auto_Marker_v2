from __future__ import annotations

import asyncio
import sys

import pytest


def pytest_configure(config) -> None:
    # psycopg async requires SelectorEventLoop on Windows; pytest defaults to ProactorEventLoop.
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


@pytest.fixture(autouse=True)
def dispose_orm_engines():
    """Dispose every database engine a test creates, before the next test runs.

    Most tests here drive async code with ``asyncio.run(scenario())`` and leave
    their ``OrmDatabase`` to the garbage collector. The collector then finalises
    an aiosqlite ``Connection`` after that loop is closed, and its ``__del__``
    asks for an event loop that no longer exists — the
    ``PytestUnraisableExceptionWarning`` the suite used to end on. The suite
    passed, but those warnings were the opposite of evidence for clean teardown:
    connections were being reclaimed by chance, at an arbitrary later point.

    Disposal is done here rather than test by test because the discipline has to
    hold for tests not yet written. ``OrmDatabase.__init__`` is wrapped for the
    duration of the test so every engine is known, including the ones built
    deep inside an ``AppContainer``; disposing an engine a test already shut
    down is a no-op.
    """
    from app.database.orm import OrmDatabase

    created: list[OrmDatabase] = []
    original_init = OrmDatabase.__init__

    def tracking_init(self, *args, **kwargs) -> None:
        original_init(self, *args, **kwargs)
        created.append(self)

    OrmDatabase.__init__ = tracking_init
    try:
        yield
    finally:
        OrmDatabase.__init__ = original_init
        if created:
            asyncio.run(_dispose_all(created))


async def _dispose_all(databases: list) -> None:
    for database in databases:
        try:
            await database.shutdown()
        except Exception:  # noqa: BLE001 - teardown must not fail a passing test
            pass
