"""LLM fallback for the case-study rubric, and the single-flight lock it needs.

The deterministic parse stays first and free. Only when it fails on a PDF that
has readable text is a model asked, and everything that model could do to the
panel (two markers extracting different criteria lists) is closed off by
sharing one extraction through the cache lock. ``rubric_llm_fallback`` is
mocked throughout: its own behaviour is tested with it.
"""
from __future__ import annotations

import json
import runpy
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import case_study_rubric  # noqa: E402
import rubric_llm_fallback  # noqa: E402

load = case_study_rubric.load_case_study_rubric

# Long enough to count as extractable text, and with no "Analytical Checklist"
# header, so the deterministic parser finds no rubric section.
UNKNOWN_LAYOUT = (
    "[Page 1]\nA 24-year-old presents with a sore throat and a rash on the arms. The student "
    "must take a history and explain the plan to the patient in plain language.\n\n"
    "[Page 3]\nMarking guide for examiners\n"
    "Introduces self and confirms identity (critical)\n"
    "Asks about drug allergies\n"
    "Explains the management plan\n"
)

DETERMINISTIC_TEXT = (
    "[Page 1]\nPatient presents with an itchy rash on the foot.\n\n"
    "[Page 4]\nAnalytical Checklist\nGATHERING INFORMATION/ INTRODUCTION YES NO\n"
    "1. Elicits onset and duration.\n2. Asks about other symptoms.\n"
    "3. Checks medication history.\n"
)


@dataclass(frozen=True)
class FakeExtraction:
    payload: dict[str, Any]
    model: str = "fake-model"
    provider: str = "fake"
    warnings: list[str] = field(default_factory=list)

    def provenance(self, *, deterministic_error: str) -> dict[str, Any]:
        return {
            "method": "llm",
            "model": self.model,
            "provider": self.provider,
            "prompt_version": rubric_llm_fallback.RUBRIC_EXTRACTION_PROMPT_VERSION,
            "deterministic_error": deterministic_error,
            "warnings": list(self.warnings),
        }


LLM_PAYLOAD = {
    "criteria": [
        {"label": "Introduces self and confirms identity", "is_critical": True},
        {"label": "Asks about drug allergies", "is_critical": False},
        {"label": "Explains the management plan", "is_critical": False},
    ],
    "rubric_start_quote": "marking   GUIDE for examiners",
}


class LlmDouble:
    def __init__(self, result: Any = None) -> None:
        self.calls: list[tuple[str, str]] = []
        self.result = FakeExtraction(LLM_PAYLOAD) if result is None else result

    def __call__(self, kind: str, document_text: str, *, router: Any = None):
        # Like the real extractor, which refuses when the operator switched it off.
        if not rubric_llm_fallback.fallback_enabled():
            raise rubric_llm_fallback.RubricLlmFallbackUnavailable("disabled")
        self.calls.append((kind, document_text))
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


@pytest.fixture()
def llm(monkeypatch) -> LlmDouble:
    double = LlmDouble()
    monkeypatch.setattr(rubric_llm_fallback, "extract_rubric_with_llm", double)
    return double


def write_case(tmp_path: Path, text: str, name: str = "case.txt") -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def test_deterministic_success_makes_no_llm_call(tmp_path: Path, llm: LlmDouble) -> None:
    rubric = load(write_case(tmp_path, DETERMINISTIC_TEXT))
    assert llm.calls == []
    assert rubric.method == "deterministic"
    assert rubric.extraction == {"method": "deterministic"}


def test_missing_section_falls_back_to_the_llm(tmp_path: Path, llm: LlmDouble) -> None:
    rubric = load(write_case(tmp_path, UNKNOWN_LAYOUT))

    assert [call[0] for call in llm.calls] == ["content"]
    assert rubric.source == "extracted"
    assert rubric.method == "llm"
    assert rubric.criteria == LLM_PAYLOAD["criteria"]
    assert rubric.criteria[0]["is_critical"] is True
    assert rubric.rubric_section_text == (
        "1. Introduces self and confirms identity [CRITICAL CRITERIA]\n"
        "2. Asks about drug allergies\n"
        "3. Explains the management plan"
    )
    assert rubric.extraction["model"] == "fake-model"
    assert "Analytical Checklist" in rubric.extraction["deterministic_error"]


