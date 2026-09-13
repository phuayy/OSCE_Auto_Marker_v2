"""Test doubles for the content-marking seam.

``PipelineService`` asks its scoring pipeline for a *prepared* run
(``prepare_content_marking``) and then asks that run three things: does the
sheet on disk still count, run if not, and what to record on the step. It used
to probe for that method with ``getattr`` and fall back to a bare
``run_content_scoring`` when it was absent — a production branch that existed
only so the doubles in this suite could stay small, and that meant those doubles
exercised a code path the real pipeline never takes.

The branch is gone. This mixin keeps the doubles just as small: implement
``run_content_scoring`` (and optionally ``should_refresh_score_payload``), mix
this in, and the double speaks the seam the pipeline actually uses.
"""

from __future__ import annotations

from typing import Any

from app.pipeline.marking.base import MarkingPlan


class PreparedContentMarking:
    """One prepared content-marking run, backed by a ``run_content_scoring``
    double. Mirrors ``ScoringPipeline.ContentMarkingRun``'s three questions."""

    def __init__(self, scoring: Any, session: dict[str, Any], plan: MarkingPlan) -> None:
        self.scoring = scoring
        self.session = session
        self.plan = plan
        self.result: dict[str, Any] | None = None

    def needs_refresh(self, payload: Any) -> bool:
        predicate = getattr(self.scoring, "should_refresh_score_payload", None)
        if callable(predicate):
            return bool(predicate(payload))
        # Same default the pipeline used for a double with no predicate: only a
        # missing payload forces a re-run.
        return payload is None

    async def run(self, on_progress=None) -> dict[str, Any]:
        return await self.scoring.run_content_scoring(self.session)

    def step_metadata(self) -> dict[str, Any]:
        return {
            "markingMode": str(self.plan.mode),
            "selectedMarkingMode": str(self.plan.selected_mode),
        }


class ContentMarkingSeam:
    """Mixin giving a ``run_content_scoring``-shaped double the prepared seam.

    Override ``marking_plan`` to exercise a mode other than single.
    """

    marking_plan: MarkingPlan | None = None

    async def prepare_content_marking(self, session: dict[str, Any]) -> PreparedContentMarking:
        return PreparedContentMarking(self, session, self.marking_plan or MarkingPlan.single_only())
