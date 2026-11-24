"""Unit tests for the pure communication-rubric domain module."""
from __future__ import annotations

import copy
import sys
from pathlib import Path

import pytest

from app.domain import communication_rubric as cr
from app.domain.communication_rubric import (
    DEFAULT_SCORING_SCALE,
    RubricValidationError,
    build_rubric_payload,
    edited_provenance,
    normalize_scoring_scale,
    points_for_label,
    rubric_fingerprint,
    rubric_quality_issues,
    validate_edited_rubric,
)

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import parse_communication_rubric as parser  # noqa: E402


def make_criteria(count: int, sections: list | None = None) -> list[dict]:
    return [
        {
            "id": index,
            "section": (sections[index - 1] if sections else None),
            "label": f"Label {index}",
            "indicators": [f"Indicator {index}a", f"Indicator {index}b"],
        }
        for index in range(1, count + 1)
    ]


def edited(count: int = 3, **overrides) -> dict:
    raw = {
        "title": "My rubric",
        "notes": "n",
        "criteria": [{"label": f"Label {i}", "indicators": ["x"]} for i in range(1, count + 1)],
    }
    raw.update(overrides)
    return raw


def issues_of(raw) -> list[str]:
    with pytest.raises(RubricValidationError) as info:
        validate_edited_rubric(raw)
    return info.value.issues


# --- build_rubric_payload -------------------------------------------------


def test_build_default_scale_seven_criteria():
    sections = ["Communicative effectiveness"] * 3 + [None, "Other", None, "Other"]
    criteria = list(reversed(make_criteria(7, sections)))
    payload = build_rubric_payload(title="T", criteria=criteria, notes="N")
    assert list(payload) == [
        "schema", "title", "scoring_scale", "max_score", "pass_threshold",
        "criteria_count", "sections", "criteria", "notes",
    ]
    assert payload["schema"] == "communication-rubric-v1"
    assert payload["max_score"] == 21
    assert payload["pass_threshold"] == 11
    assert payload["criteria_count"] == 7
    assert [c["id"] for c in payload["criteria"]] == [1, 2, 3, 4, 5, 6, 7]
    assert payload["sections"] == [
        {"name": "Communicative effectiveness", "criteria_ids": [1, 2, 3]},
        {"name": "Uncategorized", "criteria_ids": [4, 6]},
        {"name": "Other", "criteria_ids": [5, 7]},
    ]


def test_build_returns_fresh_scale():
    payload = build_rubric_payload(title="T", criteria=make_criteria(2), notes="")
    assert payload["scoring_scale"] == DEFAULT_SCORING_SCALE
    assert payload["scoring_scale"] is not DEFAULT_SCORING_SCALE
    payload["scoring_scale"]["All"] = 99
    assert DEFAULT_SCORING_SCALE["All"] == 3


def test_build_custom_scale_five_criteria():
    scale = {"All": 4, "Most": 3, "Some": 1, "None": 0}
    payload = build_rubric_payload(
        title="T", criteria=make_criteria(5), notes="", scoring_scale=scale
    )
    assert payload["max_score"] == 20
    assert payload["pass_threshold"] == 10
    assert list(payload["scoring_scale"]) == ["All", "Most", "Some", "None"]


def test_build_seven_criteria_custom_scale_not_eleven():
    scale = {"All": 4, "Most": 3, "Some": 1, "None": 0}
    payload = build_rubric_payload(
        title="T", criteria=make_criteria(7), notes="", scoring_scale=scale
    )
    assert payload["max_score"] == 28
    assert payload["pass_threshold"] == 14


def test_build_explicit_threshold_wins():
    payload = build_rubric_payload(
        title="T", criteria=make_criteria(3), notes="", pass_threshold=0
    )
    assert payload["pass_threshold"] == 0


@pytest.mark.parametrize(
    ("count", "max_score", "expected"),
    [(7, 21, 11), (7, 28, 14), (5, 15, 8), (2, 6, 3), (3, 9, 5)],
)
def test_default_pass_threshold(count, max_score, expected):
    assert cr.default_pass_threshold(count, max_score) == expected


# --- parity with the script ----------------------------------------------


@pytest.mark.parametrize("count", [2, 5, 7, 9])
def test_parity_build_rubric_payload(count):
    sections = [("Communicative effectiveness" if i % 2 else None) for i in range(count)]
    criteria = make_criteria(count, sections)
    expected = parser.build_rubric_payload(
        title="T", criteria=copy.deepcopy(criteria), notes="N"
    )
    actual = build_rubric_payload(title="T", criteria=copy.deepcopy(criteria), notes="N")
    assert actual == expected
    assert list(actual) == list(expected)