def test_context_is_the_text_before_the_start_quote(tmp_path: Path, llm: LlmDouble) -> None:
    rubric = load(write_case(tmp_path, UNKNOWN_LAYOUT))
    assert "sore throat" in rubric.context_text
    assert "Marking guide" not in rubric.context_text
    assert "Asks about drug allergies" not in rubric.context_text


@pytest.mark.parametrize("quote", ["this text is nowhere in the document", None, ""])
def test_unlocatable_start_quote_keeps_the_whole_text(tmp_path: Path, quote, llm: LlmDouble) -> None:
    llm.result = FakeExtraction({**LLM_PAYLOAD, "rubric_start_quote": quote})
    rubric = load(write_case(tmp_path, UNKNOWN_LAYOUT))
    assert "Marking guide" in rubric.context_text
    assert "sore throat" in rubric.context_text


def test_too_few_criteria_falls_back_to_the_llm(tmp_path: Path, llm: LlmDouble) -> None:
    text = DETERMINISTIC_TEXT.replace("2. Asks about other symptoms.\n3. Checks medication history.\n", "") + (
        "Additional examiner notes that pad this document out so it is clearly readable text. " * 3
    )
    rubric = load(write_case(tmp_path, text))

    assert len(llm.calls) == 1
    assert rubric.method == "llm"
    assert "Found 1 criteria" in rubric.extraction["deterministic_error"]


def test_scanned_pdf_never_reaches_the_llm(tmp_path: Path, llm: LlmDouble) -> None:
    with pytest.raises(ValueError) as error:
        load(write_case(tmp_path, "[Page 1]\n[Page 2]\n tiny "))
    assert "scanned" in str(error.value)
    assert llm.calls == []


def test_unavailable_fallback_leaves_the_original_message(tmp_path: Path, llm: LlmDouble) -> None:
    path = write_case(tmp_path, UNKNOWN_LAYOUT)
    with pytest.raises(ValueError) as without_fallback:
        load(path, allow_llm_fallback=False)
    llm.result = rubric_llm_fallback.RubricLlmFallbackUnavailable("disabled")
    with pytest.raises(ValueError) as unavailable:
        load(path)
    assert str(unavailable.value) == str(without_fallback.value)
    assert "disabled" not in str(unavailable.value)


def test_failed_fallback_combines_both_reasons(tmp_path: Path, llm: LlmDouble) -> None:
    llm.result = rubric_llm_fallback.RubricLlmFallbackFailed("bad JSON after 2 repairs")
    path = write_case(tmp_path, UNKNOWN_LAYOUT)
    with pytest.raises(ValueError) as original:
        load(path, allow_llm_fallback=False)
    with pytest.raises(ValueError) as combined:
        load(path)
    assert str(combined.value) == (
        f"{original.value} LLM fallback extraction also failed: bad JSON after 2 repairs"
    )


def test_allow_llm_fallback_false_makes_no_call(tmp_path: Path, llm: LlmDouble) -> None:
    with pytest.raises(ValueError):
        load(write_case(tmp_path, UNKNOWN_LAYOUT), allow_llm_fallback=False)
    assert llm.calls == []


def test_llm_result_is_cached_and_adopted(tmp_path: Path, llm: LlmDouble) -> None:
    path = write_case(tmp_path, UNKNOWN_LAYOUT)
    cache = tmp_path / "cache"
    first = load(path, cache_dir=cache)
    second = load(path, cache_dir=cache)
    # The adjudicator's read: no fallback allowed, adopts the markers' entry.
    third = load(path, cache_dir=cache, allow_llm_fallback=False)

    assert len(llm.calls) == 1
    assert first.source == "extracted"
    assert second.source == third.source == "cache"
    assert second.extraction == third.extraction == first.extraction
    assert second.method == "llm"
    assert second.criteria == first.criteria
    assert second.rubric_section_text == first.rubric_section_text


