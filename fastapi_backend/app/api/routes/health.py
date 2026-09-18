from __future__ import annotations

import asyncio
import shutil
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Response
from sqlalchemy import text

from app.api.dependencies import get_container
from app.database.change_tracking import verify_change_tracking
from app.schemas.common import HealthResponse
from app.services.container import AppContainer


router = APIRouter()


@router.get("/health", response_model=HealthResponse)
async def health() -> dict[str, object]:
    """Liveness probe: the process is up and serving."""
    return {"ok": True, "service": "osce-ai-marker-local-api"}


@router.get("/health/ready")
async def readiness(response: Response, container: AppContainer = Depends(get_container)) -> dict[str, Any]:
    """Readiness probe: verifies the dependencies a request actually needs.

    Database and storage are *critical* (a failure returns 503). External media
    binaries are *informational* — they are only needed by the worker, so their
    absence is reported but does not fail readiness.
    """
    database_ok = await _check_database(container)
    tracking_ok = await _check_change_tracking(container) if database_ok else False
    storage_ok = await asyncio.to_thread(_check_storage_writable, container.settings.paths.storage_root)
    settings = container.settings
    binaries = {
        "ffmpeg": _binary_available(settings.ffmpeg_bin),
        "ffprobe": _binary_available(settings.ffprobe_bin),
        "whisperx": _binary_available(settings.whisperx_bin),
        # Informational, like the binaries: person segmentation runs on the
        # worker, so absence degrades that feature (falls back to bells)
        # rather than failing API readiness.
        "humanDetector": await asyncio.to_thread(_human_detector_available, settings),
    }

    ready = database_ok and tracking_ok and storage_ok
    if not ready:
        response.status_code = 503
    return {
        "ready": ready,
        "checks": {"database": database_ok, "changeTracking": tracking_ok, "storage": storage_ok, **binaries},
        # Informational. The credential cache is only correct while the change
        # feed can tell it a key rotated, so "pushActive: false with a high hit
        # rate" is the shape worth alerting on — it means rotations are reaching
        # this process by counter comparison rather than by announcement.
        "caches": {
            "providerCredentials": container.llm_settings.credential_cache_stats(),
            "appSettings": container.app_settings.cache_stats(),
            "customProviders": container.llm_settings.custom_provider_cache_stats(),
            "userDirectory": container.user_directory.cache_stats(),
            "changeFeedPushActive": container.changes.push_active,
        },
        # The accelerator lease: how many GPU steps run now and how many wait.
        # A steady "waiting" above zero says the machine is transcription-bound,
        # not that anything is wrong.
        "leases": {"gpu": container.gpu.stats()},
    }


async def _check_database(container: AppContainer) -> bool:
    try:
        async with container.orm_database.engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


async def _check_change_tracking(container: AppContainer) -> bool:
    try:
        await verify_change_tracking(container.orm_database.engine)
        return True
    except Exception:
        return False


def _check_storage_writable(storage_root: Path) -> bool:
    try:
        storage_root.mkdir(parents=True, exist_ok=True)
        probe = storage_root / ".health_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
        return True
    except Exception:
        return False


def _human_detector_available(settings: Any) -> bool:
    """True when RT-DETR person segmentation can run: feature enabled, script
    present, and torch + transformers importable in this venv (the detector
    subprocess uses the same interpreter)."""
    try:
        if not settings.enable_human_detector:
            return False
        if not settings.human_detector_script_path.exists():
            return False
        import importlib.util

        return all(importlib.util.find_spec(name) is not None for name in ("torch", "transformers"))
    except Exception:
        return False


def _binary_available(binary: str) -> bool:
    value = str(binary or "").strip()
    if not value:
        return False
    if any(separator in value for separator in ("/", "\\")):
        return Path(value).exists()
    return shutil.which(value) is not None
