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
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from content_marking import (
    extract_rubric_criteria_from_case_study_rubric,
    read_file_as_context_text,
)
import rubric_llm_fallback
from rubric_section import (
    diagnose_missing_rubric_section,
    has_extractable_text,
    locate_phrase,
    split_case_study_context_and_rubric,
)


# Bump when the extraction above starts producing different output for the same
# bytes. Entries written by an older version are simply never looked up again.
EXTRACTOR_VERSION = "case-study-rubric-v1"

CACHE_SCHEMA = "case-study-rubric-cache-v1"

# A rubric with fewer criteria than this is a failed extraction, not a short
# rubric: the marker cannot produce a usable sheet from it.
MIN_RUBRIC_CRITERIA = 2

# A lock older than this belonged to a process that died holding it. Stealing it
# is safe because the worst case is two processes extracting at once, which is
# exactly the behaviour this module replaces.
LOCK_STALE_SECONDS = 60.0

# How long a process that lost the race waits for the holder's entry. A
# deterministic extraction is well under a second, but an LLM extraction can take
# minutes, not milliseconds. While the lock holder is
# alive its lock is kept fresh (see ``_LockHeartbeat``), so waiters keep waiting
# for it up to this hard cap. Sharing matters more
# than speed here: markers routed to different models would otherwise each run
# their own extraction, get different criteria lists and fail the panel's
# alignment check.
LLM_LOCK_WAIT_SECONDS = 600.0

# How often a live holder touches its lock. Must stay well under
# LOCK_STALE_SECONDS or a healthy holder looks dead.
LOCK_HEARTBEAT_SECONDS = 5.0

# When a holder releases the lock without publishing an entry (its extraction
# failed), a waiter goes back and tries to take the lock itself, so extractions
# still run one at a time. This bounds that loop.
MAX_LOCK_ROUNDS = 8

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
    # Provenance: ``{"method": "deterministic"}`` or the LLM block from
    # ``LlmRubricExtraction.provenance``. Cached with the entry.
    extraction: dict[str, Any] = field(default_factory=rubric_llm_fallback.deterministic_provenance)

    @property
    def method(self) -> str:
        return str((self.extraction or {}).get("method") or "deterministic")


def load_case_study_rubric(
    case_study_path: Path,
    *,
    cache_dir: Path | None = None,
    min_criteria: int = MIN_RUBRIC_CRITERIA,
    allow_llm_fallback: bool = True,
) -> CaseStudyRubric:
    """The parsed rubric for ``case_study_path``, from cache when possible.

    ``cache_dir`` is handed over by the caller (the API passes
    ``--rubric-cache``); with none, this is exactly the inline extraction the
    scorers did before, so running a scorer by hand needs no extra setup.

    ``allow_llm_fallback=False`` is for a reader that must adopt the rubric the
    markers used (the panel adjudicator): it takes their cache entry or the
    deterministic parse, and never runs an extraction of its own.
    """
    case_study_path = Path(case_study_path)
    digest = file_digest(case_study_path)

    def extract() -> CaseStudyRubric:
        return _extract(
            case_study_path,
            digest=digest,
            min_criteria=min_criteria,
            allow_llm_fallback=allow_llm_fallback,
        )

    if cache_dir is None:
        return extract()

    cache_path = entry_path(cache_dir, digest)
    cached = _read_entry(cache_path, digest=digest, min_criteria=min_criteria)
    if cached is not None:
        return cached

    lock_path = cache_path.with_suffix(".lock")
    for _round in range(MAX_LOCK_ROUNDS):
        if _acquire_lock(lock_path):
            with _LockHeartbeat(lock_path):
                try:
                    # Look again now that the lock is ours. The miss above was
                    # read before the lock was tried, and a holder can run its
                    # whole cycle - extract, publish, release - inside that gap;
                    # the lock being free then means "finished", not "nobody
                    # started", and its entry is already on disk.
                    cached = _read_entry(cache_path, digest=digest, min_criteria=min_criteria)
                    if cached is not None:
                        return cached
                    rubric = extract()
                    _write_entry(cache_path, rubric)
                    return rubric
                finally:
                    with contextlib.suppress(OSError):
                        lock_path.unlink()

        # Another process is extracting this exact PDF. Its result is ours too.
        waited, timed_out = _await_entry(cache_path, lock_path, digest=digest, min_criteria=min_criteria)
        if waited is not None:
            return waited
        if timed_out:
            # Still held and fresh at the hard wait cap: stop waiting. Decided
            # by the wait itself, not by re-checking the lock: another waiter
            # may have taken it in the meantime, and reading that as "the
            # holder outlived the cap" would put two extractions in flight.
            break
        # Released with no entry (the holder's extraction failed) or stale: go
        # round again and try to take the lock, so extractions stay one at a
        # time and a later success is shared.

    # Cache directory unusable, or the holder outlived the wait cap: extract
    # here, uncached by lock, as before the cache existed.
    return extract()


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


