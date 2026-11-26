"""The communication rubric document: its scoring rules and its validation.

One module owns what a ``communication-rubric-v1`` payload is, so the three
writers of one — the deterministic PDF parser, the LLM fallback, and an
administrator editing the extracted rubric in the browser — cannot disagree
about ids, sections, the score scale, the maximum or the pass mark.

Imported by the API (``RubricService``) and by ``scripts/
parse_communication_rubric.py`` / ``scripts/nvidia_osce_communication.py``
(which reach ``fastapi_backend`` through ``llm_bootstrap``). Pure: stdlib only.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from enum import StrEnum
from typing import Any

SCHEMA = "communication-rubric-v1"

# The four labels the communication scorer may award, best first. The labels
# are fixed (the scorer's prompt is written around them); the POINTS each is
# worth are part of the rubric and may be edited.
SCORE_LABELS: tuple[str, ...] = ("All", "Most", "Some", "None")
DEFAULT_SCORING_SCALE: dict[str, int] = {"All": 3, "Most": 2, "Some": 1, "None": 0}
MAX_LEVEL_POINTS = 100

MIN_CRITERIA = 2
MAX_CRITERIA = 60
MAX_INDICATORS_PER_CRITERION = 30
MAX_LABEL_CHARS = 300
MAX_INDICATOR_CHARS = 500
MAX_TITLE_CHARS = 200
MAX_SECTION_CHARS = 200
MAX_NOTES_CHARS = 5000
UNCATEGORIZED = "Uncategorized"


class RubricVersionStatus(StrEnum):
    """A stored rubric version is the one scoring uses, or it is history.
    Exactly one version is active once any exists."""

    ACTIVE = "active"
    RETIRED = "retired"


class RubricChangeKind(StrEnum):
    """What produced a version — the version history's "what happened"."""

    ADOPTED = "adopted"  # the rubric already on disk when versioning began
    DEFAULT = "default"  # parsed from the bundled PDF on a fresh install
    UPLOAD = "upload"  # a replacement PDF, parsed and activated at once
    RESET = "reset"  # back to the bundled PDF
    DRAFT = "draft"  # an extraction draft reviewed and saved
    EDIT = "edit"  # the active rubric edited in place
    RESTORE = "restore"  # an earlier version made active again


# Keys that describe where the document is materialised, not what it says. The
# version store keeps the document without them; they are written onto the
# active file (``rubric_version``) when it is materialised.
MATERIALISED_KEYS: tuple[str, ...] = ("rubric_version",)


def stored_document(payload: dict[str, Any]) -> dict[str, Any]:
    """The rubric document as a version row keeps it (materialised keys dropped)."""
    return {key: value for key, value in payload.items() if key not in MATERIALISED_KEYS}


class RubricValidationError(ValueError):
    """An edited rubric that cannot be saved. ``issues`` lists every reason."""

    def __init__(self, issues: list[str]) -> None:
        super().__init__("; ".join(issues))
        self.issues = list(issues)


CANONICAL_SECTION_NAMES = {
    "communicative effectiveness": "Communicative effectiveness",
    "application of interpersonal communication to address problems": (
        "Application of interpersonal communication to address problems"
    ),
    "application of interpersonal communication to address problem": (
        "Application of interpersonal communication to address problems"
    ),
}

_INT_TEXT = re.compile(r"^[+-]?\d+$")


