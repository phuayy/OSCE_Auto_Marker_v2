"""Horizontal region-of-interest focus for the RT-DETR person detector.

Orthogonal to ``person_presets``: a preset says how many people must be on
screen; this says WHERE on screen the detector is allowed to look for them.
Some camera rigs frame a third party at one edge of the shot — a second
examiner walking past, a monitor, a doorway — close enough that its own limbs
or reflection cross the ``person_presets`` height gate and get counted as a
real occupant. The height gate cannot fix that: it separates a whole person
from a limb, not a wanted person from an unwanted one standing full-height at
the edge of frame. Restricting detection to the side of the frame the intended
subjects actually occupy does.

The frame is split into two independently switchable zones, measured from
each edge inward:

* left zone   = ``[0, left_ratio * width]``,          when ``left_enabled``
* right zone  = ``[(1 - right_ratio) * width, width]``, when ``right_enabled``

A detection counts if its box centre falls in an enabled zone. Both zones
enabled at ratio 1.0 (the default) cover the whole frame — byte-for-byte the
pre-existing behaviour, so no session that never touched this setting changes
meaning.
"""
from __future__ import annotations

from dataclasses import dataclass

# A ratio of 0 would mean "this side is on but excludes everything", which is
# indistinguishable from turning it off except silently. The checkbox is what
# turns a side off; the ratio only ever narrows an enabled side.
RATIO_RANGE = (0.05, 1.0)

DEFAULT_LEFT_ENABLED = True
DEFAULT_RIGHT_ENABLED = True
DEFAULT_LEFT_RATIO = 1.0
DEFAULT_RIGHT_RATIO = 1.0


@dataclass(frozen=True)
class RegionFocusConfig:
    """Which horizontal zone(s) of the frame the detector counts people in."""

    left_enabled: bool = DEFAULT_LEFT_ENABLED
    right_enabled: bool = DEFAULT_RIGHT_ENABLED
    left_ratio: float = DEFAULT_LEFT_RATIO
    right_ratio: float = DEFAULT_RIGHT_RATIO

    @property
    def is_full_frame(self) -> bool:
        """True when this config filters nothing (the backward-compatible default)."""
        return (
            self.left_enabled
            and self.right_enabled
            and self.left_ratio >= 1.0
            and self.right_ratio >= 1.0
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "leftEnabled": self.left_enabled,
            "rightEnabled": self.right_enabled,
            "leftRatio": self.left_ratio,
            "rightRatio": self.right_ratio,
        }


FULL_FRAME = RegionFocusConfig()


class RegionFocusError(ValueError):
    """Both zones were disabled — nothing would ever count as a person."""


def _clamp(value: float, bounds: tuple[float, float]) -> float:
    low, high = bounds
    return max(low, min(high, value))


def describe_defaults() -> dict[str, object]:
    """Defaults + bounds for the settings/upload UI — JSON-ready."""
    return {
        "defaults": FULL_FRAME.as_dict(),
        "bounds": {"ratio": list(RATIO_RANGE)},
    }


def resolve(options: dict | None, *, strict: bool = False) -> RegionFocusConfig:
    """Resolve an incoming/stored options mapping into a usable config.

    ``strict`` (the API path) rejects a config with both zones disabled — a
    request that would never match a person. The lenient default (the job
    path, replaying a session written before this build, or one whose author
    unchecked both by mistake) degrades to :data:`FULL_FRAME` rather than
    failing a run that is already minutes deep, matching how an unknown
    ``person_presets`` id degrades to the default preset.
    """
    source = options or {}
    left_enabled = bool(source.get("leftEnabled", DEFAULT_LEFT_ENABLED))
    right_enabled = bool(source.get("rightEnabled", DEFAULT_RIGHT_ENABLED))
    if not left_enabled and not right_enabled:
        if strict:
            raise RegionFocusError("At least one of the left/right zones must stay enabled.")
        left_enabled = DEFAULT_LEFT_ENABLED
        right_enabled = DEFAULT_RIGHT_ENABLED

    def _ratio(key: str, default: float) -> float:
        raw = source.get(key, default)
        try:
            value = float(raw) if raw is not None else default
        except (TypeError, ValueError):
            value = default
        return round(_clamp(value, RATIO_RANGE), 4)

    left_ratio = _ratio("leftRatio", DEFAULT_LEFT_RATIO)
    right_ratio = _ratio("rightRatio", DEFAULT_RIGHT_RATIO)
    return RegionFocusConfig(
        left_enabled=left_enabled,
        right_enabled=right_enabled,
        left_ratio=left_ratio,
        right_ratio=right_ratio,
    )


def resolve_options(options: dict | None, *, strict: bool = False) -> dict[str, object]:
    """Resolve into a persistable record — same shape stored on the session."""
    return resolve(options, strict=strict).as_dict()
