"""Region-of-interest focus for RT-DETR person segmentation.

Two layers, same split as ``test_person_presets.py``: pure resolve/clamp tests
for ``app.pipeline.region_focus``, and pure geometry tests for
``boxes_in_region`` in ``scripts/detect_human_segments.py`` (loaded the same
way the height-gate tests load it — no GPU, no model, no video).
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from app.pipeline import region_focus

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "detect_human_segments.py"
_spec = importlib.util.spec_from_file_location("detect_human_segments", _SCRIPT)
dhs = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = dhs
_spec.loader.exec_module(dhs)


# ---------------------------------------------------------------------------
# region_focus.resolve / resolve_options
# ---------------------------------------------------------------------------


def test_default_is_the_whole_frame() -> None:
    resolved = region_focus.resolve(None)
    assert resolved.left_enabled and resolved.right_enabled
    assert resolved.left_ratio == 1.0 and resolved.right_ratio == 1.0
    assert resolved.is_full_frame


def test_one_side_disabled_is_not_full_frame() -> None:
    resolved = region_focus.resolve({"leftEnabled": True, "rightEnabled": False, "leftRatio": 0.6})
    assert resolved.left_enabled is True
    assert resolved.right_enabled is False
    assert resolved.left_ratio == 0.6
    assert not resolved.is_full_frame


def test_both_sides_disabled_degrades_by_default_and_raises_when_strict() -> None:
    """Matches the unknown-preset rule in person_presets: a replayed session
    (or a config this build cannot make sense of) must not kill a queued job,
    but the API that accepts a fresh choice should refuse a config that would
    never match a person."""
    degraded = region_focus.resolve({"leftEnabled": False, "rightEnabled": False})
    assert degraded.left_enabled and degraded.right_enabled

    with pytest.raises(region_focus.RegionFocusError):
        region_focus.resolve({"leftEnabled": False, "rightEnabled": False}, strict=True)


def test_ratios_clamp_into_range() -> None:
    resolved = region_focus.resolve({"leftRatio": 5.0, "rightRatio": -1.0})
    assert resolved.left_ratio == region_focus.RATIO_RANGE[1]
    assert resolved.right_ratio == region_focus.RATIO_RANGE[0]


def test_unparseable_ratio_falls_back_to_default_rather_than_raising() -> None:
    resolved = region_focus.resolve({"leftRatio": "not-a-number"})
    assert resolved.left_ratio == region_focus.DEFAULT_LEFT_RATIO


def test_resolve_options_returns_the_persistable_shape() -> None:
    assert region_focus.resolve_options({"leftEnabled": True, "rightEnabled": False, "leftRatio": 0.6}) == {
        "leftEnabled": True,
        "rightEnabled": False,
        "leftRatio": 0.6,
        "rightRatio": 1.0,
    }
    assert region_focus.resolve_options(None) == region_focus.FULL_FRAME.as_dict()


# ---------------------------------------------------------------------------
# boxes_in_region (pure geometry, no GPU)
# ---------------------------------------------------------------------------

# Frame normalised to width=1.0 (matches the recorded-detection fixtures used
# by test_person_presets.py, where height is normalised the same way).
LEFT_BOX = (0.05, 0.2, 0.15, 0.8)  # centre x = 0.10
RIGHT_BOX = (0.85, 0.2, 0.95, 0.8)  # centre x = 0.90
CENTER_BOX = (0.45, 0.2, 0.55, 0.8)  # centre x = 0.50


def test_full_frame_keeps_every_box() -> None:
    kept, rejected = dhs.boxes_in_region(
        [LEFT_BOX, RIGHT_BOX, CENTER_BOX],
        1.0,
        left_enabled=True,
        right_enabled=True,
        left_ratio=1.0,
        right_ratio=1.0,
    )
    assert kept == [LEFT_BOX, RIGHT_BOX, CENTER_BOX]
    assert rejected == 0


def test_left_only_excludes_the_right_hand_box() -> None:
    """The scenario from the feature request: the patient/examiner half cut
    off on the right, so the right zone is unchecked and the left zone is
    narrowed to where the intended subjects actually stand."""
    kept, rejected = dhs.boxes_in_region(
        [LEFT_BOX, RIGHT_BOX], 1.0, left_enabled=True, right_enabled=False, left_ratio=0.6, right_ratio=1.0
    )
    assert kept == [LEFT_BOX]
    assert rejected == 1


def test_right_only_excludes_the_left_hand_box() -> None:
    kept, rejected = dhs.boxes_in_region(
        [LEFT_BOX, RIGHT_BOX], 1.0, left_enabled=False, right_enabled=True, left_ratio=1.0, right_ratio=0.2
    )
    assert kept == [RIGHT_BOX]
    assert rejected == 1


def test_a_box_between_two_narrow_zones_is_rejected_by_both() -> None:
    """Narrowing both sides can leave a gap in the middle of the frame — a box
    centred there belongs to neither zone and must be dropped, not double
    counted or kept by accident."""
    kept, rejected = dhs.boxes_in_region(
        [CENTER_BOX], 1.0, left_enabled=True, right_enabled=True, left_ratio=0.3, right_ratio=0.3
    )
    assert kept == []
    assert rejected == 1


def test_both_zones_disabled_rejects_everything() -> None:
    """boxes_in_region itself has no leniency — that lives in resolve(). A
    caller that hands it two disabled zones gets exactly what it asked for."""
    kept, rejected = dhs.boxes_in_region(
        [LEFT_BOX, RIGHT_BOX, CENTER_BOX],
        1.0,
        left_enabled=False,
        right_enabled=False,
        left_ratio=1.0,
        right_ratio=1.0,
    )
    assert kept == []
    assert rejected == 3


def test_zero_width_frame_keeps_everything_rather_than_dividing_by_zero() -> None:
    kept, rejected = dhs.boxes_in_region(
        [LEFT_BOX], 0.0, left_enabled=True, right_enabled=False, left_ratio=0.1, right_ratio=1.0
    )
    assert kept == [LEFT_BOX]
    assert rejected == 0


# ---------------------------------------------------------------------------
# Composition: region filter feeds the height gate, same order _infer uses.
# ---------------------------------------------------------------------------


def test_region_filter_composes_with_the_height_gate() -> None:
    """A short box (a limb) on the excluded side must not need the height gate
    to be dropped, and a full-height box on the excluded side must not survive
    just because it would have passed the height gate."""
    tall_left = (0.05, 0.1, 0.15, 0.9)  # centre x = 0.10, height 0.8
    tall_right = (0.85, 0.1, 0.95, 0.9)  # centre x = 0.90, height 0.8
    short_left = (0.05, 0.4, 0.15, 0.6)  # centre x = 0.10, height 0.2 (a limb)

    in_region, region_rejected = dhs.boxes_in_region(
        [tall_left, tall_right, short_left],
        1.0,
        left_enabled=True,
        right_enabled=False,
        left_ratio=0.6,
        right_ratio=1.0,
    )
    assert in_region == [tall_left, short_left]
    assert region_rejected == 1  # tall_right, outside the enabled left zone

    kept, height_rejected = dhs.count_valid_people(in_region, 1.0, 0.4)
    assert kept == 1  # only tall_left clears both gates
    assert height_rejected == 1  # short_left: in region, but too short
