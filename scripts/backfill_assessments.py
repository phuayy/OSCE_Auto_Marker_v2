#!/usr/bin/env python3
"""
Re-persist assessment results for every stored session using the CURRENT
score-mapping code in AssessmentService.

Why: assessment_results/assessment_criteria rows written by older mapping code
can have NULL score/label/passed columns even though the score JSON on disk
(or in session.outputs payload) has the data. record_session_results() is an
idempotent upsert (criteria are delete+reinsert), so re-running it repairs
those rows in place.

Usage:
    python scripts/backfill_assessments.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR / "fastapi_backend"))

from app.core.asyncio_compat import configure_windows_selector_event_loop_policy  # noqa: E402

configure_windows_selector_event_loop_policy()

OUTPUT_KEYS = ("scores", "communicationScores", "audioProfessionalism")


async def run() -> None:
    from app.core.config import Settings
    from app.database.orm import OrmDatabase
    from app.repositories.assessment_repository import AssessmentRepository
    from app.repositories.session_repository import SessionRepository
    from app.services.assessment_service import AssessmentService

    settings = Settings.load()
    database = OrmDatabase(settings.resolved_database_source)
    await database.initialize()
    try:
        sessions = SessionRepository(database)
        service = AssessmentService(AssessmentRepository(database))
        updated = skipped = failed = 0
        for entry in await sessions.read_all():
            session = entry.session
            outputs = session.get("outputs") or {}
            if not any(isinstance(outputs.get(key), dict) for key in OUTPUT_KEYS):
                skipped += 1
                continue
            try:
                await service.record_session_results(session)
                updated += 1
            except Exception as exc:  # keep going; report at the end
                failed += 1
                print(f"  FAILED {session.get('id')}: {exc}")
        print(f"Backfilled {updated} session(s); skipped {skipped} without score outputs; {failed} failed.")
    finally:
        await database.shutdown()


if __name__ == "__main__":
    asyncio.run(run())