@pytest.mark.parametrize(
    "payload",
    [
        build_rubric_payload(title="T", criteria=make_criteria(3), notes=""),
        {"criteria": []},
        {"criteria": [{"id": 1, "label": "a", "indicators": []}]},
        {"criteria": [{"id": 1, "label": " ", "indicators": []},
                      {"id": 1, "label": "b", "indicators": []}]},
        {"criteria": [{"id": 2, "label": "a", "indicators": ["x"]},
                      {"id": 4, "label": "b", "indicators": []}]},
        {"criteria": [{"id": 1, "label": "a", "indicators": []},
                      {"id": 2, "label": "b", "indicators": []}]},
    ],
)
def test_parity_quality_issues(payload):
    assert rubric_quality_issues(payload) == parser.rubric_quality_issues(payload)


def test_quality_issues_clean_payload_is_empty():
    payload = build_rubric_payload(title="T", criteria=make_criteria(3), notes="")
    assert rubric_quality_issues(payload) == []


@pytest.mark.parametrize(
    "name",
    ["", "  ", "communicative   EFFECTIVENESS", "Application of interpersonal "
     "communication to address problem", "some  odd name", "ALL CAPS"],
)
def test_parity_section_and_whitespace(name):
    assert cr.canonicalize_section_name(name) == parser.canonicalize_section_name(name)
    assert cr.normalize_whitespace(name) == parser.normalize_whitespace(name)


def test_canonicalize_blank_is_uncategorized():
    assert cr.canonicalize_section_name("   ") == "Uncategorized"


def test_normalize_whitespace_none():
    assert cr.normalize_whitespace(None) == ""
    assert cr.normalize_whitespace(" a \n b\t") == "a b"


# --- normalize_scoring_scale ---------------------------------------------


def test_scale_none_returns_mutation_safe_default():
    scale = normalize_scoring_scale(None)
    assert scale == DEFAULT_SCORING_SCALE
    scale["All"] = 50
    assert DEFAULT_SCORING_SCALE["All"] == 3


def test_scale_accepts_float_and_string_and_orders_keys():
    scale = normalize_scoring_scale(
        {"None": "0", "Some": 1.0, "Most": " 2 ", "All": 3}
    )
    assert scale == {"All": 3, "Most": 2, "Some": 1, "None": 0}
    assert list(scale) == ["All", "Most", "Some", "None"]


def test_scale_equal_levels_allowed_except_all_none():
    assert normalize_scoring_scale({"All": 2, "Most": 2, "Some": 2, "None": 0})["Most"] == 2


@pytest.mark.parametrize(
    "bad",
    [True, 2.5, -1, 101, "abc", "2.5", None.__class__, [1]],
)
def test_scale_rejects_bad_points(bad):
    raw = dict(DEFAULT_SCORING_SCALE, Most=bad)
    with pytest.raises(RubricValidationError) as info:
        normalize_scoring_scale(raw)
    assert any("Points for 'Most' must be a whole number from 0 to 100." == i
               for i in info.value.issues)


def test_scale_missing_key():
    raw = {"All": 3, "Most": 2, "Some": 1}
    with pytest.raises(RubricValidationError) as info:
        normalize_scoring_scale(raw)
    assert any("'None'" in i for i in info.value.issues)


def test_scale_extra_key():
    raw = dict(DEFAULT_SCORING_SCALE, Bonus=5)
    with pytest.raises(RubricValidationError) as info:
        normalize_scoring_scale(raw)
    assert any("Bonus" in i for i in info.value.issues)


@pytest.mark.parametrize("raw", ["x", 3, [1, 2], ()])
def test_scale_non_dict(raw):
    with pytest.raises(RubricValidationError):
        normalize_scoring_scale(raw)


def test_scale_increasing_rejected():
    with pytest.raises(RubricValidationError) as info:
        normalize_scoring_scale({"All": 3, "Most": 4, "Some": 1, "None": 0})
    assert "Points must not increase from 'All' down to 'Most'." in info.value.issues


def test_scale_all_equals_none_rejected():
    with pytest.raises(RubricValidationError) as info:
        normalize_scoring_scale({"All": 0, "Most": 0, "Some": 0, "None": 0})
    assert "'All' must be worth more than 'None'." in info.value.issues


