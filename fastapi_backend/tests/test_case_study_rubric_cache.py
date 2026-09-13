"""The case-study rubric is extracted once per distinct PDF, not once per marker.

A panel of N markers plus the adjudicator used to run N+1 identical pypdf
extractions of the same file, and every clip child of a long recording repeated
them again. These pin the properties that fix relies on: the cache is keyed by
the file's bytes (so it is never stale), concurrent markers share one
extraction, and every failure mode degrades to extracting in-process rather than
to a failed run.
"""
from __future__ import annotations

import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest


SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import case_study_rubric  # noqa: E402  (scripts/ has to be on sys.path first)

load_case_study_rubric = case_study_rubric.load_case_study_rubric


CASE_STUDY_TEXT = (
    "[Page 1]\nPatient presents with an itchy rash on the foot.\n\n"
    "[Page 4]\nAnalytical Checklist\nGATHERING INFORMATION/ INTRODUCTION YES NO\n"
    "1. Elicits onset and duration.\n2. Asks about other symptoms.\n"
    "3. Checks medication history.\n"
)


@pytest.fixture()
def case_study(tmp_path: Path) -> Path:
    # A .txt goes through the same read_file_as_context_text dispatch a .pdf
    # does, minus the pypdf dependency — the caching is indifferent to format.
    path = tmp_path / "case-study.txt"
    path.write_text(CASE_STUDY_TEXT, encoding="utf-8")
    return path


@pytest.fixture()
def counted_extraction(monkeypatch):
    """Counts how many times the PDF text is actually read and parsed."""
    calls: list[Path] = []
    original = case_study_rubric.read_file_as_context_text

    def counting(path: Path) -> str:
        calls.append(Path(path))
        return original(path)

    monkeypatch.setattr(case_study_rubric, "read_file_as_context_text", counting)
    return calls


def test_extracts_the_rubric_and_the_clinical_context(case_study: Path) -> None:
    rubric = load_case_study_rubric(case_study)

    assert rubric.source == "extracted"
    assert len(rubric.criteria) == 3
    assert rubric.rubric_section_text.startswith("Analytical Checklist")
    assert "itchy rash" in rubric.context_text


def test_second_load_is_served_from_cache(case_study: Path, tmp_path: Path, counted_extraction) -> None:
    cache_dir = tmp_path / "cache"

    first = load_case_study_rubric(case_study, cache_dir=cache_dir)
    second = load_case_study_rubric(case_study, cache_dir=cache_dir)

    assert first.source == "extracted"
    assert second.source == "cache"
    assert len(counted_extraction) == 1
    assert second.criteria == first.criteria
    assert second.rubric_section_text == first.rubric_section_text
    assert second.context_text == first.context_text


def test_without_a_cache_directory_every_load_extracts(case_study: Path, counted_extraction) -> None:
    """Running a scorer by hand needs no cache setup and behaves as it always did."""
    assert load_case_study_rubric(case_study).source == "extracted"
    assert load_case_study_rubric(case_study).source == "extracted"
    assert len(counted_extraction) == 2


def test_changed_bytes_are_a_different_entry(case_study: Path, tmp_path: Path, counted_extraction) -> None:
    """The key *is* the content, so a re-uploaded case study cannot be served a
    previous version's rubric — there is nothing to invalidate."""
    cache_dir = tmp_path / "cache"
    load_case_study_rubric(case_study, cache_dir=cache_dir)

    case_study.write_text(CASE_STUDY_TEXT + "4. Asks about allergies.\n", encoding="utf-8")
    reloaded = load_case_study_rubric(case_study, cache_dir=cache_dir)

    assert reloaded.source == "extracted"
    assert len(reloaded.criteria) == 4
    assert len(counted_extraction) == 2


