import math
from typing import Any

from app.core.utils import runtime_seconds, utc_now_iso
from app.domain.enums import PipelineStep, StepStatus
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


# The standard pipeline's step order, used only to rank steps against one
# another (see ``step_rank``). ``whisperx`` is listed for documentation's own
# sake — it is the legacy alias of ``transcription`` and is remapped onto that
# same rank in ``_STEP_RANK_OVERRIDES`` below, never given a rank of its own.
# Segmentation steps (auto-crop's own job, never run alongside the standard
# sequence) rank after every standard step.
PIPELINE_STEP_ORDER: tuple[str, ...] = (
    PipelineStep.AUDIO_EXTRACTION,
    PipelineStep.WHISPERX,
    PipelineStep.TRANSCRIPTION,
    PipelineStep.TRANSCRIPT_NORMALIZATION,
    PipelineStep.LLM_PREPROCESS,
    PipelineStep.AUDIO_PROFESSIONALISM,
    PipelineStep.COMMUNICATION_SCORING,
    PipelineStep.CONTENT_SCORING,
    PipelineStep.ASSESSMENT_PERSISTENCE,
    PipelineStep.PERSON_DETECTION,
    PipelineStep.BELL_DETECTION,
)

# ``whisperx`` must sort exactly where ``transcription`` does — a session
# recorded before the transcription router shipped, and one processed since,
# have to compare equal so neither reads as "more advanced" than the other.
_STEP_RANK_OVERRIDES: dict[str, int] = {
    PipelineStep.WHISPERX: PIPELINE_STEP_ORDER.index(PipelineStep.TRANSCRIPTION),
}


def step_rank(step: str) -> int:
    """Where ``step`` sits in the standard pipeline order; higher = later.

    A step name this build does not recognise ranks after everything else
    (rather than raising) so an unknown key cannot crash progress projection —
    it simply never wins "most advanced".
    """
    if step in _STEP_RANK_OVERRIDES:
        return _STEP_RANK_OVERRIDES[step]
    try:
        return PIPELINE_STEP_ORDER.index(step)
    except ValueError:
        return len(PIPELINE_STEP_ORDER)


def running_steps(pipeline: dict[str, Any]) -> list[str]:
    """Step names currently ``running``, ascending by :func:`step_rank`."""
    steps = pipeline.get("steps")
    if not isinstance(steps, dict):
        return []
    names = [
        name
        for name, state in steps.items()
        if isinstance(state, dict) and state.get("status") == StepStatus.RUNNING
    ]
    names.sort(key=step_rank)
    return names


def sync_current_step(pipeline: dict[str, Any]) -> None:
    """Derive ``currentStep``/``stepProgress`` from ``steps`` — the *only* place
    the two scalars are written.

    ``PARALLEL_SCORING`` runs the content and communication branches at once,
    so more than one step can be ``running`` simultaneously. Before this, each
    branch's own step-transition code set ``currentStep``/``stepProgress``
    directly, which meant whichever branch wrote last decided the label, while
    ``record_progress`` kept writing the *other* branch's percentage into the
    same slot — the card could show "Scoring communication" carrying the
    content branch's number, and the bar would drop to a bare "Processing"
    the moment either branch finished, even though the other was still
    running. Deriving both scalars from ``steps`` in one place makes that
    combination structurally impossible: ``currentStep`` is always the most
    advanced (highest-rank) step that is actually running, and
    ``stepProgress`` is always *that* step's own recorded reading.

    ``currentStep`` becomes ``None`` only when nothing is running — completing
    the current step while a less-advanced one is still running hands the
    label to that other step instead of clearing it.
    """
    running = running_steps(pipeline)
    if not running:
        pipeline["currentStep"] = None
        pipeline["stepProgress"] = None
        return
    current = running[-1]
    pipeline["currentStep"] = current
    state = (pipeline.get("steps") or {}).get(current)
    progress = state.get("progress") if isinstance(state, dict) else None
    pipeline["stepProgress"] = progress if isinstance(progress, int | float) else None


def record_progress(session: dict, step: str, percent: float, *, tracked_step: bool) -> bool | None:
    """Persist a live completion percentage for a running step.

    A *tracked* step (the standard pipeline, one entry in ``pipeline.steps``)
    always records its own reading on ``steps[step]["progress"]`` regardless
    of which step is currently labelling the card — a background branch keeps
    its number even while it is not "current" — but only copies that reading
    into ``pipeline.stepProgress`` (the scalar the card actually renders)
    when ``step`` is the one ``sync_current_step`` has chosen as current. An
    *untracked* step (auto-crop's segmentation steps, which do not appear in
    ``steps``) keeps the pre-existing behaviour: it writes ``stepProgress``
    directly, gated on being the session's ``currentStep``.
    """
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
    if tracked_step:
        state["progress"] = percent
        if pipeline.get("currentStep") == step:
            pipeline["stepProgress"] = percent
    else:
        pipeline["stepProgress"] = percent
    return None