def _extract(
    case_study_path: Path, *, digest: str, min_criteria: int, allow_llm_fallback: bool = True
) -> CaseStudyRubric:
    full_text = read_file_as_context_text(case_study_path)
    context_text, rubric_section_text = split_case_study_context_and_rubric(full_text)
    if not rubric_section_text:
        deterministic_error = diagnose_missing_rubric_section(full_text)
    else:
        criteria = extract_rubric_criteria_from_case_study_rubric(rubric_section_text)
        if len(criteria) >= min_criteria:
            return CaseStudyRubric(
                # The caller clips these to its own prompt budget. The cache stores
                # them whole so it does not depend on any one prompt's limits.
                context_text=context_text or full_text,
                rubric_section_text=rubric_section_text,
                criteria=criteria,
                digest=digest,
                source="extracted",
                extraction=rubric_llm_fallback.deterministic_provenance(),
            )
        deterministic_error = (
            "Could not extract enough rubric criteria from case-study PDF rubric section. "
            f"Found {len(criteria)} criteria."
        )

    if not allow_llm_fallback or not has_extractable_text(full_text):
        # A scanned PDF has nothing for a model to read either.
        raise ValueError(deterministic_error)
    return _extract_with_llm(
        full_text,
        digest=digest,
        min_criteria=min_criteria,
        deterministic_error=deterministic_error,
    )


def _extract_with_llm(
    full_text: str, *, digest: str, min_criteria: int, deterministic_error: str
) -> CaseStudyRubric:
    try:
        llm = rubric_llm_fallback.extract_rubric_with_llm("content", full_text)
    except rubric_llm_fallback.RubricLlmFallbackUnavailable:
        # Off, or no model: behave exactly as if the fallback did not exist.
        raise ValueError(deterministic_error) from None
    except rubric_llm_fallback.RubricLlmFallbackFailed as error:
        raise ValueError(
            f"{deterministic_error} LLM fallback extraction also failed: {error}"
        ) from error

    criteria = [
        {"label": str(item["label"]), "is_critical": bool(item.get("is_critical", False))}
        for item in llm.payload.get("criteria", [])
    ]
    if len(criteria) < min_criteria:
        raise ValueError(
            f"{deterministic_error} LLM fallback extraction also failed: "
            f"it returned {len(criteria)} criteria."
        )
    return CaseStudyRubric(
        context_text=_context_before_quote(full_text, llm.payload.get("rubric_start_quote")),
        rubric_section_text=render_criteria_section(criteria),
        criteria=criteria,
        digest=digest,
        source="extracted",
        extraction=llm.provenance(deterministic_error=deterministic_error),
    )


def rubric_source_anchor(method: str | None) -> str:
    """The fragment a score sheet's ``rubric_source`` points at: the PDF's own
    rubric section, or the criteria the LLM fallback extracted from it."""
    return "#llm-extracted-rubric" if method == "llm" else "#rubric-section"