def test_legacy_entry_without_extraction_reads_as_deterministic(tmp_path: Path, llm: LlmDouble) -> None:
    path = write_case(tmp_path, DETERMINISTIC_TEXT)
    cache = tmp_path / "cache"
    first = load(path, cache_dir=cache)
    entry = case_study_rubric.entry_path(cache, first.digest)
    payload = json.loads(entry.read_text(encoding="utf-8"))
    del payload["extraction"]
    entry.write_text(json.dumps(payload), encoding="utf-8")

    again = load(path, cache_dir=cache)
    assert again.source == "cache"
    assert again.extraction == {"method": "deterministic"}


@pytest.mark.parametrize("bad", ["llm", ["llm"], {"model": "x"}, {"method": 3}, None])
def test_malformed_extraction_makes_the_entry_unusable(tmp_path: Path, bad, llm: LlmDouble) -> None:
    """Defaulting would let an unlabelled (possibly LLM) result read as a
    deterministic parse, so the entry is ignored and re-extracted."""
    path = write_case(tmp_path, DETERMINISTIC_TEXT)
    cache = tmp_path / "cache"
    first = load(path, cache_dir=cache)
    entry = case_study_rubric.entry_path(cache, first.digest)
    payload = json.loads(entry.read_text(encoding="utf-8"))
    payload["extraction"] = bad
    entry.write_text(json.dumps(payload), encoding="utf-8")

    again = load(path, cache_dir=cache)
    assert again.source == "extracted"
    assert again.extraction == {"method": "deterministic"}


# --- an entry is valid only while what produced it still applies --------------


def _entry_file(cache: Path, path: Path) -> Path:
    return case_study_rubric.entry_path(cache, case_study_rubric.file_digest(path))


def _edit_entry(entry: Path, **changes: Any) -> None:
    payload = json.loads(entry.read_text(encoding="utf-8"))
    payload["extraction"].update(changes)
    entry.write_text(json.dumps(payload), encoding="utf-8")


def test_llm_entry_from_an_older_prompt_is_re_extracted_and_overwritten(tmp_path: Path, llm: LlmDouble) -> None:
    path = write_case(tmp_path, UNKNOWN_LAYOUT)
    cache = tmp_path / "cache"
    load(path, cache_dir=cache)
    entry = _entry_file(cache, path)
    _edit_entry(entry, prompt_version="rubric-extraction-v0")

    again = load(path, cache_dir=cache)

    assert len(llm.calls) == 2
    assert again.source == "extracted"
    stored = json.loads(entry.read_text(encoding="utf-8"))
    assert stored["extraction"]["prompt_version"] == rubric_llm_fallback.RUBRIC_EXTRACTION_PROMPT_VERSION
    # ...and the refreshed entry is shared again.
    assert load(path, cache_dir=cache).source == "cache"
    assert len(llm.calls) == 2


def test_llm_entry_without_a_prompt_version_is_distrusted(tmp_path: Path, llm: LlmDouble) -> None:
    path = write_case(tmp_path, UNKNOWN_LAYOUT)
    cache = tmp_path / "cache"
    load(path, cache_dir=cache)
    entry = _entry_file(cache, path)
    payload = json.loads(entry.read_text(encoding="utf-8"))
    del payload["extraction"]["prompt_version"]
    entry.write_text(json.dumps(payload), encoding="utf-8")

    assert load(path, cache_dir=cache).source == "extracted"
    assert len(llm.calls) == 2


def test_llm_entry_with_the_current_prompt_is_reused(tmp_path: Path, llm: LlmDouble) -> None:
    path = write_case(tmp_path, UNKNOWN_LAYOUT)
    cache = tmp_path / "cache"
    load(path, cache_dir=cache)
    assert load(path, cache_dir=cache).source == "cache"
    assert len(llm.calls) == 1


def test_disabling_the_fallback_stops_a_cached_llm_entry_being_used(
    tmp_path: Path, llm: LlmDouble, monkeypatch
) -> None:
    path = write_case(tmp_path, UNKNOWN_LAYOUT)
    cache = tmp_path / "cache"
    load(path, cache_dir=cache)

    monkeypatch.setenv(rubric_llm_fallback.FALLBACK_ENV_VAR, "false")
    with pytest.raises(ValueError):
        load(path, cache_dir=cache)
    assert len(llm.calls) == 1

    # The refused entry is left alone: switching the fallback back on reuses it.
    monkeypatch.delenv(rubric_llm_fallback.FALLBACK_ENV_VAR)
    assert load(path, cache_dir=cache).source == "cache"
    assert len(llm.calls) == 1