def test_scale_reports_multiple_issues_together():
    with pytest.raises(RubricValidationError) as info:
        normalize_scoring_scale({"All": True, "Most": -1, "Some": 1, "None": 1000, "X": 1})
    issues = info.value.issues
    assert len(issues) >= 4
    assert str(info.value) == "; ".join(issues)


# --- validate_edited_rubric ----------------------------------------------


def test_validate_happy_path():
    raw = {
        "title": "  My   rubric ",
        "notes": " some \n notes ",
        "extraction": {"method": "llm"},
        "rubric_asset_id": "abc",
        "id": 9,
        "criteria": [
            {"id": 7, "section": "communicative  effectiveness", "label": " First  one ",
             "indicators": ["  a  b ", "", "   ", "c"]},
            {"id": 3, "section": "   ", "label": "Second", "indicators": []},
            {"id": 1, "section": None, "label": "Third"},
        ],
    }
    payload = validate_edited_rubric(raw)
    assert list(payload) == [
        "schema", "title", "scoring_scale", "max_score", "pass_threshold",
        "criteria_count", "sections", "criteria", "notes",
    ]
    assert payload["title"] == "My rubric"
    assert payload["notes"] == "some notes"
    assert [c["id"] for c in payload["criteria"]] == [1, 2, 3]
    assert payload["criteria"][0] == {
        "id": 1, "section": "Communicative effectiveness",
        "label": "First one", "indicators": ["a b", "c"],
    }
    assert payload["criteria"][1]["section"] is None
    assert payload["criteria"][2]["indicators"] == []
    assert payload["max_score"] == 9
    assert payload["pass_threshold"] == 5
    assert payload["sections"][0]["name"] == "Communicative effectiveness"
    assert payload["sections"][1]["name"] == "Uncategorized"


def test_validate_custom_scale_and_threshold():
    payload = validate_edited_rubric(
        edited(3, scoring_scale={"All": "5", "Most": 3, "Some": 1.0, "None": 0}, pass_threshold="8")
    )
    assert payload["max_score"] == 15
    assert payload["pass_threshold"] == 8


@pytest.mark.parametrize("threshold", [0, 9, "9", 9.0])
def test_validate_threshold_bounds_accepted(threshold):
    payload = validate_edited_rubric(edited(3, pass_threshold=threshold))
    assert payload["pass_threshold"] == int(threshold)


def test_validate_threshold_none_uses_default():
    assert validate_edited_rubric(edited(3, pass_threshold=None))["pass_threshold"] == 5


@pytest.mark.parametrize("raw", [None, [], "x", 5])
def test_validate_non_dict(raw):
    assert issues_of(raw)


def test_validate_criteria_not_a_list():
    assert any("Criteria must be a list" in i for i in issues_of(edited(criteria="nope")))
    assert any("Criteria must be a list" in i for i in issues_of({"title": "T"}))


def test_validate_too_few_criteria():
    assert any("At least 2" in i for i in issues_of(edited(1)))
    assert any("At least 2" in i for i in issues_of(edited(0)))


def test_validate_too_many_criteria():
    raw = edited(cr.MAX_CRITERIA + 1)
    assert any("At most 60" in i for i in issues_of(raw))
    assert validate_edited_rubric(edited(cr.MAX_CRITERIA))["criteria_count"] == 60


def test_validate_title_problems():
    assert any("title is empty" in i for i in issues_of(edited(title="  ")))
    assert any("title is empty" in i for i in issues_of(edited(title=None)))
    assert any("title is longer" in i for i in issues_of(edited(title="x" * 201)))
    assert validate_edited_rubric(edited(title="x" * 200))["title"] == "x" * 200
    assert any("title must be text" in i for i in issues_of(edited(title=5)))


def test_validate_label_problems():
    raw = edited(3)
    raw["criteria"][1]["label"] = "  "
    assert "Criterion 2: the label is empty." in issues_of(raw)
    raw["criteria"][1]["label"] = "x" * 301
    assert any(i.startswith("Criterion 2:") and "longer than 300" in i for i in issues_of(raw))
    raw["criteria"][1]["label"] = "x" * 300
    assert validate_edited_rubric(raw)


def test_validate_duplicate_labels_case_insensitive():
    raw = edited(3)
    raw["criteria"][2]["label"] = "  LABEL   1 "
    issues = issues_of(raw)
    assert any(i.startswith("Criterion 3:") and "criterion 1" in i for i in issues)


