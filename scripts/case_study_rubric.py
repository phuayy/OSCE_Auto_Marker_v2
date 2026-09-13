"""Extract a case-study PDF's embedded rubric once, then reuse it.

Every content marker and the panel adjudicator need the same three things from
the same PDF: the clinical context text, the rubric section, and the criteria
parsed out of it. Each one used to derive them itself, so a panel of N markers
paid N+1 pypdf extractions of the same file — and a long recording, where every
exported clip becomes a child session scored against the *same* case study,
multiplied that again by the number of clips.

The derivation is a pure function of the PDF's bytes, so it is cached by a
digest of those bytes. A cache entry is therefore never stale: different bytes
mean a different key, and there is nothing to invalidate. ``EXTRACTOR_VERSION``
covers the other input — this package's own parsing logic — and must be bumped
whenever ``rubric_section`` or ``extract_rubric_criteria_from_case_study_rubric``
changes what they return for unchanged bytes.

Concurrency matters here because the markers of a panel start at the same
moment and would otherwise all miss a cold cache together. One process takes an
exclusive lock and extracts; the others wait for its result and adopt it. Every
failure mode falls back to extracting in-process, so the cache can make a run
faster but never makes one fail: no cache directory, an unwritable one, a
corrupt entry and a lock holder that dies all end in the same plain extraction
the scripts did before this module existed.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from content_marking import (
    extract_rubric_criteria_from_case_study_rubric,
    read_file_as_context_text,
)
from rubric_section import diagnose_missing_rubric_section, split_case_study_context_and_rubric


# Bump when the extraction above starts producing different output for the same
# bytes. Entries written by an older version are simply never looked up again.
EXTRACTOR_VERSION = "case-study-rubric-v1"

CACHE_SCHEMA = "case-study-rubric-cache-v1"

# A rubric with fewer criteria than this is a failed extraction, not a short
# rubric: the marker cannot produce a usable sheet from it.
MIN_RUBRIC_CRITERIA = 2

# How long a process that lost the race waits for the winner's entry before
# giving up and extracting the PDF itself. Extraction of a normal case study is
# well under a second; this is generous so that a very large PDF on a loaded
# machine is still shared rather than duplicated.
LOCK_WAIT_SECONDS = 20.0

# A lock older than this belonged to a process that died holding it. Stealing it
# is safe because the worst case is two processes extracting at once, which is
# exactly the behaviour this module replaces.
LOCK_STALE_SECONDS = 60.0

_LOCK_POLL_SECONDS = 0.05

_READ_CHUNK_BYTES = 1024 * 1024


@dataclass(frozen=True)
class CaseStudyRubric:
    """The rubric a marker needs, and where it came from.

    ``source`` is for logging only — ``"extracted"`` when this process parsed
    the PDF, ``"cache"`` when it adopted another run's parse.
    """

    context_text: str
    rubric_section_text: str
    criteria: list[dict[str, Any]]
    digest: str
    source: str


def load_case_study_rubric(
    case_study_path: Path,
    *,
    cache_dir: Path | None = None,
    min_criteria: int = MIN_RUBRIC_CRITERIA,
) -> CaseStudyRubric:
    """The parsed rubric for ``case_study_path``, from cache when possible.

    ``cache_dir`` is handed over by the caller (the API passes
    ``--rubric-cache``); with none, this is exactly the inline extraction the
    scorers did before, so running a scorer by hand needs no extra setup.
    """
    case_study_path = Path(case_study_path)
    digest = file_digest(case_study_path)

    if cache_dir is None:
        return _extract(case_study_path, digest=digest, min_criteria=min_criteria)

    cache_path = entry_path(cache_dir, digest)
    cached = _read_entry(cache_path, digest=digest, min_criteria=min_criteria)
    if cached is not None:
        return cached

    lock_path = cache_path.with_suffix(".lock")
    if not _acquire_lock(lock_path):
        # Another process is extracting this exact PDF. Its result is ours too.
        waited = _await_entry(cache_path, lock_path, digest=digest, min_criteria=min_criteria)
        if waited is not None:
            return waited
        return _extract(case_study_path, digest=digest, min_criteria=min_criteria)

    try:
        rubric = _extract(case_study_path, digest=digest, min_criteria=min_criteria)
        _write_entry(cache_path, rubric)
        return rubric
    finally:
        with contextlib.suppress(OSError):
            lock_path.unlink()


def file_digest(path: Path) -> str:
    """SHA-256 of the file's bytes, streamed so a large PDF is not held in memory."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(_READ_CHUNK_BYTES), b""):
            digest.update(chunk)
    return digest.hexdigest()


def entry_path(cache_dir: Path, digest: str) -> Path:
    return Path(cache_dir) / f"{EXTRACTOR_VERSION}-{digest}.json"


# --- extraction --------------------------------------------------------------