def test_adjudicator_read_adopts_the_current_entry_but_not_a_stale_one(tmp_path: Path, llm: LlmDouble) -> None:
    """``allow_llm_fallback=False`` means "never extract", not "never read an LLM
    entry": the adjudicator must align with the markers' criteria."""
    path = write_case(tmp_path, UNKNOWN_LAYOUT)
    cache = tmp_path / "cache"
    load(path, cache_dir=cache)
    assert load(path, cache_dir=cache, allow_llm_fallback=False).source == "cache"

    _edit_entry(_entry_file(cache, path), prompt_version="rubric-extraction-v0")
    with pytest.raises(ValueError):
        load(path, cache_dir=cache, allow_llm_fallback=False)
    assert len(llm.calls) == 1


def test_deterministic_entry_is_reused_whatever_the_fallback_setting(
    tmp_path: Path, llm: LlmDouble, monkeypatch
) -> None:
    path = write_case(tmp_path, DETERMINISTIC_TEXT)
    cache = tmp_path / "cache"
    load(path, cache_dir=cache)
    monkeypatch.setenv(rubric_llm_fallback.FALLBACK_ENV_VAR, "false")
    assert load(path, cache_dir=cache).source == "cache"
    assert load(path, cache_dir=cache, allow_llm_fallback=False).source == "cache"
    assert llm.calls == []


def test_a_model_change_does_not_re_extract_and_provenance_keeps_the_original(
    tmp_path: Path, llm: LlmDouble
) -> None:
    """The model is provenance, not part of the key: re-extracting per model
    would let two sessions of one case study be marked on different criteria."""
    path = write_case(tmp_path, UNKNOWN_LAYOUT)
    cache = tmp_path / "cache"
    load(path, cache_dir=cache)
    llm.result = FakeExtraction(LLM_PAYLOAD, model="another-model", provider="other")

    again = load(path, cache_dir=cache)

    assert again.source == "cache"
    assert len(llm.calls) == 1
    assert again.extraction["model"] == "fake-model"


# --- concurrency -------------------------------------------------------------


class SlowExtract:
    """Replaces ``_extract``: records overlap, sleeps, optionally fails first."""

    def __init__(self, original, *, seconds: float, fail_first: bool = False) -> None:
        self.original = original
        self.seconds = seconds
        self.fail_first = fail_first
        self.calls = 0
        self.active = 0
        self.max_active = 0
        self._lock = threading.Lock()

    def __call__(self, case_study_path, **kwargs):
        with self._lock:
            self.calls += 1
            number = self.calls
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            time.sleep(self.seconds)
            if self.fail_first and number == 1:
                raise ValueError("first extraction failed")
            return self.original(case_study_path, **kwargs)
        finally:
            with self._lock:
                self.active -= 1


def run_in_thread(fn):
    box: dict[str, Any] = {}

    def target() -> None:
        try:
            box["value"] = fn()
        except BaseException as error:  # noqa: BLE001 - reported to the test
            box["error"] = error

    thread = threading.Thread(target=target)
    thread.start()
    return thread, box


def test_heartbeat_keeps_a_slow_holder_from_being_stolen(tmp_path: Path, monkeypatch) -> None:
    """The holder's work outlives LOCK_STALE_SECONDS; without the heartbeat the
    waiter would steal the lock and run a second extraction."""
    path = write_case(tmp_path, DETERMINISTIC_TEXT)
    cache = tmp_path / "cache"
    monkeypatch.setattr(case_study_rubric, "LOCK_STALE_SECONDS", 0.6)
    slow = SlowExtract(case_study_rubric._extract, seconds=1.6)
    monkeypatch.setattr(case_study_rubric, "_extract", slow)

    holder, holder_box = run_in_thread(lambda: load(path, cache_dir=cache))
    time.sleep(0.3)
    waiter, waiter_box = run_in_thread(lambda: load(path, cache_dir=cache))
    holder.join(10)
    waiter.join(10)

    assert slow.calls == 1
    assert slow.max_active == 1
    assert holder_box["value"].source == "extracted"
    # Waited well past one stale window (the old 20 s cap, scaled down) and adopted.
    assert waiter_box["value"].source == "cache"


