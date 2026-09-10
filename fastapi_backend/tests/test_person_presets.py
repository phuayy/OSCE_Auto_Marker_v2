"""Occupancy presets for RT-DETR person segmentation.

Two layers of test:

* pure unit tests for the height gate and the preset table, and
* regression tests that replay **recorded detections from two real OSCE
  recordings** (tests/fixtures/human_segments/) through the production
  segmenter. The fixtures hold every person box the detector found at score
  >= 0.5, so a gate or threshold change replays offline — no GPU, no weights,
  no video, and the whole file runs in under a second.

The two recordings are the same cohort of 15 stations filmed by two cameras,
which is what makes them worth freezing: the wide camera under the `pair` rule
and the tight camera under the `solo` rule must find the SAME 15 stations at
the same clock times. That agreement, not a hand-labelled list, is the ground
truth these tests assert against.
"""
from __future__ import annotations

import gzip
import importlib.util
import json
import sys
from pathlib import Path

import pytest

from app.pipeline import person_presets

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "detect_human_segments.py"
_spec = importlib.util.spec_from_file_location("detect_human_segments", _SCRIPT)
dhs = importlib.util.module_from_spec(_spec)
# Register before exec: the script's @dataclass decorators resolve their own
# module through sys.modules at class-creation time.
sys.modules[_spec.name] = dhs
_spec.loader.exec_module(dhs)

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "human_segments"
COMMON_COLD = FIXTURES / "common_cold_session_1.json.gz"
HAYFEVER = FIXTURES / "hayfever_session_1.json.gz"
COMMON_COLD_DURATION = 7022.2
HAYFEVER_DURATION = 6968.4

# The detector's own default; the fixtures were recorded below it so any
# threshold at or above 0.5 replays exactly.
CONFIDENCE = 0.7

# Starts (seconds) of the 15 stations on the wide camera under `pair` — the
# behaviour that shipped before presets existed, frozen so the gate cannot
# quietly change it.
COMMON_COLD_PAIR_STARTS = [
    184.0, 677.0, 1159.0, 1567.0, 2049.0, 2531.0, 2898.0,
    3376.0, 3867.0, 4268.0, 4755.0, 5234.0, 5617.0, 6103.0, 6588.0,
]


def load_fixture(path: Path) -> dict:
    return json.loads(gzip.decompress(path.read_bytes()).decode("utf-8"))


def counts_for(fixture: dict, preset: person_presets.PersonSegmentationPreset) -> list[int]:
    """Per-sample person count under a preset, from recorded detections.

    Mirrors what ``PersonCounter._infer`` does at runtime: filter to the
    confidence threshold, then apply the height gate through the same pure
    function the detector calls. Fixture boxes are normalised, so the frame is
    one unit tall.
    """
    counts = []
    for frame in fixture["frames"]:
        boxes = [(x0, y0, x1, y1) for score, x0, y0, x1, y1 in frame if score >= CONFIDENCE]
        kept, _rejected = count_with_gate(boxes, preset.min_box_height_ratio)
        counts.append(kept)
    return counts


def count_with_gate(boxes, min_box_height_ratio: float) -> tuple[int, int]:
    return dhs.count_valid_people(boxes, 1.0, min_box_height_ratio)


def sessions_for(
    fixture: dict,
    duration: float,
    preset: person_presets.PersonSegmentationPreset,
) -> list[dict]:
    """Confirmed sessions under a preset, through the production segmenter."""
    return dhs.build_tolerant_session_ranges(
        counts_for(fixture, preset),
        duration,
        min_people=preset.min_people,
        sample_dt=1.0,
        start_window_samples=50,
        start_tolerance_samples=10,
        end_window_samples=50,
        end_tolerance_samples=10,
        start_offset_seconds=0.0,
        end_offset_seconds=0.0,
        min_clip_seconds=0.5,
        min_session_seconds=preset.min_session_seconds,
    )[0]


@pytest.fixture(scope="module")
def common_cold() -> dict:
    return load_fixture(COMMON_COLD)


@pytest.fixture(scope="module")
def hayfever() -> dict:
    return load_fixture(HAYFEVER)


# ---------------------------------------------------------------------------
# The height gate (pure)
# ---------------------------------------------------------------------------


def test_gate_off_counts_every_detection() -> None:
    """0 must be a true no-op: `pair` is the pre-preset behaviour and any drift
    here silently re-cuts every existing long session."""
    boxes = [(0.0, 0.1, 0.3, 0.9), (0.9, 0.7, 1.0, 0.95), (0.4, 0.0, 0.5, 0.05)]
    assert count_with_gate(boxes, 0.0) == (3, 0)


