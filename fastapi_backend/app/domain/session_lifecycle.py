import math

from app.core.utils import runtime_seconds, utc_now_iso
from app.domain.enums import StepStatus
from app.domain.sessions import SessionStatus


def fail_session(session: dict, message: str, *, expected_status: SessionStatus | None = None) -> bool | None:
    if expected_status is not None and session.get("status") != expected_status:
        return False
    session["status"] = SessionStatus.FAILED
    session["error"] = message
    pipeline = session.setdefault("pipeline", {})
    pipeline["endedAt"] = utc_now_iso()
    if pipeline.get("startedAt"):
        pipeline["runtimeSeconds"] = runtime_seconds(pipeline["startedAt"], pipeline["endedAt"])
    return None


def record_progress(session: dict, step: str, percent: float, *, tracked_step: bool) -> bool | None:
    if not math.isfinite(percent):
        return False
    pipeline = session.setdefault("pipeline", {})
    state = (pipeline.get("steps") or {}).get(step)
    if tracked_step:
        if not isinstance(state, dict) or state.get("status") != StepStatus.RUNNING:
            return False
    elif pipeline.get("currentStep") != step:
        return False
    percent = min(100.0, max(0.0, percent))
    pipeline["stepProgress"] = percent
    if tracked_step:
        state["progress"] = percent
    return None