def test_waiter_gives_up_at_the_hard_cap_and_extracts_itself(tmp_path: Path, monkeypatch) -> None:
    path = write_case(tmp_path, DETERMINISTIC_TEXT)
    cache = tmp_path / "cache"
    monkeypatch.setattr(case_study_rubric, "LLM_LOCK_WAIT_SECONDS", 0.3)
    slow = SlowExtract(case_study_rubric._extract, seconds=1.2)
    monkeypatch.setattr(case_study_rubric, "_extract", slow)

    holder, _ = run_in_thread(lambda: load(path, cache_dir=cache))
    time.sleep(0.2)
    waiter, waiter_box = run_in_thread(lambda: load(path, cache_dir=cache))
    holder.join(10)
    waiter.join(10)

    assert slow.calls == 2
    assert waiter_box["value"].source == "extracted"


def test_waiter_takes_over_when_the_holder_fails_one_at_a_time(tmp_path: Path, monkeypatch) -> None:
    path = write_case(tmp_path, DETERMINISTIC_TEXT)
    cache = tmp_path / "cache"
    slow = SlowExtract(case_study_rubric._extract, seconds=0.5, fail_first=True)
    monkeypatch.setattr(case_study_rubric, "_extract", slow)

    holder, holder_box = run_in_thread(lambda: load(path, cache_dir=cache))
    time.sleep(0.15)
    waiters = [run_in_thread(lambda: load(path, cache_dir=cache)) for _ in range(3)]
    holder.join(10)
    for thread, _ in waiters:
        thread.join(10)

    assert isinstance(holder_box["error"], ValueError)
    assert slow.max_active == 1, "waiters must not extract concurrently"
    assert slow.calls == 2, "one retry shared by every waiter"
    results = [box["value"] for _, box in waiters]
    assert sorted(result.source for result in results) == ["cache", "cache", "extracted"]


def test_llm_extraction_is_shared_across_concurrent_markers(tmp_path: Path, monkeypatch) -> None:
    path = write_case(tmp_path, UNKNOWN_LAYOUT)
    cache = tmp_path / "cache"
    calls: list[int] = []

    def slow_llm(kind, text, *, router=None):
        calls.append(1)
        time.sleep(0.5)
        return FakeExtraction(LLM_PAYLOAD)

    monkeypatch.setattr(rubric_llm_fallback, "extract_rubric_with_llm", slow_llm)
    threads = [run_in_thread(lambda: load(path, cache_dir=cache)) for _ in range(4)]
    for thread, _ in threads:
        thread.join(10)

    assert len(calls) == 1
    assert {tuple(item["label"] for item in box["value"].criteria) for _, box in threads} == {
        tuple(item["label"] for item in LLM_PAYLOAD["criteria"])
    }


# --- adjudicator and assessor ------------------------------------------------


def test_adjudicator_disables_fallback_and_carries_provenance(tmp_path: Path, llm: LlmDouble, monkeypatch) -> None:
    for name in ("content_marking", "scorer_checkpoint", "osce_panel_adjudicator"):
        sys.modules.pop(name, None)
    adjudicator = runpy.run_path(str(SCRIPTS / "osce_panel_adjudicator.py"), run_name="adj_fallback_test")
    path = write_case(tmp_path, UNKNOWN_LAYOUT)

    # No marker entry and a deterministic failure: a clear error, no LLM call.
    with pytest.raises(ValueError):
        adjudicator["load_rubric_criteria"](path, tmp_path / "cache")
    assert llm.calls == []

    # Markers' entry present: adopted, still no call from the adjudicator.
    load(path, cache_dir=tmp_path / "cache")
    assert len(llm.calls) == 1
    criteria = adjudicator["load_rubric_criteria"](path, tmp_path / "cache")
    assert [item["label"] for item in criteria] == [item["label"] for item in LLM_PAYLOAD["criteria"]]
    assert len(llm.calls) == 1

    extraction = {"method": "llm", "model": "m"}
    pick = adjudicator["marker_rubric_extraction"]
    assert pick([{}, {"rubric_extraction": extraction}, {"rubric_extraction": {"method": "x"}}]) == extraction
    assert pick([{}, {"rubric_extraction": "junk"}]) is None