def normalize_whitespace(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def canonicalize_section_name(raw_name: str) -> str:
    """Known section spellings map to one canonical name; others are
    whitespace-normalised and capitalised; blank -> ``UNCATEGORIZED``."""
    cleaned = normalize_whitespace(raw_name).lower()
    if cleaned in CANONICAL_SECTION_NAMES:
        return CANONICAL_SECTION_NAMES[cleaned]
    return normalize_whitespace(raw_name).capitalize() or UNCATEGORIZED


def default_pass_threshold(criterion_count: int, max_score: int) -> int:
    """11 for the canonical 7-criterion / 21-point rubric, else ceil(max/2)."""
    if criterion_count == 7 and max_score == 21:
        return 11
    return math.ceil(max_score / 2)


def _coerce_int(value: Any) -> int | None:
    """A whole number from an int, an integral float or a numeric string."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() else None
    if isinstance(value, str):
        text = value.strip()
        return int(text) if _INT_TEXT.match(text) else None
    return None


def normalize_scoring_scale(raw: Any) -> dict[str, int]:
    """A valid scale or ``RubricValidationError``.

    ``None`` -> ``DEFAULT_SCORING_SCALE``. Otherwise exactly the four
    ``SCORE_LABELS`` keys, integer points 0..MAX_LEVEL_POINTS (bool rejected,
    integral floats/strings accepted), non-increasing All >= Most >= Some >=
    None, and All > None (a scale that cannot separate anyone is refused).
    """
    if raw is None:
        return dict(DEFAULT_SCORING_SCALE)
    if not isinstance(raw, dict):
        raise RubricValidationError(["The scoring scale must be an object of points per level."])
    issues: list[str] = []
    for key in raw:
        if key not in SCORE_LABELS:
            issues.append(f"Unknown scoring level '{key}'.")
    values: dict[str, int] = {}
    for label in SCORE_LABELS:
        if label not in raw:
            issues.append(f"Points for '{label}' are missing.")
            continue
        number = _coerce_int(raw[label])
        if number is None or not 0 <= number <= MAX_LEVEL_POINTS:
            issues.append(
                f"Points for '{label}' must be a whole number from 0 to {MAX_LEVEL_POINTS}."
            )
            continue
        values[label] = number
    if len(values) == len(SCORE_LABELS):
        for higher, lower in zip(SCORE_LABELS, SCORE_LABELS[1:]):
            if values[higher] < values[lower]:
                issues.append(f"Points must not increase from '{higher}' down to '{lower}'.")
        if values["All"] == values["None"]:
            issues.append("'All' must be worth more than 'None'.")
    if issues:
        raise RubricValidationError(issues)
    return {label: values[label] for label in SCORE_LABELS}


def build_rubric_payload(
    *,
    title: str,
    criteria: list[dict[str, Any]],
    notes: str,
    scoring_scale: dict[str, int] | None = None,
    pass_threshold: int | None = None,
) -> dict[str, Any]:
    """The one place a payload is assembled.

    ``criteria`` items carry ``id``, ``section`` (or None), ``label``,
    ``indicators``; ordered by id. Returns ``{schema, title, scoring_scale,
    max_score = len(criteria) * scale["All"], pass_threshold (given, else
    default_pass_threshold), criteria_count, sections: [{name, criteria_ids}]
    (None section grouped as UNCATEGORIZED, first-appearance order), criteria,
    notes}``. Must reproduce the previous parser output exactly for the
    default scale (the shipped PHR1012 rubric: 7 criteria, 21, 11).
    """
    scale = normalize_scoring_scale(scoring_scale)
    ordered = sorted(criteria, key=lambda item: int(item.get("id", 0)))

    section_to_criteria: dict[str, list[int]] = {}
    for criterion in ordered:
        name = criterion.get("section") or UNCATEGORIZED
        section_to_criteria.setdefault(name, []).append(int(criterion["id"]))
    sections = [
        {"name": name, "criteria_ids": ids} for name, ids in section_to_criteria.items()
    ]

    count = len(ordered)
    max_score = count * scale["All"]
    threshold = (
        pass_threshold if pass_threshold is not None else default_pass_threshold(count, max_score)
    )
    return {
        "schema": SCHEMA,
        "title": title,
        "scoring_scale": scale,
        "max_score": max_score,
        "pass_threshold": threshold,
        "criteria_count": count,
        "sections": sections,
        "criteria": ordered,
        "notes": notes,
    }


def rubric_quality_issues(payload: dict[str, Any]) -> list[str]:
    """Why a parse that did not raise is still unusable (empty = fine):
    < MIN_CRITERIA criteria, duplicate ids, ids not contiguous from 1, an
    empty label, no indicators on any criterion."""
    criteria = payload.get("criteria") or []
    issues: list[str] = []
    if len(criteria) < MIN_CRITERIA:
        issues.append(
            f"only {len(criteria)} criterion found (expected at least {MIN_CRITERIA})"
        )
    ids = [criterion.get("id") for criterion in criteria]
    if len(set(ids)) != len(ids):
        issues.append("duplicate criterion numbers")
    elif sorted(ids) != list(range(1, len(ids) + 1)):
        issues.append(f"criterion numbers are not contiguous from 1: {sorted(ids)}")
    for criterion in criteria:
        if not normalize_whitespace(criterion.get("label", "")):
            issues.append(f"criterion {criterion.get('id')} has an empty label")
    if criteria and not any(criterion.get("indicators") for criterion in criteria):
        issues.append("no performance indicators were recognised on any criterion")
    return issues


def _clean_criterion(
    position: int, raw: Any, seen_labels: dict[str, int], issues: list[str]
) -> dict[str, Any] | None:
    prefix = f"Criterion {position}:"
    if not isinstance(raw, dict):
        issues.append(f"{prefix} must be an object with a label and indicators.")
        return None

    label_raw = raw.get("label")
    label = ""
    if label_raw is not None and not isinstance(label_raw, str):
        issues.append(f"{prefix} the label must be text.")
    else:
        label = normalize_whitespace(label_raw)
        if not label:
            issues.append(f"{prefix} the label is empty.")
        elif len(label) > MAX_LABEL_CHARS:
            issues.append(f"{prefix} the label is longer than {MAX_LABEL_CHARS} characters.")
        else:
            key = label.casefold()
            if key in seen_labels:
                issues.append(f"{prefix} the label repeats criterion {seen_labels[key]}.")
            else:
                seen_labels[key] = position

    section_raw = raw.get("section")
    section: str | None = None
    if section_raw is not None and not isinstance(section_raw, str):
        issues.append(f"{prefix} the section must be text.")
    else:
        section_text = normalize_whitespace(section_raw)
        if len(section_text) > MAX_SECTION_CHARS:
            issues.append(f"{prefix} the section is longer than {MAX_SECTION_CHARS} characters.")
        elif section_text:
            section = canonicalize_section_name(section_text)

    indicators: list[str] = []
    indicators_raw = raw.get("indicators")
    if indicators_raw is None:
        indicators_raw = []
    if not isinstance(indicators_raw, list):
        issues.append(f"{prefix} the indicators must be a list of text.")
    else:
        for index, item in enumerate(indicators_raw, start=1):
            if not isinstance(item, str):
                issues.append(f"{prefix} indicator {index} must be text.")
                continue
            text = normalize_whitespace(item)
            if not text:
                continue
            if len(text) > MAX_INDICATOR_CHARS:
                issues.append(
                    f"{prefix} indicator {index} is longer than {MAX_INDICATOR_CHARS} characters."
                )
                continue
            indicators.append(text)
        if len(indicators) > MAX_INDICATORS_PER_CRITERION:
            issues.append(
                f"{prefix} has {len(indicators)} indicators "
                f"(at most {MAX_INDICATORS_PER_CRITERION})."
            )

    return {"id": position, "section": section, "label": label, "indicators": indicators}


def validate_edited_rubric(raw: Any) -> dict[str, Any]:
    """Turn an administrator's edited rubric into a canonical payload, or
    raise ``RubricValidationError`` listing every problem at once.

    Accepts ``{title, notes?, scoring_scale?, pass_threshold?, criteria:
    [{section?, label, indicators?: [str]}]}`` (any ``id`` on input is ignored:
    criteria are renumbered 1..N in the order given). Normalises whitespace,
    drops blank indicators, canonicalises sections, enforces the MAX_* limits,
    MIN/MAX_CRITERIA, non-empty title/labels, case-insensitive duplicate
    labels, and 0 <= pass_threshold <= max_score (integer). At least one
    indicator in total is NOT required here (a hand-written rubric may be
    label-only). Builds through ``build_rubric_payload``. Never carries
    unknown keys through (no ``extraction``/asset keys: the caller adds
    provenance).
    """
    if not isinstance(raw, dict):
        raise RubricValidationError(["The rubric must be an object."])
    issues: list[str] = []

    title_raw = raw.get("title")
    title = ""
    if title_raw is not None and not isinstance(title_raw, str):
        issues.append("The title must be text.")
    else:
        title = normalize_whitespace(title_raw)
        if not title:
            issues.append("The title is empty.")
        elif len(title) > MAX_TITLE_CHARS:
            issues.append(f"The title is longer than {MAX_TITLE_CHARS} characters.")

    notes_raw = raw.get("notes")
    notes = ""
    if notes_raw is not None and not isinstance(notes_raw, str):
        issues.append("The notes must be text.")
    else:
        notes = normalize_whitespace(notes_raw)
        if len(notes) > MAX_NOTES_CHARS:
            issues.append(f"The notes are longer than {MAX_NOTES_CHARS} characters.")

    scale: dict[str, int] | None = None
    try:
        scale = normalize_scoring_scale(raw.get("scoring_scale"))
    except RubricValidationError as error:
        issues.extend(error.issues)

    criteria: list[dict[str, Any]] = []
    criteria_raw = raw.get("criteria")
    count_known = isinstance(criteria_raw, list)
    if not count_known:
        issues.append("Criteria must be a list.")
    else:
        if len(criteria_raw) < MIN_CRITERIA:
            issues.append(f"At least {MIN_CRITERIA} criteria are required.")
        elif len(criteria_raw) > MAX_CRITERIA:
            issues.append(f"At most {MAX_CRITERIA} criteria are allowed.")
        seen_labels: dict[str, int] = {}
        for position, item in enumerate(criteria_raw, start=1):
            cleaned = _clean_criterion(position, item, seen_labels, issues)
            if cleaned is not None:
                criteria.append(cleaned)

    threshold: int | None = None
    threshold_raw = raw.get("pass_threshold")
    if threshold_raw is not None:
        threshold = _coerce_int(threshold_raw)
        if threshold is None:
            issues.append("The pass mark must be a whole number.")
        elif threshold < 0:
            issues.append("The pass mark must not be negative.")
        elif scale is not None and count_known:
            max_score = len(criteria_raw) * scale["All"]
            if threshold > max_score:
                issues.append(
                    f"The pass mark ({threshold}) must not exceed the maximum score ({max_score})."
                )

    if issues:
        raise RubricValidationError(issues)
    return build_rubric_payload(
        title=title,
        criteria=criteria,
        notes=notes,
        scoring_scale=scale,
        pass_threshold=threshold,
    )


def points_for_label(scale: dict[str, int], label: str) -> int:
    """Points the scale awards a score label; 0 for an unknown label."""
    try:
        return int(scale.get(label, 0))
    except (AttributeError, TypeError, ValueError):
        return 0


def rubric_fingerprint(payload: dict[str, Any]) -> str:
    """SHA-256 hex over what scoring depends on — the criteria (id, section,
    label, indicators), the scoring scale and the pass threshold, as canonical
    JSON (sorted keys, no whitespace). Stable across re-saves of identical
    content; changes when an edit changes anything a mark could depend on.
    Title/notes/provenance do not count. Stored as ``rubric_revision``."""
    criteria = [
        {
            "id": item.get("id"),
            "section": item.get("section"),
            "label": item.get("label"),
            "indicators": item.get("indicators") or [],
        }
        for item in (payload.get("criteria") or [])
    ]
    document = {
        "criteria": criteria,
        "scoring_scale": payload.get("scoring_scale") or {},
        "pass_threshold": payload.get("pass_threshold"),
    }
    text = json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


_SOURCE_KEYS = ("method", "model", "provider", "prompt_version")


def _source_of(block: Any) -> dict[str, Any]:
    if not isinstance(block, dict) or not isinstance(block.get("method"), str):
        return {"method": "deterministic"}
    if block["method"] == "edited":
        return _source_of_unedited(block.get("source"))
    return {key: block[key] for key in _SOURCE_KEYS if key in block}


def _source_of_unedited(block: Any) -> dict[str, Any]:
    if isinstance(block, dict) and block.get("method") == "edited":
        return {"method": "deterministic"}
    return _source_of(block)


def edited_provenance(
    previous: Any, *, edited_by: dict[str, Any] | None, edited_at: str
) -> dict[str, Any]:
    """The ``extraction`` block a rubric saved from the editor carries:
    ``{"method": "edited", "edited_by": {userId, username, displayName} | None,
    "edited_at": edited_at, "source": <how the text was first read>}``.

    ``source`` is ``previous`` with only ``method``/``model``/``provider``/
    ``prompt_version`` kept; when ``previous`` is itself an ``edited`` block its
    own ``source`` is carried forward (no nesting chain); a missing/garbage
    ``previous`` -> ``{"method": "deterministic"}``.
    """
    editor = None
    if isinstance(edited_by, dict):
        editor = {key: edited_by.get(key) for key in ("userId", "username", "displayName")}
    return {
        "method": "edited",
        "edited_by": editor,
        "edited_at": edited_at,
        "source": _source_of(previous),
    }
