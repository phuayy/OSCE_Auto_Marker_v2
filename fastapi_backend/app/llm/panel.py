"""How many models mark, and who settles their disagreements.

:mod:`app.llm.routing` describes *one* logical target — a primary and the
fallbacks tried when it fails. A panel is a different shape: several markers
that all run, each against its own model, plus an adjudicator that is asked
only about the criteria the markers disagree on. This module holds that
configuration as a value, the way ``RoutingConfig`` does, so the API (which
stores it), the pipeline (which runs it) and the settings screen (which edits
it) share one definition.

Two rules the shape enforces:

* **Markers have no fallback chain.** A marker whose fallback lands on the
  other marker's model silently turns the panel into two samples of one model,
  which measures nothing. A marker that fails degrades the run loudly instead.
* **The adjudicator is its own target.** Falling back to a marker's model
  would let a marker judge its own dispute.

Secrets do not live here; provider keys travel by their own variables.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Mapping

from app.llm.routing import LLMTarget

PANEL_SCHEMA = "osce-llm-panel-v1"

# Fewer than two markers is not a panel; the run would be single mode with
# extra bookkeeping.
MIN_PANEL_MARKERS = 2


class MarkingMode(StrEnum):
    """Which content-marking strategy a run uses."""

    SINGLE = "single"
    PANEL = "panel"


class TieBreak(StrEnum):
    """What settles a disputed criterion when the adjudicator cannot answer.

    ``lenient`` awards the criterion, which is the rubric's own instruction
    for borderline evidence; ``strict`` withholds it; ``first_marker`` keeps
    whatever the first configured marker said.
    """

    LENIENT = "lenient"
    STRICT = "strict"
    FIRST_MARKER = "first_marker"


_SLUG = re.compile(r"[^a-z0-9]+")


def marker_key(target: LLMTarget) -> str:
    """A filesystem-safe name for one marker, derived from provider and model.

    Used to name a marker's output file, so a marker swapped in Settings can
    never adopt the previous marker's sheet: two different targets always
    resolve to two different keys. ``gemini:gemini-2.5-pro`` becomes
    ``gemini__gemini-2-5-pro``; an empty model (provider default) becomes
    ``gemini__default``.
    """
    provider = _SLUG.sub("-", target.provider_id.strip().lower()).strip("-") or "provider"
    model = _SLUG.sub("-", target.model.strip().lower()).strip("-") or "default"
    return f"{provider}__{model}"


@dataclass(frozen=True)
class PanelValidation:
    """What ``PanelConfig.validate`` found: hard errors and softer warnings.

    Errors make the configuration unrunnable and are rejected at the API.
    Warnings describe a panel that will run but is weaker than it looks — an
    adjudicator that is also a marker, two markers from one vendor — and are
    shown in the settings screen rather than enforced.
    """

    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.errors


@dataclass(frozen=True)
class PanelConfig:
    """The markers, the adjudicator and the tie-break policy for a panel run."""

    markers: tuple[LLMTarget, ...] = ()
    adjudicator: LLMTarget | None = None
    tie_break: TieBreak = TieBreak.LENIENT

    def marker_keys(self) -> tuple[str, ...]:
        return tuple(marker_key(target) for target in self.markers)

    def validate(self) -> PanelValidation:
        errors: list[str] = []
        warnings: list[str] = []

        markers = [target for target in self.markers if target.provider_id]
        if len(markers) < MIN_PANEL_MARKERS:
            errors.append(f"A panel needs at least {MIN_PANEL_MARKERS} markers; {len(markers)} configured.")

        seen: dict[str, int] = {}
        for index, target in enumerate(markers, start=1):
            first = seen.setdefault(target.key, index)
            if first != index:
                errors.append(
                    f"Marker {index} repeats marker {first} ({target.key}); two samples of one model are not a panel."
                )

        providers = {target.provider_id for target in markers}
        if len(markers) >= MIN_PANEL_MARKERS and len(providers) < len(markers):
            warnings.append(
                "Two markers share a provider. Marks from one vendor's models tend to agree with each "
                "other, so the panel catches less than a cross-vendor pair would."
            )

        if self.adjudicator is None or not self.adjudicator.provider_id:
            errors.append("A panel needs an adjudicator to settle the criteria the markers disagree on.")
        elif any(self.adjudicator.key == target.key for target in markers):
            warnings.append(
                "The adjudicator is also a marker. A model tends to side with its own earlier answer, "
                "so disputes will lean towards that marker."
            )

        return PanelValidation(errors=tuple(errors), warnings=tuple(warnings))

    def to_public(self) -> dict[str, Any]:
        return {
            "schema": PANEL_SCHEMA,
            "markers": [target.to_public() for target in self.markers],
            "adjudicator": self.adjudicator.to_public() if self.adjudicator else {"providerId": "", "model": ""},
            "tieBreak": str(self.tie_break),
        }

    @classmethod
    def from_raw(cls, raw: Any) -> "PanelConfig":
        """Build a config from stored settings or a request body, tolerating
        partial data.

        Tolerant for the same reason ``RoutingConfig.from_raw`` is: a stored
        row outlives the release that wrote it. What comes out may fail
        :meth:`validate` — that is the caller's question to ask — but it never
        raises on shape.
        """
        payload = raw if isinstance(raw, Mapping) else {}
        markers_raw = payload.get("markers")
        markers: list[LLMTarget] = []
        if isinstance(markers_raw, (list, tuple)):
            for item in markers_raw:
                target = LLMTarget.from_raw(item)
                if target is not None:
                    markers.append(target)
        adjudicator = LLMTarget.from_raw(payload.get("adjudicator"))
        tie_break_raw = str(payload.get("tieBreak") or payload.get("tie_break") or "").strip().lower()
        try:
            tie_break = TieBreak(tie_break_raw) if tie_break_raw else TieBreak.LENIENT
        except ValueError:
            tie_break = TieBreak.LENIENT
        return cls(markers=tuple(markers), adjudicator=adjudicator, tie_break=tie_break)


def parse_marking_mode(raw: Any) -> MarkingMode:
    """The mode a stored or submitted value names; anything unrecognised is
    ``single``, because that is the behaviour a deployment had before the
    setting existed."""
    token = str(raw or "").strip().lower()
    try:
        return MarkingMode(token) if token else MarkingMode.SINGLE
    except ValueError:
        return MarkingMode.SINGLE


__all__ = [
    "MIN_PANEL_MARKERS",
    "PANEL_SCHEMA",
    "MarkingMode",
    "PanelConfig",
    "PanelValidation",
    "TieBreak",
    "marker_key",
    "parse_marking_mode",
]