def test_marker_sheet_loader_keeps_rubric_extraction(tmp_path: Path) -> None:
    for name in ("content_marking", "scorer_checkpoint", "osce_panel_adjudicator"):
        sys.modules.pop(name, None)
    adjudicator = runpy.run_path(str(SCRIPTS / "osce_panel_adjudicator.py"), run_name="adj_sheet_test")
    labels = [item["label"] for item in LLM_PAYLOAD["criteria"]]
    sheet = {
        "criteria": [
            {"label": label, "is_critical": index == 0, "value": "Yes", "timestamp": "00:00:01", "reason": "r"}
            for index, label in enumerate(labels)
        ],
        "keep_start_stop": {"keep": "k", "start": "s", "stop": "t"},
        "overall_summary": "o",
        "model": "m",
        "model_provider": "p",
        "prompt_version": "v",
        "rubric_extraction": {"method": "llm", "model": "m"},
    }
    path = tmp_path / "marker.json"
    path.write_text(json.dumps(sheet), encoding="utf-8")
    criteria = [{"label": item["label"], "is_critical": item["is_critical"]} for item in LLM_PAYLOAD["criteria"]]

    _sheet, payload = adjudicator["load_marker_sheet"](path, criteria)
    assert payload["rubric_extraction"] == {"method": "llm", "model": "m"}


def test_extra_rubric_extraction_key_does_not_break_sheet_reuse_predicate() -> None:
    from app.pipeline.marking import sheets

    payload = {
        "rubric_file": "embedded_in_case_study_pdf",
        "rubric_source": "case.pdf#llm-extracted-rubric",
        "criteria": [
            {"label": label, "value": "Yes", "is_critical": label == "A", "timestamp": "00:00:01", "reason": "r"}
            for label in ("A", "B")
        ],
        "keep_start_stop": {"keep": "k", "start": "s", "stop": "t"},
        "overall_summary": "o",
        "scoring_summary": {"total_criteria": 2, "yes_count": 2, "critical_total": 1, "pass_fail": "Pass"},
    }
    before = sheets.sheet_needs_refresh(payload)
    assert sheets.sheet_needs_refresh({**payload, "rubric_extraction": {"method": "llm"}}) == before
    assert before is False, "fixture must itself be a well-formed sheet"


def test_rubric_source_anchor_names_the_extraction_method():
    assert case_study_rubric.rubric_source_anchor("llm") == "#llm-extracted-rubric"
    assert case_study_rubric.rubric_source_anchor("deterministic") == "#rubric-section"
    assert case_study_rubric.rubric_source_anchor(None) == "#rubric-section"


def test_a_lock_taken_by_another_waiter_is_not_mistaken_for_a_timeout(
    tmp_path: Path, monkeypatch
) -> None:
    """Regression: the wait ended because the holder released the lock, and by
    the time the waiter looked again another waiter had taken it. That used to
    read as "holder outlived the cap" and run an unlocked, uncached extraction
    beside the new holder's. Only a real timeout may do that now."""
    path = write_case(tmp_path, DETERMINISTIC_TEXT)
    cache = tmp_path / "cache"
    cache.mkdir()
    lock_path = case_study_rubric.entry_path(cache, case_study_rubric.file_digest(path)).with_suffix(".lock")
    lock_path.write_text("")  # live: another waiter now holds it

    attempts = iter([False, True])
    monkeypatch.setattr(case_study_rubric, "_acquire_lock", lambda _path: next(attempts))
    monkeypatch.setattr(case_study_rubric, "_await_entry", lambda *a, **k: (None, False))

    rubric = load(path, cache_dir=cache)

    assert rubric.source == "extracted"
    # Extracted under the lock (second round), so the entry was published.
    assert case_study_rubric.entry_path(cache, rubric.digest).exists()
