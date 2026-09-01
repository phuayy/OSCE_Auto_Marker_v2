"""Post-hoc hallucination screening for normalized transcripts.

Whisper's own decode-time guards (``logprob_threshold``,
``compression_ratio_threshold``, ``no_speech_threshold``) never fire here: the
WhisperX CLI is invoked without them, and Canary-Qwen has no equivalent at all.
The same signature is therefore checked after the fact, against the normalized
segments, where it is also explainable: every flagged segment is recorded in
``transcript["hallucinations"]`` the way corpus substitutions are recorded in
``transcript["corrections"]``.

Four independent tests, any one of which flags a segment:

* ``avg_logprob`` below ``min_avg_logprob`` (the decoder itself had no
  confidence in the window; only checked when the engine reported one).
* gzip compression ratio above ``max_compression_ratio`` — verbatim repetition
  compresses far better than speech does.
* an n-gram repeated consecutively more than ``max_ngram_repeats_allowed``
  times.
* an exact match against the known Whisper / Canary-Qwen filler set.

The failure this prevents is specific: a hallucinated "I understand, that must
be difficult for you" is scored by the communication branch as genuine empathy
the student never showed.
"""
from __future__ import annotations

import gzip
import re
from typing import Any

# Phrases both engines emit over silence, music or crosstalk. Whisper's come
# from subtitle-scraped training data (YouTube outros, subtitle-house credits);
# Canary-Qwen's are its degenerate short outputs. Matched against the whole
# normalized segment only — "thank you for coming in today" is real speech and
# must survive.
FILLER_PHRASES: frozenset[str] = frozenset(
    {
        "thank you",
        "thank you very much",
        "thank you for watching",
        "thanks for watching",
        "thank you so much for watching",
        "please subscribe",
        "please subscribe to my channel",
        "subscribe to my channel",
        "like and subscribe",
        "see you next time",
        "see you in the next video",
        "bye",
        "bye bye",
        "goodbye",
        "you",
        "the end",
        "mm",
        "mmm",
        "hmm",
        "uh",
        "um",
        # Stored in normalize_phrase() form: "Amara.org" loses its dot.
        "subtitles by the amara org community",
        "amara org",
        "www amara org",
        "subtitles by",
        "transcription by castingwords",
        "copyright",
        "all rights reserved",
        "music",
        "applause",
        "silence",
        "background noise",
        "no speech",
    }
)

_PUNCTUATION = re.compile(r"[^\w\s]+", re.UNICODE)
_WHITESPACE = re.compile(r"\s+")

DEFAULT_MIN_AVG_LOGPROB = -1.0
DEFAULT_MAX_COMPRESSION_RATIO = 2.4
DEFAULT_MAX_NGRAM_REPEATS = 2
# Longer phrases than this are not degenerate repetition, they are a speaker
# repeating themselves for emphasis.
_MAX_NGRAM_WORDS = 6


def normalize_phrase(text: str) -> str:
    """Casefold, strip punctuation and collapse whitespace for filler lookup."""
    stripped = _PUNCTUATION.sub(" ", str(text or ""))
    return _WHITESPACE.sub(" ", stripped).strip().casefold()


def compression_ratio(text: str) -> float:
    """Whisper's own metric: uncompressed length over gzip length."""
    payload = str(text or "").encode("utf-8")
    if not payload:
        return 0.0
    return len(payload) / len(gzip.compress(payload))


def max_ngram_repeats(text: str) -> tuple[int, str]:
    """Longest run of one consecutively repeated n-gram, and that n-gram.

    "no no no no" scores 4 and "I understand. I understand. I understand."
    scores 3; ordinary speech scores 1.
    """
    words = normalize_phrase(text).split()
    if not words:
        return 0, ""
    best_count = 1
    best_gram = words[0]
    for size in range(1, min(_MAX_NGRAM_WORDS, len(words)) + 1):
        index = 0
        while index + size <= len(words):
            gram = words[index : index + size]
            count = 1
            probe = index + size
            while probe + size <= len(words) and words[probe : probe + size] == gram:
                count += 1
                probe += size
            if count > best_count:
                best_count = count
                best_gram = " ".join(gram)
            index += size * count if count > 1 else 1
    return best_count, best_gram


def _inspect(
    segment: dict[str, Any],
    min_avg_logprob: float,
    max_compression_ratio: float,
    max_ngram_repeats_allowed: int,
) -> dict[str, Any] | None:
    """Return a flag record for one segment, or None if it looks like speech."""
    text = str(segment.get("text") or "").strip()
    if not text:
        return None

    reasons: list[str] = []
    metrics: dict[str, Any] = {}

    avg_logprob = segment.get("avgLogprob")
    if isinstance(avg_logprob, (int, float)) and not isinstance(avg_logprob, bool):
        metrics["avgLogprob"] = round(float(avg_logprob), 4)
        if float(avg_logprob) < min_avg_logprob:
            reasons.append("low_avg_logprob")

    ratio = compression_ratio(text)
    metrics["compressionRatio"] = round(ratio, 3)
    if ratio > max_compression_ratio:
        reasons.append("high_compression_ratio")

    repeats, gram = max_ngram_repeats(text)
    metrics["ngramRepeats"] = repeats
    if repeats > max_ngram_repeats_allowed:
        metrics["repeatedNgram"] = gram
        reasons.append("repeated_ngram")

    if normalize_phrase(text) in FILLER_PHRASES:
        reasons.append("filler_phrase")

    if not reasons:
        return None
    return {
        "segmentId": segment.get("id"),
        "start": segment.get("start"),
        "end": segment.get("end"),
        "speaker": segment.get("speaker"),
        "text": text,
        "reasons": reasons,
        "metrics": metrics,
    }


def screen_segments(
    segments: list[dict[str, Any]],
    *,
    drop: bool = False,
    min_avg_logprob: float = DEFAULT_MIN_AVG_LOGPROB,
    max_compression_ratio: float = DEFAULT_MAX_COMPRESSION_RATIO,
    max_ngram_repeats_allowed: int = DEFAULT_MAX_NGRAM_REPEATS,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Screen segments and return ``(kept_segments, flag_records)``.

    Flagged segments are annotated in place with a ``hallucination`` block, so
    a transcript read on its own still explains itself. With ``drop`` they are
    also removed from the returned list — except when every segment is flagged,
    which is a broken screen rather than a fully hallucinated encounter: the run
    then keeps all segments and each record says so. Segment ids are never
    renumbered, so ``corrections`` entries stay resolvable across a drop.
    """
    flags: list[dict[str, Any]] = []
    flagged_indexes: list[int] = []
    for index, segment in enumerate(segments):
        record = _inspect(segment, min_avg_logprob, max_compression_ratio, max_ngram_repeats_allowed)
        if record is None:
            continue
        flags.append(record)
        flagged_indexes.append(index)

    if not flags:
        return list(segments), []

    drop_all = drop and len(flagged_indexes) == len(segments)
    dropping = drop and not drop_all
    for position, index in enumerate(flagged_indexes):
        record = flags[position]
        record["action"] = "dropped" if dropping else "flagged"
        if drop_all:
            record["retained"] = "every_segment_flagged"
        segments[index]["hallucination"] = {
            "reasons": record["reasons"],
            "metrics": record["metrics"],
            "action": record["action"],
        }

    if not dropping:
        return list(segments), flags
    dropped = set(flagged_indexes)
    return [segment for index, segment in enumerate(segments) if index not in dropped], flags