def test_validate_indicator_limits():
    raw = edited(2)
    raw["criteria"][0]["indicators"] = [f"i{n}" for n in range(31)]
    assert any("31 indicators" in i for i in issues_of(raw))
    raw["criteria"][0]["indicators"] = [f"i{n}" for n in range(30)]
    assert validate_edited_rubric(raw)
    raw["criteria"][0]["indicators"] = ["x" * 501]
    assert any("indicator 1 is longer than 500" in i for i in issues_of(raw))


def test_validate_blank_indicators_do_not_count_toward_limit():
    raw = edited(2)
    raw["criteria"][0]["indicators"] = ["a"] + [""] * 50
    assert validate_edited_rubric(raw)["criteria"][0]["indicators"] == ["a"]


def test_validate_section_too_long():
    raw = edited(2)
    raw["criteria"][0]["section"] = "s" * 201
    assert any("section is longer than 200" in i for i in issues_of(raw))


def test_validate_notes_too_long():
    assert any("notes are longer" in i for i in issues_of(edited(notes="n" * 5001)))
    assert validate_edited_rubric(edited(notes="n" * 5000))


@pytest.mark.parametrize("threshold", [10, -1])
def test_validate_threshold_out_of_range(threshold):
    assert issues_of(edited(3, pass_threshold=threshold))


def test_validate_threshold_greater_than_max_uses_scale():
    scale = {"All": 1, "Most": 1, "Some": 1, "None": 0}
    assert issues_of(edited(3, scoring_scale=scale, pass_threshold=4))
    assert validate_edited_rubric(edited(3, scoring_scale=scale, pass_threshold=3))


@pytest.mark.parametrize("threshold", [True, False, 2.5, "x", [1], {}])
def test_validate_threshold_wrong_type(threshold):
    assert any("whole number" in i for i in issues_of(edited(3, pass_threshold=threshold)))


def test_validate_bad_scale_is_reported_with_criteria_problems():
    raw = edited(3, scoring_scale={"All": 3}, pass_threshold=2)
    raw["criteria"][0]["label"] = ""
    issues = issues_of(raw)
    assert any("'Most'" in i for i in issues)
    assert "Criterion 1: the label is empty." in issues


def test_validate_collects_everything_in_one_error():
    raw = {
        "title": "",
        "notes": "n" * 6000,
        "scoring_scale": {"All": -1, "Most": 0, "Some": 0, "None": 0},
        "pass_threshold": True,
        "criteria": [
            {"label": ""},
            {"label": "dup"},
            {"label": "DUP", "indicators": ["x" * 600]},
            "not a dict",
            {"label": "ok", "indicators": "not a list"},
            {"label": "ok2", "indicators": [1, None]},
        ],
    }
    with pytest.raises(RubricValidationError) as info:
        validate_edited_rubric(raw)
    issues = info.value.issues
    assert len(issues) >= 9
    joined = " | ".join(issues)
    for position in (1, 3, 4, 5, 6):
        assert f"Criterion {position}:" in joined
    assert "title is empty" in joined
    assert "notes are longer" in joined
    assert "whole number" in joined


def test_validate_defensive_types_do_not_raise_other_exceptions():
    for criteria in ([None, 5, [], "s"], [{"label": 5}, {"label": "a", "section": 3}]):
        issues = issues_of(edited(criteria=criteria))
        assert issues


def test_validate_output_has_no_unknown_keys():
    raw = edited(2, extraction={"method": "llm"}, rubric_asset_id="r", id=1, schema="evil")
    raw["criteria"][0].update({"id": 5, "extra": "x", "rubric_asset_id": "r"})
    payload = validate_edited_rubric(raw)
    assert set(payload) == {
        "schema", "title", "scoring_scale", "max_score", "pass_threshold",
        "criteria_count", "sections", "criteria", "notes",
    }
    assert payload["schema"] == "communication-rubric-v1"
    assert all(set(c) == {"id", "section", "label", "indicators"} for c in payload["criteria"])


def test_validate_does_not_mutate_input():
    raw = edited(3)
    snapshot = copy.deepcopy(raw)
    validate_edited_rubric(raw)
    assert raw == snapshot


# --- points_for_label ----------------------------------------------------


@pytest.mark.parametrize(
    ("label", "expected"),
    [("All", 4), ("Most", 3), ("Some", 1), ("None", 0), ("Bogus", 0), ("", 0)],
)
def test_points_for_label(label, expected):
    assert points_for_label({"All": 4, "Most": 3, "Some": 1, "None": 0}, label) == expected


def test_points_for_label_garbage_scale():
    assert points_for_label({"All": "x"}, "All") == 0
    assert points_for_label(None, "All") == 0  # type: ignore[arg-type]