def test_two_files_with_identical_bytes_share_one_entry(
    case_study: Path, tmp_path: Path, counted_extraction
) -> None:
    """A clip child copies its parent's case study; the copy is the same rubric."""
    cache_dir = tmp_path / "cache"
    copy = tmp_path / "child" / "case-study.txt"
    copy.parent.mkdir()
    copy.write_text(CASE_STUDY_TEXT, encoding="utf-8")

    load_case_study_rubric(case_study, cache_dir=cache_dir)
    adopted = load_case_study_rubric(copy, cache_dir=cache_dir)

    assert adopted.source == "cache"
    assert len(counted_extraction) == 1


def test_a_corrupt_entry_is_ignored_and_replaced(case_study: Path, tmp_path: Path) -> None:
    cache_dir = tmp_path / "cache"
    first = load_case_study_rubric(case_study, cache_dir=cache_dir)
    entry = case_study_rubric.entry_path(cache_dir, first.digest)
    entry.write_text("{ not json", encoding="utf-8")

    recovered = load_case_study_rubric(case_study, cache_dir=cache_dir)

    assert recovered.source == "extracted"
    assert recovered.criteria == first.criteria
    assert json.loads(entry.read_text(encoding="utf-8"))["criteriaCount"] == len(first.criteria)


def test_an_entry_from_an_older_extractor_is_not_reused(case_study: Path, tmp_path: Path) -> None:
    """The bytes are one input to the extraction; this package's parsing is the
    other. Bumping EXTRACTOR_VERSION has to strand the old entries."""
    cache_dir = tmp_path / "cache"
    first = load_case_study_rubric(case_study, cache_dir=cache_dir)
    entry = case_study_rubric.entry_path(cache_dir, first.digest)
    payload = json.loads(entry.read_text(encoding="utf-8"))
    payload["extractorVersion"] = "case-study-rubric-v0"
    entry.write_text(json.dumps(payload), encoding="utf-8")

    assert load_case_study_rubric(case_study, cache_dir=cache_dir).source == "extracted"


def test_concurrent_markers_extract_once_between_them(
    case_study: Path, tmp_path: Path, counted_extraction
) -> None:
    """The cold-cache case is the one that matters: a panel's markers all start
    at the same moment, so without single-flighting they would all miss."""
    cache_dir = tmp_path / "cache"

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(lambda _: load_case_study_rubric(case_study, cache_dir=cache_dir), range(6)))

    assert len(counted_extraction) == 1
    assert [len(result.criteria) for result in results] == [3] * 6
    assert sum(1 for result in results if result.source == "extracted") == 1


def test_a_stale_lock_does_not_block_a_run(case_study: Path, tmp_path: Path, monkeypatch) -> None:
    """A process killed while holding the lock must not make every later run
    wait it out — the lock is stolen once it is older than the stale window."""
    cache_dir = tmp_path / "cache"
    digest = case_study_rubric.file_digest(case_study)
    lock_path = case_study_rubric.entry_path(cache_dir, digest).with_suffix(".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text("", encoding="utf-8")

    monkeypatch.setattr(case_study_rubric, "LOCK_STALE_SECONDS", 0.0)
    rubric = load_case_study_rubric(case_study, cache_dir=cache_dir)

    assert rubric.source == "extracted"
    assert case_study_rubric.entry_path(cache_dir, digest).exists()
    assert not lock_path.exists()


def test_an_unwritable_cache_still_returns_the_rubric(case_study: Path, tmp_path: Path, monkeypatch) -> None:
    """A cache can make a run faster; it must never be why one fails."""
    cache_dir = tmp_path / "cache"

    def refuse(*_args, **_kwargs):
        raise OSError("read-only volume")

    monkeypatch.setattr(case_study_rubric.tempfile, "NamedTemporaryFile", refuse)
    rubric = load_case_study_rubric(case_study, cache_dir=cache_dir)

    assert rubric.source == "extracted"
    assert len(rubric.criteria) == 3


def test_a_case_study_with_no_rubric_section_reports_why(tmp_path: Path) -> None:
    path = tmp_path / "not-a-case-study.txt"
    path.write_text("A letter to the department with no checklist in it at all." * 10, encoding="utf-8")

    with pytest.raises(ValueError) as error:
        load_case_study_rubric(path, cache_dir=tmp_path / "cache")

    assert "rubric" in str(error.value).lower()