def _extract(case_study_path: Path, *, digest: str, min_criteria: int) -> CaseStudyRubric:
    full_text = read_file_as_context_text(case_study_path)
    context_text, rubric_section_text = split_case_study_context_and_rubric(full_text)
    if not rubric_section_text:
        raise ValueError(diagnose_missing_rubric_section(full_text))

    criteria = extract_rubric_criteria_from_case_study_rubric(rubric_section_text)
    if len(criteria) < min_criteria:
        raise ValueError(
            "Could not extract enough rubric criteria from case-study PDF rubric section. "
            f"Found {len(criteria)} criteria."
        )

    return CaseStudyRubric(
        # The caller clips these to its own prompt budget. The cache stores them
        # whole so it does not depend on any one prompt's limits.
        context_text=context_text or full_text,
        rubric_section_text=rubric_section_text,
        criteria=criteria,
        digest=digest,
        source="extracted",
    )


# --- cache entries -----------------------------------------------------------


def _read_entry(cache_path: Path, *, digest: str, min_criteria: int) -> CaseStudyRubric | None:
    """A usable entry, or ``None``. Anything unreadable or malformed is ignored
    rather than raised: the caller can always extract instead, and a cache must
    not be a way for a run to fail."""
    try:
        payload = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None

    if not isinstance(payload, dict):
        return None
    if payload.get("schema") != CACHE_SCHEMA or payload.get("extractorVersion") != EXTRACTOR_VERSION:
        return None
    if payload.get("digest") != digest:
        return None

    criteria = payload.get("criteria")
    rubric_section_text = payload.get("rubricSectionText")
    context_text = payload.get("contextText")
    if not isinstance(criteria, list) or len(criteria) < min_criteria:
        return None
    if not isinstance(rubric_section_text, str) or not rubric_section_text:
        return None
    if not isinstance(context_text, str):
        return None
    if not all(isinstance(item, dict) for item in criteria):
        return None

    return CaseStudyRubric(
        context_text=context_text,
        rubric_section_text=rubric_section_text,
        criteria=criteria,
        digest=digest,
        source="cache",
    )


def _write_entry(cache_path: Path, rubric: CaseStudyRubric) -> None:
    """Publish an entry atomically, or leave the cache untouched.

    Temp file plus ``os.replace`` in the same directory, so a reader never sees
    a half-written entry and a crash mid-write leaves nothing behind but a temp
    file. A cache that cannot be written is not an error — the value is already
    in hand.
    """
    payload = {
        "schema": CACHE_SCHEMA,
        "extractorVersion": EXTRACTOR_VERSION,
        "digest": rubric.digest,
        "criteriaCount": len(rubric.criteria),
        "criteria": rubric.criteria,
        "rubricSectionText": rubric.rubric_section_text,
        "contextText": rubric.context_text,
    }
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        handle = tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=cache_path.parent,
            prefix=f".{cache_path.stem}.",
            suffix=".tmp",
            delete=False,
        )
        temp_path = Path(handle.name)
        try:
            with handle:
                json.dump(payload, handle, ensure_ascii=True)
            os.replace(temp_path, cache_path)
        except BaseException:
            with contextlib.suppress(OSError):
                temp_path.unlink()
            raise
    except OSError:
        return


# --- single-flight lock ------------------------------------------------------


def _acquire_lock(lock_path: Path) -> bool:
    """True when this process should do the extraction.

    ``O_CREAT | O_EXCL`` is the portable "create it only if it does not exist"
    primitive — it works the same on Windows and POSIX and needs no third-party
    file-locking package. A lock that cannot be created for any reason other
    than "already held" is reported as not acquired, which degrades to an
    uncached extraction rather than to a failure.
    """
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return _steal_if_stale(lock_path)
    except OSError:
        return False
    os.close(descriptor)
    return True


def _steal_if_stale(lock_path: Path) -> bool:
    """Take over a lock whose holder died. False while the holder is alive."""
    try:
        age = time.time() - lock_path.stat().st_mtime
    except OSError:
        # It disappeared between the failed create and now: the holder finished.
        return False
    if age < LOCK_STALE_SECONDS:
        return False
    with contextlib.suppress(OSError):
        lock_path.unlink()
    try:
        descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except OSError:
        return False
    os.close(descriptor)
    return True


def _await_entry(
    cache_path: Path, lock_path: Path, *, digest: str, min_criteria: int
) -> CaseStudyRubric | None:
    """Poll for the lock holder's entry. ``None`` when it never arrives.

    The wait ends early when the lock is released without an entry appearing:
    that is the holder's extraction having failed, and every waiter is about to
    fail the same way. Waiting out the full timeout first would add it to a run
    that is already doomed.
    """
    deadline = time.monotonic() + LOCK_WAIT_SECONDS
    while time.monotonic() < deadline:
        entry = _read_entry(cache_path, digest=digest, min_criteria=min_criteria)
        if entry is not None:
            return entry
        if not lock_path.exists():
            return _read_entry(cache_path, digest=digest, min_criteria=min_criteria)
        time.sleep(_LOCK_POLL_SECONDS)
    return _read_entry(cache_path, digest=digest, min_criteria=min_criteria)


__all__ = [
    "CACHE_SCHEMA",
    "EXTRACTOR_VERSION",
    "MIN_RUBRIC_CRITERIA",
    "CaseStudyRubric",
    "entry_path",
    "file_digest",
    "load_case_study_rubric",
]