# --- rubric_fingerprint --------------------------------------------------


def base_payload() -> dict:
    return build_rubric_payload(title="T", criteria=make_criteria(3), notes="N")


def test_fingerprint_is_sha256_hex_and_stable():
    fingerprint = rubric_fingerprint(base_payload())
    assert len(fingerprint) == 64
    int(fingerprint, 16)
    assert fingerprint == rubric_fingerprint(base_payload())


def test_fingerprint_ignores_key_order_title_notes_extraction():
    payload = base_payload()
    other = {key: payload[key] for key in reversed(list(payload))}
    other["title"] = "Different"
    other["notes"] = "Other notes"
    other["extraction"] = {"method": "edited"}
    other["criteria"] = [dict(reversed(list(c.items()))) for c in payload["criteria"]]
    assert rubric_fingerprint(other) == rubric_fingerprint(payload)


def test_fingerprint_changes_with_label():
    payload = base_payload()
    other = copy.deepcopy(payload)
    other["criteria"][0]["label"] = "changed"
    assert rubric_fingerprint(other) != rubric_fingerprint(payload)


def test_fingerprint_changes_with_indicator():
    payload = base_payload()
    other = copy.deepcopy(payload)
    other["criteria"][1]["indicators"].append("new")
    assert rubric_fingerprint(other) != rubric_fingerprint(payload)


def test_fingerprint_changes_with_section():
    payload = base_payload()
    other = copy.deepcopy(payload)
    other["criteria"][0]["section"] = "Z"
    assert rubric_fingerprint(other) != rubric_fingerprint(payload)


def test_fingerprint_changes_with_scale():
    payload = base_payload()
    other = copy.deepcopy(payload)
    other["scoring_scale"]["Most"] = 1
    assert rubric_fingerprint(other) != rubric_fingerprint(payload)


def test_fingerprint_changes_with_threshold():
    payload = base_payload()
    other = copy.deepcopy(payload)
    other["pass_threshold"] += 1
    assert rubric_fingerprint(other) != rubric_fingerprint(payload)


def test_fingerprint_tolerates_missing_keys():
    assert len(rubric_fingerprint({})) == 64
    assert len(rubric_fingerprint({"criteria": [{"id": 1}]})) == 64


# --- edited_provenance ---------------------------------------------------

EDITOR = {"userId": "u1", "username": "alice", "displayName": "Alice"}
WHEN = "2026-01-01T00:00:00Z"


def test_provenance_from_llm_keeps_only_whitelisted_keys():
    previous = {
        "method": "llm", "model": "m", "provider": "p", "prompt_version": "v1",
        "deterministic_error": "boom", "warnings": ["w"],
    }
    block = edited_provenance(previous, edited_by=EDITOR, edited_at=WHEN)
    assert block == {
        "method": "edited",
        "edited_by": EDITOR,
        "edited_at": WHEN,
        "source": {"method": "llm", "model": "m", "provider": "p", "prompt_version": "v1"},
    }


def test_provenance_from_deterministic():
    block = edited_provenance({"method": "deterministic"}, edited_by=EDITOR, edited_at=WHEN)
    assert block["source"] == {"method": "deterministic"}


def test_provenance_carries_forward_edited_source():
    first = edited_provenance(
        {"method": "llm", "model": "m", "provider": "p", "prompt_version": "v1"},
        edited_by=EDITOR, edited_at=WHEN,
    )
    second = edited_provenance(first, edited_by=None, edited_at="later")
    assert second["source"] == first["source"]
    assert "source" not in second["source"]
    assert second["edited_at"] == "later"
    assert second["edited_by"] is None


@pytest.mark.parametrize("garbage", [None, "llm", 5, [], {}, {"method": 5}, {"model": "m"}])
def test_provenance_garbage_is_deterministic(garbage):
    block = edited_provenance(garbage, edited_by=None, edited_at=WHEN)
    assert block["method"] == "edited"
    assert block["edited_by"] is None
    assert block["source"] == {"method": "deterministic"}


def test_provenance_edited_with_garbage_source():
    block = edited_provenance(
        {"method": "edited", "source": "junk"}, edited_by=EDITOR, edited_at=WHEN
    )
    assert block["source"] == {"method": "deterministic"}
    nested = edited_provenance(
        {"method": "edited", "source": {"method": "edited", "source": {"method": "llm"}}},
        edited_by=EDITOR, edited_at=WHEN,
    )
    assert nested["source"] == {"method": "deterministic"}