def render_criteria_section(criteria: list[dict[str, Any]]) -> str:
    """The marker prompt's rubric text for LLM-extracted criteria: a numbered
    list that is, by construction, consistent with the criteria themselves."""
    return "\n".join(
        f"{index}. {item['label']}" + (" [CRITICAL CRITERIA]" if item.get("is_critical") else "")
        for index, item in enumerate(criteria, start=1)
    )


def _context_before_quote(full_text: str, quote: Any) -> str:
    """Text before where the rubric starts; the whole text when the quote is
    absent or cannot be located (case and whitespace insensitive)."""
    start = locate_phrase(full_text, str(quote or ""))
    if start is None:
        return full_text
    return full_text[:start].strip() or full_text


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

    # Entries written before provenance existed are deterministic parses (the
    # LLM fallback did not exist), so they stay valid under the same
    # EXTRACTOR_VERSION. A present-but-malformed field is distrusted rather than
    # defaulted: it would otherwise let an unlabelled LLM guess pass as a
    # deterministic parse, so the entry is unusable and gets re-extracted.
    if "extraction" in payload:
        extraction = payload["extraction"]
        if not isinstance(extraction, dict) or not isinstance(extraction.get("method"), str):
            return None
    else:
        extraction = rubric_llm_fallback.deterministic_provenance()

    return CaseStudyRubric(
        context_text=context_text,
        rubric_section_text=rubric_section_text,
        criteria=criteria,
        digest=digest,
        source="cache",
        extraction=extraction,
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
        "extraction": rubric.extraction,
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


def _lock_age(lock_path: Path) -> float | None:
    try:
        return time.time() - lock_path.stat().st_mtime
    except OSError:
        return None


def _lock_is_live(lock_path: Path) -> bool:
    """The lock exists and its holder has touched it recently."""
    age = _lock_age(lock_path)
    return age is not None and age < LOCK_STALE_SECONDS


class _LockHeartbeat:
    """Keeps a held lock's mtime fresh from a daemon thread.

    Without it a holder doing a minutes-long LLM extraction would look dead
    after LOCK_STALE_SECONDS and get its lock stolen, putting two (differently
    routed) extractions in flight.
    """

    def __init__(self, lock_path: Path) -> None:
        self._lock_path = lock_path
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="rubric-lock-heartbeat", daemon=True)

    def _run(self) -> None:
        interval = max(0.01, min(LOCK_HEARTBEAT_SECONDS, LOCK_STALE_SECONDS / 3))
        while not self._stop.wait(interval):
            with contextlib.suppress(OSError):
                os.utime(self._lock_path)

    def __enter__(self) -> "_LockHeartbeat":
        self._thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self._stop.set()
        self._thread.join(timeout=2.0)


def _await_entry(
    cache_path: Path, lock_path: Path, *, digest: str, min_criteria: int
) -> tuple[CaseStudyRubric | None, bool]:
    """Poll for the lock holder's entry: ``(entry, timed_out)``.

    Waits as long as the lock exists and is fresh (its holder is alive), up to
    LLM_LOCK_WAIT_SECONDS. Returns early when the lock is released without an
    entry (the holder's extraction failed) or goes stale (the holder died); the
    caller then retries the lock itself rather than every waiter extracting at
    once. ``timed_out`` is true only when the hard cap ran out with the lock
    still live.
    """
    deadline = time.monotonic() + LLM_LOCK_WAIT_SECONDS
    while time.monotonic() < deadline:
        entry = _read_entry(cache_path, digest=digest, min_criteria=min_criteria)
        if entry is not None:
            return entry, False
        if not _lock_is_live(lock_path):
            return _read_entry(cache_path, digest=digest, min_criteria=min_criteria), False
        time.sleep(_LOCK_POLL_SECONDS)
    return _read_entry(cache_path, digest=digest, min_criteria=min_criteria), True


__all__ = [
    "CACHE_SCHEMA",
    "EXTRACTOR_VERSION",
    "MIN_RUBRIC_CRITERIA",
    "CaseStudyRubric",
    "render_criteria_section",
    "rubric_source_anchor",
    "entry_path",
    "file_digest",
    "load_case_study_rubric",
]
