"""What every content-marking strategy shares.

Two things. The :class:`MarkingPlan` is the settings service's answer to "what
should this run do?" — mode, targets and the per-target subprocess
environments, all derived from **one** credential snapshot so a rotation
landing mid-request cannot give two markers keys from different worlds. It is
read once at the start of ``content_scoring`` and never re-read: a toggle
flipped mid-run applies to the next run.

The :class:`ContentMarkerRunner` spawns one marker subprocess — the assessor
script — with a given environment to a given output path. Single mode calls it
once to ``scores/<id>.json``; a panel calls it once per marker to that marker's
own file. Nothing in it knows which of the two is happening.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.core.artifacts import ArtifactMetadata, artifact_metadata
from app.core.config import Settings
from app.core.json_utils import extract_json_object, write_json_file
from app.core.process import CommandRunner
from app.llm.panel import MarkingMode, TieBreak
from app.llm.routing import LLMTarget, RoutingConfig
from app.pipeline.marking.fingerprint import SHEET_INPUTS_KEY
from app.services.auth_service import AuthService
from app.services.event_service import EventService

logger = logging.getLogger(__name__)

# Where the single-mode sheet, and a panel's final sheet, are served from.
SCORES_MEDIA_DIRECTORY = "/media/scores"

# A strategy reports how far through the step it is, 0-100. The pipeline turns
# that into the session card's moving bar; a strategy with nothing to report
# between start and finish simply never calls it.
ProgressCallback = Callable[[float], Awaitable[None]]


@dataclass(frozen=True)
class MarkerAssignment:
    """One model that will mark, and the environment its subprocess needs.

    ``llm_env`` is the routing blob plus *only this provider's* credential (and
    the custom-provider catalogue), built by ``LLMSettingsService`` from the
    same snapshot as every other assignment in the plan. ``key`` names the
    marker's output file; see :func:`app.llm.panel.marker_key`.
    """

    target: LLMTarget
    key: str
    llm_env: Mapping[str, str] = field(default_factory=dict, repr=False)

    def describe(self) -> dict[str, Any]:
        return {"providerId": self.target.provider_id, "model": self.target.model, "key": self.key}


@dataclass(frozen=True)
class MarkingPlan:
    """How this run marks content: resolved, credential-complete, immutable.

    ``mode`` is what will *actually* run; ``selected_mode`` is what the operator
    chose. They differ when a panel cannot be assembled here — a marker with no
    key on this machine, an invalid stored configuration — and ``warnings``
    says why, so the step metadata and the settings screen can both show it.
    """

    mode: MarkingMode = MarkingMode.SINGLE
    selected_mode: MarkingMode = MarkingMode.SINGLE
    # What single mode runs, and what a degraded panel falls back to. ``None``
    # only when no settings service exists (tests); the subprocess then reads
    # its legacy environment variables.
    single: RoutingConfig | None = None
    single_env: Mapping[str, str] = field(default_factory=dict, repr=False)
    markers: tuple[MarkerAssignment, ...] = ()
    adjudicator: MarkerAssignment | None = None
    tie_break: TieBreak = TieBreak.LENIENT
    warnings: tuple[str, ...] = ()

    @classmethod
    def single_only(
        cls,
        routing: RoutingConfig | None = None,
        llm_env: Mapping[str, str] | None = None,
    ) -> "MarkingPlan":
        return cls(single=routing, single_env=dict(llm_env or {}))

    def describe(self) -> dict[str, Any]:
        """A credential-free summary for logs, step metadata and the settings
        screen's "effective" block."""
        return {
            "mode": str(self.mode),
            "selectedMode": str(self.selected_mode),
            "markers": [assignment.describe() for assignment in self.markers],
            "adjudicator": self.adjudicator.describe() if self.adjudicator else None,
            "tieBreak": str(self.tie_break),
            "warnings": list(self.warnings),
        }


class ContentMarkerRunner:
    """Spawns the content assessor once.

    The script is handed every path it reads (see ``scripts/scorer_inputs.py``)
    and an environment that already carries the routing and credentials for
    the model it should call. It writes its sheet to ``output_path``; if it
    printed the JSON but did not write the file, the payload on stdout is
    persisted so the caller always gets a file-backed artefact.
    """

    def __init__(
        self,
        settings: Settings,
        runner: CommandRunner,
        events: EventService,
        auth: AuthService,
    ) -> None:
        self.settings = settings
        self.runner = runner
        self.events = events
        self.auth = auth

    def python_env(self, extra_env: Mapping[str, str] | None = None) -> dict[str, str]:
        """The subprocess environment every scorer starts from: the
        deployment's Python settings, the in-memory NVIDIA key when one was
        loaded from the platform secrets file, and whatever the caller adds."""
        env = self.settings.subprocess_env()
        if self.auth.runtime.nvidia_api_key:
            env["NVIDIA_API_KEY"] = self.auth.runtime.nvidia_api_key
        env.update(extra_env or {})
        return env

    async def run(
        self,
        session_id: str,
        *,
        transcript_path: Path,
        case_study_path: Path,
        output_path: Path,
        llm_env: Mapping[str, str],
        inputs: Mapping[str, Any] | None = None,
        media_directory: str = SCORES_MEDIA_DIRECTORY,
        label: str = "Content scoring",
        log_source: str = "scorer",
    ) -> ArtifactMetadata:
        """``inputs`` is the fingerprint of the transcript and case study this
        run handed the script; when given it is recorded on the sheet."""
        if not self.settings.scorer_script_path.exists():
            raise RuntimeError(f"Scorer script not found at {self.settings.scorer_script_path}")
        args = [
            str(self.settings.scorer_script_path),
            "--session-id",
            session_id,
            "--transcript",
            str(transcript_path),
            "--case-study",
            str(case_study_path),
            "--output",
            str(output_path),
        ]
        await self.events.publish(
            session_id,
            "log",
            {"source": log_source, "message": f"{self.settings.scorer_python_bin} {' '.join(args)}"},
        )
        result = await self.runner.run(
            self.settings.scorer_python_bin,
            args,
            label,
            env=self.python_env(llm_env),
            on_output=lambda stream, text: self.events.publish(
                session_id,
                "log",
                {"source": f"{log_source}-{stream}", "message": text},
            ),
        )
        if output_path.exists():
            payload = await asyncio.to_thread(lambda: extract_json_object(output_path.read_text(encoding="utf-8")))
            rewrite = inputs is not None
        else:
            payload = extract_json_object(result.stdout)
            rewrite = True
        if inputs is not None:
            # Stamp what this sheet was marked *from*, so a reuse predicate can
            # refuse it against different inputs. Written here rather than by
            # the script so producer and consumer are always the same build: a
            # skew between them would surface as PanelMarking._mark's post-run
            # re-check rejecting a sheet the marker just produced, i.e. as
            # "every marker failed". A caller that passes nothing (single mode)
            # gets the script's file back untouched.
            payload[SHEET_INPUTS_KEY] = dict(inputs)
        if rewrite:
            await asyncio.to_thread(write_json_file, output_path, payload)
        return artifact_metadata(output_path, media_directory)


__all__ = [
    "SCORES_MEDIA_DIRECTORY",
    "ContentMarkerRunner",
    "MarkerAssignment",
    "MarkingPlan",
    "ProgressCallback",
]
