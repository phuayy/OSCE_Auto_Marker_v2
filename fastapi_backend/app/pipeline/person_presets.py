"""Occupancy presets for the RT-DETR person-presence segmenter.

One table, read by four layers: the upload schema validates against it, the
async upload service resolves an upload's choice into concrete numbers, the
settings API renders the picker from it, and ``scripts/detect_human_segments.py``
imports it so a hand-run CLI and a queued job agree on what ``solo`` means.

Why a preset instead of a single ``--min-people`` flag: counting people is not
enough on its own. A hand or a shoulder entering at the edge of the frame is a
*person* to the detector (COCO trains on truncated bodies, and a clean forearm
scores well above any usable confidence threshold), so a camera framing one
student reports two people for minutes at a time. Raising the confidence
threshold is the wrong lever — it discards distant real people long before it
discards a close-up limb. Box height is the lever that separates them.

Measured on two 117-minute OSCE tapes of the same cohort (1 fps, 864x480,
rtdetr_v2_r18vd, confidence 0.7):

* real people occupy 0.56-1.05 of frame height (p10-p90)
* limbs intruding at the frame edge occupy 0.16-0.35

``min_box_height_ratio`` between 0.32 and 0.55 gives the correct answer on both
tapes; 0.40 sits in the middle of that plateau. ``min_session_seconds`` is the
independent second guard: every false session those tapes produced was 50-93 s
long, while every real one ran 221-362 s.

``pair`` reproduces the pre-preset behaviour byte for byte and stays the
default, so no existing session changes meaning.
"""
from __future__ import annotations

from dataclasses import dataclass, replace

# Bounds for operator-supplied numbers. Enforced here so the API, the CLI and
# the resolver cannot disagree about what is acceptable.
MIN_PEOPLE_RANGE = (1, 10)
MIN_BOX_HEIGHT_RATIO_RANGE = (0.0, 0.95)
MIN_SESSION_SECONDS_RANGE = (0.0, 3600.0)

CUSTOM_PRESET = "custom"
DEFAULT_PRESET = "pair"


@dataclass(frozen=True)
class PersonSegmentationPreset:
    """A named occupancy rule.

    ``min_people``            people required on screen for a session to be active
    ``min_box_height_ratio``  smallest box height (as a fraction of frame height)
                              that counts as a person at all; 0 disables the gate
    ``min_session_seconds``   shortest confirmed session kept; 0 disables the guard
    """

    id: str
    label: str
    description: str
    min_people: int
    min_box_height_ratio: float
    min_session_seconds: float

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "label": self.label,
            "description": self.description,
            "minPeople": self.min_people,
            "minBoxHeightRatio": self.min_box_height_ratio,
            "minSessionSeconds": self.min_session_seconds,
        }


PRESETS: dict[str, PersonSegmentationPreset] = {
    "pair": PersonSegmentationPreset(
        id="pair",
        label="Two people (wide shot)",
        description=(
            "A session is running whenever two people are on screen. No size "
            "filter — matches the original behaviour."
        ),
        min_people=2,
        min_box_height_ratio=0.0,
        min_session_seconds=0.0,
    ),
    "pair_strict": PersonSegmentationPreset(
        id="pair_strict",
        label="Two people, ignore edge intrusions",
        description=(
            "Two people, but a hand, shoulder or passer-by clipping the edge of "
            "the frame is not counted as the second person."
        ),
        min_people=2,
        min_box_height_ratio=0.40,
        min_session_seconds=120.0,
    ),
    "solo": PersonSegmentationPreset(
        id="solo",
        label="One student in frame",
        description=(
            "A session is running whenever one full-body person is on screen. "
            "Limbs and people at the edge of the frame are ignored, so the gaps "
            "between stations still read as breaks."
        ),
        min_people=1,
        min_box_height_ratio=0.40,
        min_session_seconds=120.0,
    ),
    CUSTOM_PRESET: PersonSegmentationPreset(
        id=CUSTOM_PRESET,
        label="Custom",
        description="Choose the occupancy count and filters yourself.",
        min_people=2,
        min_box_height_ratio=0.40,
        min_session_seconds=120.0,
    ),
}

PRESET_IDS = tuple(PRESETS)


class PresetError(ValueError):
    """An unusable preset id or out-of-range override."""


def _clamp(value: float, bounds: tuple[float, float]) -> float:
    low, high = bounds
    return max(low, min(high, value))


def describe_presets() -> list[dict[str, object]]:
    """Catalogue for the settings/upload UI — ordered, JSON-ready."""
    return [PRESETS[preset_id].as_dict() for preset_id in PRESET_IDS]


def resolve(
    preset_id: str | None,
    *,
    min_people: int | None = None,
    min_box_height_ratio: float | None = None,
    min_session_seconds: float | None = None,
    strict: bool = False,
) -> PersonSegmentationPreset:
    """Resolve a preset id plus optional overrides into concrete numbers.

    ``strict`` (the API path) rejects an unknown preset id; the lenient default
    (the job path, replaying a session written by an older or newer build)
    degrades to :data:`DEFAULT_PRESET` rather than failing a run that is already
    minutes deep. Out-of-range numbers are clamped in both modes — a run with a
    slightly-too-large threshold is still a useful run, and the API rejects
    those upstream anyway.
    """
    key = str(preset_id or DEFAULT_PRESET).strip().lower().replace("-", "_")
    preset = PRESETS.get(key)
    if preset is None:
        if strict:
            raise PresetError(
                f"Unknown segmentation preset {preset_id!r}. Expected one of: {', '.join(PRESET_IDS)}."
            )
        preset = PRESETS[DEFAULT_PRESET]

    overrides: dict[str, object] = {}
    if min_people is not None:
        overrides["min_people"] = int(_clamp(int(min_people), MIN_PEOPLE_RANGE))
    if min_box_height_ratio is not None:
        overrides["min_box_height_ratio"] = round(
            _clamp(float(min_box_height_ratio), MIN_BOX_HEIGHT_RATIO_RANGE), 4
        )
    if min_session_seconds is not None:
        overrides["min_session_seconds"] = round(
            _clamp(float(min_session_seconds), MIN_SESSION_SECONDS_RANGE), 3
        )
    return replace(preset, **overrides) if overrides else preset


def resolve_options(options: dict | None, *, strict: bool = False) -> dict[str, object]:
    """Resolve a stored/incoming options mapping into a persistable record.

    The record carries BOTH the preset name and the numbers it resolved to.
    Storing only the name would make a session replay differently after the
    table is retuned; storing only the numbers would lose what the operator
    actually picked.
    """
    source = options or {}
    preset = resolve(
        source.get("preset"),
        min_people=source.get("minPeople"),
        min_box_height_ratio=source.get("minBoxHeightRatio"),
        min_session_seconds=source.get("minSessionSeconds"),
        strict=strict,
    )
    return {
        "preset": preset.id,
        "minPeople": preset.min_people,
        "minBoxHeightRatio": preset.min_box_height_ratio,
        "minSessionSeconds": preset.min_session_seconds,
    }
