"""Deterministic corpus-term correction for normalized transcripts.

Two independent matching channels run over the same sliding window, both
stdlib-only and both fully logged:

* **Orthographic** — ``difflib`` similarity against the written term. Catches
  the near-miss spelling ("nasal blog" -> "nasal block").
* **Phonetic** — Double Metaphone ([app/pipeline/phonetics.py]) over the window
  with its word boundaries removed. Catches what an ASR model actually does to
  clinical vocabulary: it does not misspell, it decodes the audio into ordinary
  words that sound right and score well under a language model. "Paracetamol"
  comes back as "para set a mole" — edit distance 4 from the truth, three extra
  word boundaries, and phonetically identical.

The phonetic channel is therefore span-flexible: a single-word term is matched
against windows of one to several words, so a term the transcriber fragmented
is recovered whole. Every substitution is recorded with the channel that made
it and both scores, so a corrected transcript is explainable rather than
silently rewritten.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from app.pipeline.phonetics import phonetic_codes

_EDGE_PUNCTUATION = ".,;:!?…\"'()[]{}"
_NON_ALPHA = re.compile(r"[^a-z]+")

# Terms shorter than this are too ambiguous to fuzzy-match safely.
_MIN_TERM_LENGTH = 4


@dataclass(frozen=True)
class CorrectionPolicy:
    """Thresholds for both matching channels.

    Defaults are the tuned production values; the pipeline builds one of these
    from Settings so an operator can move them per deployment.
    """

    # -- orthographic channel --
    min_ratio: float = 0.84
    # Long medical words relax slightly: "black" vs "block" is 0.80 and must
    # stay rejected, while "parasitamol" vs "paracetamol" is 0.818 and must be
    # caught.
    relaxed_ratio_delta: float = 0.04
    relaxed_length: int = 8

    # -- phonetic channel --
    phonetic_enabled: bool = True
    # Similarity between phonetic keys. Exact key equality is accepted outright;
    # this covers the near miss ("sal butter mole" -> SLPTRML vs SLPTML).
    min_phonetic_ratio: float = 0.90
    # A phonetic hit still has to look vaguely like the term in writing —
    # without this, any two words sharing a consonant skeleton would swap.
    min_phonetic_char_ratio: float = 0.5
    # Keys shorter than this collide far too often to be evidence ("black" and
    # "block" are both PLK).
    min_phonetic_key_codes: int = 4
    # How many extra words beyond the term's own word count one window may span.
    # ASR fragments a word into a handful of tokens, not a sentence.
    max_extra_span_words: int = 3


@dataclass(frozen=True)
class _Term:
    """A corpus term with everything the matcher needs precomputed."""

    text: str
    casefold: str
    words: tuple[str, ...]
    letters: str
    keys: tuple[str, str]

    @property
    def phonetic_eligible_length(self) -> int:
        return min(len(key) for key in self.keys if key) if any(self.keys) else 0


@dataclass(frozen=True)
class _Match:
    """One accepted window/term pairing, ranked before it is applied."""

    span: int
    ratio: float
    phonetic_ratio: float
    method: str

    @property
    def rank(self) -> tuple[float, float, float, int]:
        # Exact phonetic agreement outranks everything; then the blend of both
        # scores; then the *shorter* span. Preferring the shorter span is what
        # keeps "a nasal blog" from being swallowed whole when "nasal blog"
        # alone is the real misrecognition.
        return (
            1.0 if self.phonetic_ratio >= 1.0 else 0.0,
            self.ratio + self.phonetic_ratio,
            self.ratio,
            -self.span,
        )


def _core(token: str) -> tuple[str, str, str]:
    """Split a token into (leading punctuation, core, trailing punctuation)."""
    start = 0
    end = len(token)
    while start < end and token[start] in _EDGE_PUNCTUATION:
        start += 1
    while end > start and token[end - 1] in _EDGE_PUNCTUATION:
        end -= 1
    return token[:start], token[start:end], token[end:]


def _letters(text: str) -> str:
    return _NON_ALPHA.sub("", str(text or "").casefold())


def _ratio(left: str, right: str) -> float:
    return SequenceMatcher(None, left, right).ratio()


def _prepare_terms(terms: list[Any]) -> list[_Term]:
    """Clean, de-duplicate and order the corpus.

    Longer terms are matched first so a short term can never mangle a longer
    term's replacement; the span-locking in :func:`correct_segments` then keeps
    the result stable.
    """
    prepared: dict[str, _Term] = {}
    for raw in terms:
        text = str(raw or "").strip()
        if len(text) < _MIN_TERM_LENGTH:
            continue
        casefold = text.casefold()
        if casefold in prepared:
            continue
        prepared[casefold] = _Term(
            text=text,
            casefold=casefold,
            words=tuple(text.split()),
            letters=_letters(text),
            keys=phonetic_codes(text),
        )
    return sorted(prepared.values(), key=lambda term: (len(term.words), len(term.text)), reverse=True)


def _orthographic_ratio(window_text: str, window_letters: str, term: _Term) -> float:
    """Best of the written and de-spaced comparisons.

    The de-spaced form is what matters once a window spans more words than the
    term has: "para set a mole" reads as 0.55 against "paracetamol" with its
    spaces in, and 0.87 without them.
    """
    return max(_ratio(window_text.casefold(), term.casefold), _ratio(window_letters, term.letters))


def _phonetic_ratio(window_letters: str, term: _Term) -> float:
    """Best agreement across the primary/alternate readings of both sides."""
    window_keys = [key for key in phonetic_codes(window_letters) if key]
    term_keys = [key for key in term.keys if key]
    if not window_keys or not term_keys:
        return 0.0
    return max(_ratio(window_key, term_key) for window_key in window_keys for term_key in term_keys)


def _length_compatible(window_letters: str, term: _Term) -> bool:
    """Reject windows too far from the term in length to be a misrecognition."""
    tolerance = max(2, int(len(term.letters) * 0.4))
    return abs(len(window_letters) - len(term.letters)) <= tolerance


def _evaluate(
    window_text: str,
    window_letters: str,
    span: int,
    term: _Term,
    policy: CorrectionPolicy,
) -> _Match | None:
    """Score one window against one term; None if neither channel accepts."""
    if not window_letters or not _length_compatible(window_letters, term):
        return None

    ratio = _orthographic_ratio(window_text, window_letters, term)

    # The orthographic channel only speaks for windows of the term's own word
    # count — that is the behaviour it was tuned for, and widening it would
    # trade precision for reach the phonetic channel already covers.
    if span == len(term.words):
        threshold = policy.min_ratio
        # The relaxation exists for long single words ("parasitamol" ->
        # "paracetamol", 0.818). It must not extend to multi-word terms, whose
        # windows are phrases of common speech: at 0.80, "sure that" scores as
        # "sore throat" and "make sure that everything" is destroyed.
        if len(term.words) == 1 and len(term.casefold) >= policy.relaxed_length:
            threshold = max(0.0, threshold - policy.relaxed_ratio_delta)
        if ratio >= threshold:
            return _Match(span=span, ratio=ratio, phonetic_ratio=_phonetic_ratio(window_letters, term), method="orthographic")

    if not policy.phonetic_enabled or term.phonetic_eligible_length < policy.min_phonetic_key_codes:
        return None

    phonetic_ratio = _phonetic_ratio(window_letters, term)
    if phonetic_ratio >= policy.min_phonetic_ratio and ratio >= policy.min_phonetic_char_ratio:
        return _Match(span=span, ratio=ratio, phonetic_ratio=phonetic_ratio, method="phonetic")
    return None


def _contains_protected(cores: list[str], protected: set[str], max_words: int) -> bool:
    """True if the window already spells some corpus term verbatim.

    A window carrying a correctly transcribed term plus a neighbouring word
    ("nasal block or") is not a misrecognition of that term — it is the term,
    and rewriting it would delete the neighbour. The same check stops a correct
    "prednisolone" from being pulled towards its sibling "prednisone".
    """
    lowered = [core.casefold() for core in cores]
    for start in range(len(lowered)):
        for span in range(1, min(max_words, len(lowered) - start) + 1):
            if " ".join(lowered[start : start + span]) in protected:
                return True
    return False


def _capitalize_like(replacement: str, window_text: str) -> str:
    if window_text[:1].isupper():
        return replacement[0].upper() + replacement[1:]
    return replacement


def correct_segments(
    segments: list[dict[str, Any]],
    terms: list[Any],
    policy: CorrectionPolicy | None = None,
) -> list[dict[str, Any]]:
    """Correct near-miss corpus terms in place; return the substitution log.

    Windows already spelling some corpus term exactly are never touched: with
    two neighbouring terms in one corpus ("prednisone" and "prednisolone"),
    either channel would otherwise happily rewrite a correct transcription into
    its sibling drug.
    """
    active_policy = policy or CorrectionPolicy()
    prepared = _prepare_terms(terms)
    if not prepared:
        return []
    protected = {term.casefold for term in prepared}
    protected_max_words = max(len(term.words) for term in prepared)

    corrections: list[dict[str, Any]] = []
    for segment in segments:
        tokens: list[str | None] = str(segment.get("text") or "").split()
        if not tokens:
            continue
        locked: set[int] = set()
        changed = False
        # Keyed by start index: candidates are applied best-first, but the log
        # reads in the order the words appear.
        segment_corrections: list[tuple[int, dict[str, Any]]] = []
        for term in prepared:
            for match, start, window_text, window in _rank_candidates(
                tokens, locked, term, protected, protected_max_words, active_policy
            ):
                positions = range(start, start + match.span)
                if any(position in locked for position in positions):
                    continue  # a better-ranked candidate already claimed a word
                replacement = _capitalize_like(term.text, window_text)
                tokens[start] = f"{window[0][0]}{replacement}{window[-1][2]}"
                for position in range(start + 1, start + match.span):
                    tokens[position] = None
                locked.update(positions)
                changed = True
                segment_corrections.append(
                    (
                        start,
                        {
                            "segmentId": segment.get("id"),
                            "original": window_text,
                            "corrected": replacement,
                            "ratio": round(match.ratio, 3),
                            "phoneticRatio": round(match.phonetic_ratio, 3),
                            "method": match.method,
                        },
                    )
                )
        if changed:
            segment["text"] = " ".join(token for token in tokens if token is not None)
        corrections.extend(entry for _, entry in sorted(segment_corrections, key=lambda item: item[0]))
    return corrections


def _rank_candidates(
    tokens: list[str | None],
    locked: set[int],
    term: _Term,
    protected: set[str],
    protected_max_words: int,
    policy: CorrectionPolicy,
) -> list[tuple[_Match, int, str, list[tuple[str, str, str]]]]:
    """Every accepted window for one term in one segment, best first.

    Candidates are ranked globally rather than applied left to right: the first
    window that clears the threshold is not always the right one, and a greedy
    scan would let "a nasal blog" win before the exact "nasal blog" one word
    later is ever considered.
    """
    span_limit = len(term.words) + (policy.max_extra_span_words if policy.phonetic_enabled else 0)
    candidates: list[tuple[_Match, int, str, list[tuple[str, str, str]]]] = []
    for start in range(len(tokens)):
        for span in range(1, span_limit + 1):
            if start + span > len(tokens):
                break
            if any(position in locked for position in range(start, start + span)):
                break
            window = [_core(str(tokens[position])) for position in range(start, start + span)]
            cores = [core for _, core, _ in window]
            window_text = " ".join(cores)
            if not window_text or _contains_protected(cores, protected, protected_max_words):
                continue
            match = _evaluate(window_text, _letters(window_text), span, term, policy)
            if match is not None:
                candidates.append((match, start, window_text, window))
    candidates.sort(key=lambda candidate: (candidate[0].rank, -candidate[1]), reverse=True)
    return candidates


def apply_replacements_to_file(path: Path, corrections: list[dict[str, Any]]) -> bool:
    """Best-effort apply of the substitution log to a subtitle file (SRT/VTT)
    so the player track agrees with the corrected transcript. Replacing all
    occurrences is safe: the "original" is a misrecognition — if it appears
    twice, it is wrong twice."""
    if not path.exists():
        return False
    text = path.read_text(encoding="utf-8")
    updated = text
    for correction in corrections:
        original = str(correction.get("original") or "")
        corrected = str(correction.get("corrected") or "")
        if len(original) >= _MIN_TERM_LENGTH and original != corrected:
            updated = updated.replace(original, corrected)
    if updated == text:
        return False
    path.write_text(updated, encoding="utf-8")
    return True
