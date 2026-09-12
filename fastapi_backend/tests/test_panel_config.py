"""The panel configuration value: parsing, validation and the marker key.

What matters is that a stored panel cannot silently become something else —
two samples of one model passing as a panel, a marker adopting another's file,
an adjudicator that is one of the disputing parties without anyone being told.
"""
from __future__ import annotations

import pytest
from app.llm.panel import (
    MarkingMode,
    PanelConfig,
    TieBreak,
    marker_key,
    parse_marking_mode,
)
from app.llm.routing import LLMTarget


def _panel(**overrides):
    base = {
        "markers": [
            {"providerId": "nvidia", "model": "nvidia/nemotron-3-super"},
            {"providerId": "gemini", "model": "gemini-2.5-pro"},
        ],
        "adjudicator": {"providerId": "deepseek", "model": "deepseek-chat"},
        "tieBreak": "lenient",
    }
    base.update(overrides)
    return PanelConfig.from_raw(base)


def test_round_trips_through_its_public_shape() -> None:
    panel = _panel(tieBreak="strict")

    restored = PanelConfig.from_raw(panel.to_public())

    assert restored == panel
    assert restored.markers == (
        LLMTarget("nvidia", "nvidia/nemotron-3-super"),
        LLMTarget("gemini", "gemini-2.5-pro"),
    )
    assert restored.adjudicator == LLMTarget("deepseek", "deepseek-chat")
    assert restored.tie_break is TieBreak.STRICT


def test_a_coherent_cross_vendor_panel_validates_clean() -> None:
    validation = _panel().validate()

    assert validation.ok
    assert validation.warnings == ()


def test_fewer_than_two_markers_is_an_error() -> None:
    validation = _panel(markers=[{"providerId": "nvidia", "model": "m"}]).validate()

    assert not validation.ok
    assert any("at least 2 markers" in error for error in validation.errors)


def test_the_same_model_twice_is_not_a_panel() -> None:
    validation = _panel(
        markers=[{"providerId": "gemini", "model": "gemini-2.5-pro"}] * 2
    ).validate()

    assert not validation.ok
    assert any("repeats marker 1" in error for error in validation.errors)


def test_a_missing_adjudicator_is_an_error() -> None:
    validation = _panel(adjudicator={}).validate()

    assert not validation.ok
    assert any("adjudicator" in error for error in validation.errors)


def test_two_markers_from_one_vendor_only_warn() -> None:
    validation = _panel(
        markers=[
            {"providerId": "openai", "model": "gpt-4.1"},
            {"providerId": "openai", "model": "gpt-4.1-mini"},
        ]
    ).validate()

    assert validation.ok
    assert any("share a provider" in warning for warning in validation.warnings)


def test_an_adjudicator_that_is_also_a_marker_only_warns() -> None:
    validation = _panel(adjudicator={"providerId": "gemini", "model": "gemini-2.5-pro"}).validate()

    assert validation.ok
    assert any("also a marker" in warning for warning in validation.warnings)


@pytest.mark.parametrize("raw", [None, "", 42, [], {"markers": "nope", "adjudicator": 7, "tieBreak": "coin"}])
def test_from_raw_never_raises_on_shape(raw: object) -> None:
    panel = PanelConfig.from_raw(raw)

    assert panel.markers == ()
    assert panel.adjudicator is None
    assert panel.tie_break is TieBreak.LENIENT
    assert not panel.validate().ok


def test_marker_key_is_filesystem_safe_and_distinct_per_target() -> None:
    assert marker_key(LLMTarget("gemini", "gemini-2.5-pro")) == "gemini__gemini-2-5-pro"
    assert marker_key(LLMTarget("nvidia", "nvidia/nemotron-3-super-120b-a12b")) == "nvidia__nvidia-nemotron-3-super-120b-a12b"
    # An empty model is the provider default, and still a distinct key from any named model.
    assert marker_key(LLMTarget("gemini", "")) == "gemini__default"
    assert marker_key(LLMTarget("Campus Gateway", "GPT 4.1")) == "campus-gateway__gpt-4-1"


@pytest.mark.parametrize(("raw", "expected"), [
    ("panel", MarkingMode.PANEL),
    (" PANEL ", MarkingMode.PANEL),
    ("single", MarkingMode.SINGLE),
    ("", MarkingMode.SINGLE),
    (None, MarkingMode.SINGLE),
    ("debate", MarkingMode.SINGLE),
])
def test_unknown_modes_read_as_single(raw: object, expected: MarkingMode) -> None:
    assert parse_marking_mode(raw) is expected