def test_gate_drops_an_edge_limb_but_keeps_the_student() -> None:
    body = (0.10, 0.05, 0.45, 0.92)  # 0.87 of frame height
    limb = (0.92, 0.60, 1.00, 0.88)  # 0.28 — the measured limb median
    assert count_with_gate([body, limb], 0.40) == (1, 1)


def test_gate_keeps_a_smaller_but_real_second_person() -> None:
    """The gate must not be a proxy for "far away": a real person standing back
    still clears 0.40 on this rig (measured p10 for a second person: 0.78)."""
    near = (0.10, 0.05, 0.45, 0.92)
    far = (0.60, 0.30, 0.75, 0.85)  # 0.55 of frame height
    assert count_with_gate([near, far], 0.40) == (2, 0)


def test_gate_rejection_count_is_reported() -> None:
    """The rejected tally is what makes a mis-tuned gate diagnosable from one
    run instead of guessed at."""
    boxes = [(0.0, 0.8, 0.1, 0.9), (0.2, 0.8, 0.3, 0.9), (0.4, 0.0, 0.6, 0.9)]
    assert count_with_gate(boxes, 0.40) == (1, 2)


# ---------------------------------------------------------------------------
# The preset table
# ---------------------------------------------------------------------------


def test_pair_preset_is_the_historic_behaviour() -> None:
    pair = person_presets.resolve("pair")
    assert (pair.min_people, pair.min_box_height_ratio, pair.min_session_seconds) == (2, 0.0, 0.0)


def test_solo_preset_pairs_one_person_with_the_gate() -> None:
    """min_people=1 without a gate is unusable — the breaks between stations
    contain people too — so the preset must carry both halves."""
    solo = person_presets.resolve("solo")
    assert solo.min_people == 1
    assert solo.min_box_height_ratio > 0.0
    assert solo.min_session_seconds > 0.0


def test_unknown_preset_degrades_by_default_and_raises_when_strict() -> None:
    assert person_presets.resolve("laser-eyes").id == person_presets.DEFAULT_PRESET
    with pytest.raises(person_presets.PresetError):
        person_presets.resolve("laser-eyes", strict=True)


def test_custom_overrides_are_applied_and_clamped() -> None:
    resolved = person_presets.resolve_options(
        {"preset": "custom", "minPeople": 99, "minBoxHeightRatio": -1.0, "minSessionSeconds": 45}
    )
    assert resolved == {
        "preset": "custom",
        "minPeople": person_presets.MIN_PEOPLE_RANGE[1],
        "minBoxHeightRatio": 0.0,
        "minSessionSeconds": 45.0,
    }


def test_resolve_options_records_preset_and_numbers() -> None:
    """Both halves are persisted: the name alone would replay differently after
    a retune, the numbers alone would lose what the operator picked."""
    assert person_presets.resolve_options({"preset": "solo"}) == {
        "preset": "solo",
        "minPeople": 1,
        "minBoxHeightRatio": 0.4,
        "minSessionSeconds": 120.0,
    }
    assert person_presets.resolve_options(None)["preset"] == person_presets.DEFAULT_PRESET


def test_every_catalogued_preset_is_resolvable() -> None:
    described = person_presets.describe_presets()
    assert [item["id"] for item in described] == list(person_presets.PRESET_IDS)
    for item in described:
        resolved = person_presets.resolve(item["id"], strict=True)
        assert resolved.min_people == item["minPeople"]


# ---------------------------------------------------------------------------
# Recorded-video regression: the wide (two-person) camera
# ---------------------------------------------------------------------------


def test_pair_finds_fifteen_stations_on_the_wide_camera(common_cold) -> None:
    sessions = sessions_for(common_cold, COMMON_COLD_DURATION, person_presets.resolve("pair"))
    assert len(sessions) == 15
    starts = [round(item["start"]) for item in sessions]
    assert starts == [round(value) for value in COMMON_COLD_PAIR_STARTS]


def test_pair_strict_costs_the_wide_camera_nothing(common_cold) -> None:
    """The gate is only allowed to remove things that were not people: on a
    camera where both subjects are fully in frame it must change nothing."""
    sessions = sessions_for(common_cold, COMMON_COLD_DURATION, person_presets.resolve("pair_strict"))
    assert len(sessions) == 15
    for session, expected_start in zip(sessions, COMMON_COLD_PAIR_STARTS):
        assert abs(session["start"] - expected_start) <= 10.0


# ---------------------------------------------------------------------------
# Recorded-video regression: the tight (one-person) camera
# ---------------------------------------------------------------------------


