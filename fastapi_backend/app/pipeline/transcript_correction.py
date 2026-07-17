"""Deterministic corpus-term correction for normalized WhisperX transcripts.

Slides an n-word window over each segment's text and replaces windows that are
fuzzily close to a corpus term (stdlib difflib, no API calls). Every
substitution is recorded so the correction is fully explainable.
"""
from __future__ import annotations

from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

_EDGE_PUNCTUATION = ".,;:!?…\"'()[]{}"

# Terms shorter than this are too ambiguous to fuzzy-match safely.
_MIN_TERM_LENGTH = 4


def _core(token: str) -> tuple[str, str, str]:
    """Split a token into (leading punctuation, core, trailing punctuation)."""
    start = 0
    end = len(token)
    while start < end and token[start] in _EDGE_PUNCTUATION:
        start += 1
    while end > start and token[end - 1] in _EDGE_PUNCTUATION:
        end -= 1
    return token[:start], token[start:end], token[end:]


def _effective_min_ratio(term: str, min_ratio: float) -> float:
    # ponytail: short windows keep the strict threshold ("black" vs "block" is
    # 0.80); long medical words relax slightly so e.g. "parasitamol" ->
    # "paracetamol" (0.818) is caught. Tune via TRANSCRIPT_CORRECTION_MIN_RATIO.
    return min_ratio if len(term) < 8 else max(0.0, min_ratio - 0.04)


def correct_segments(
    segments: list[dict[str, Any]],
    terms: list[Any],
    min_ratio: float = 0.84,
) -> list[dict[str, Any]]:
    """Correct near-miss corpus terms in-place; return the substitution log.

    Multi-word terms are matched against same-length word windows, so indices
    stay stable. Longer terms are applied first and matched spans are locked,
    so a shorter term can never mangle a longer term's replacement.
    """
    cleaned_terms = [str(term or "").strip() for term in terms]
    cleaned_terms = [term for term in cleaned_terms if len(term) >= _MIN_TERM_LENGTH]
    cleaned_terms.sort(key=lambda term: (len(term.split()), len(term)), reverse=True)
    if not cleaned_terms:
        return []

    corrections: list[dict[str, Any]] = []
    for segment in segments:
        words = str(segment.get("text") or "").split()
        if not words:
            continue
        locked: set[int] = set()
        changed = False
        for term in cleaned_terms:
            term_cf = term.casefold()
            window_size = len(term.split())
            if window_size > len(words):
                continue
            threshold = _effective_min_ratio(term_cf, min_ratio)
            index = 0
            while index + window_size <= len(words):
                span = range(index, index + window_size)
                if any(position in locked for position in span):
                    index += 1
                    continue
                window = [_core(words[position]) for position in span]
                window_text = " ".join(core for _, core, _ in window)
                window_cf = window_text.casefold()
                if not window_text or window_cf == term_cf:
                    index += 1
                    continue
                if abs(len(window_cf) - len(term_cf)) > max(2, int(len(term_cf) * 0.4)):
                    index += 1
                    continue
                ratio = SequenceMatcher(None, window_cf, term_cf).ratio()
                if ratio < threshold:
                    index += 1
                    continue
                replacement = term[0].upper() + term[1:] if window_text[0].isupper() else term
                replacement_words = replacement.split()
                for offset, position in enumerate(span):
                    prefix = window[offset][0] if offset == 0 else ""
                    suffix = window[offset][2] if offset == window_size - 1 else ""
                    words[position] = f"{prefix}{replacement_words[offset]}{suffix}"
                locked.update(span)
                changed = True
                corrections.append(
                    {
                        "segmentId": segment.get("id"),
                        "original": window_text,
                        "corrected": replacement,
                        "ratio": round(ratio, 3),
                    }
                )
                index += window_size
        if changed:
            segment["text"] = " ".join(words)
    return corrections


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