def test_pair_on_a_one_person_camera_is_the_known_bad_baseline(hayfever) -> None:
    """Frozen deliberately: this is the failure the presets exist to fix. The
    old default finds four "sessions" of 50-93 s built entirely out of limbs
    intruding at the frame edge, and misses all 15 real stations."""
    sessions = sessions_for(hayfever, HAYFEVER_DURATION, person_presets.resolve("pair"))
    assert len(sessions) == 4
    assert all(item["end"] - item["start"] < 120.0 for item in sessions)


def test_pair_strict_finds_nothing_on_a_one_person_camera(hayfever) -> None:
    """Correct answer for a two-person rule on a one-person tape: no stations,
    rather than four false ones."""
    assert sessions_for(hayfever, HAYFEVER_DURATION, person_presets.resolve("pair_strict")) == []


def test_solo_finds_fifteen_stations_on_the_tight_camera(hayfever) -> None:
    sessions = sessions_for(hayfever, HAYFEVER_DURATION, person_presets.resolve("solo"))
    assert len(sessions) == 15
    assert [item["student_index"] for item in sessions] == list(range(1, 16))


def test_solo_agrees_with_the_other_camera_on_every_station(common_cold, hayfever) -> None:
    """The strongest check available without hand labels: two cameras, two
    different occupancy rules, the same 15 events within seconds of each other."""
    wide = sessions_for(common_cold, COMMON_COLD_DURATION, person_presets.resolve("pair"))
    tight = sessions_for(hayfever, HAYFEVER_DURATION, person_presets.resolve("solo"))
    assert len(wide) == len(tight) == 15
    deltas = [abs(a["start"] - b["start"]) for a, b in zip(wide, tight)]
    # Tolerance is seconds against stations ~8 minutes apart: the two cameras
    # see a student enter at slightly different moments, and each rule anchors
    # on its own first qualifying frame.
    assert max(deltas) <= 20.0, deltas
    assert sum(deltas) / len(deltas) <= 10.0, deltas


def test_one_person_rule_without_the_gate_is_unusable(hayfever) -> None:
    """Why `solo` is a preset and not just `--min-people 1`: with no gate the
    breaks between stations still contain a person, so the whole recording
    confirms as one enormous session."""
    ungated = person_presets.resolve("custom", min_people=1, min_box_height_ratio=0.0, min_session_seconds=0.0)
    sessions = sessions_for(hayfever, HAYFEVER_DURATION, ungated)
    assert any(item["end"] - item["start"] > 3000.0 for item in sessions)


def test_session_duration_guard_alone_kills_the_false_positives(hayfever) -> None:
    """The duration floor is an independent second lever: even with no height
    gate, requiring a station to last two minutes discards all four false
    sessions. Kept separate so a camera that needs one need not accept both."""
    duration_only = person_presets.resolve(
        "custom", min_people=2, min_box_height_ratio=0.0, min_session_seconds=120.0
    )
    assert sessions_for(hayfever, HAYFEVER_DURATION, duration_only) == []


@pytest.mark.parametrize("min_box_height_ratio", [0.32, 0.35, 0.40, 0.45, 0.50, 0.55])
def test_gate_threshold_sits_on_a_wide_plateau(common_cold, hayfever, min_box_height_ratio) -> None:
    """The shipped 0.40 is not a knife edge: every value across this range gives
    the right answer on both cameras. A future retune that leaves the plateau
    will fail here rather than in production."""
    pair = person_presets.resolve(
        "custom", min_people=2, min_box_height_ratio=min_box_height_ratio, min_session_seconds=120.0
    )
    solo = person_presets.resolve(
        "custom", min_people=1, min_box_height_ratio=min_box_height_ratio, min_session_seconds=120.0
    )
    assert len(sessions_for(common_cold, COMMON_COLD_DURATION, pair)) == 15
    assert sessions_for(hayfever, HAYFEVER_DURATION, pair) == []
    assert len(sessions_for(hayfever, HAYFEVER_DURATION, solo)) == 15


def test_confidence_alone_cannot_replace_the_gate(hayfever) -> None:
    """Documents the rejected alternative. Raising the confidence threshold
    removes real detections faster than it removes limbs: at 0.9 the tape still
    reports two people in places, and empty frames more than double."""
    frames = hayfever["frames"]
    pairs_at = {}
    empties_at = {}
    for threshold in (0.7, 0.9):
        counts = [sum(1 for box in frame if box[0] >= threshold) for frame in frames]
        pairs_at[threshold] = sum(1 for value in counts if value >= 2)
        empties_at[threshold] = sum(1 for value in counts if value == 0)
    assert pairs_at[0.9] > 0
    assert empties_at[0.9] > 2 * empties_at[0.7]
