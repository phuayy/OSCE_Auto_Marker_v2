"""Turn an ASR subprocess's stdout progress lines into one step percentage.

``--print_progress`` makes WhisperX print ``Progress: 42.10%...`` from *two*
independent loops — transcription and forced alignment — each counting from 0
to 100. Diarisation, which runs afterwards, prints nothing at all. A consumer
that forwarded the raw number would show the bar rewinding halfway through the
step and then sitting at 100% for the whole diarisation tail.

This tracker maps those raw readings onto a single never-decreasing percentage:
each phase owns a slice of the step, a reading that drops below the previous
one is read as the next phase starting, and the last slice stops short of 100
so the remaining span belongs to the silent diarisation pass.

Engines other than WhisperX report one counter rather than two, and configure
their own ``phase_spans`` accordingly — the parsing, monotonicity and
reporting-threshold rules are the same for all of them.
"""
from __future__ import annotations

import re
from typing import Iterable

# WhisperX writes exactly "Progress: 42.10%..." via print(); the surrounding
# text is matched loosely because the line may arrive with a captured prefix.
PROGRESS_PATTERN = re.compile(r"Progress:\s*(\d+(?:\.\d+)?)\s*%")

# (start, end) of the step each WhisperX progress loop occupies. Two loops run
# under the flags this pipeline passes: transcription then alignment. The 10
# points above the final phase are the diarisation pass, which reports nothing.
DEFAULT_PHASE_SPANS: tuple[tuple[float, float], ...] = ((0.0, 45.0), (45.0, 90.0))

# A raw reading has to fall at least this many points below the previous one to
# count as the next loop starting over. A smaller dip is an out-of-order line,
# not a new phase — treating one as a restart would skip a whole phase's span.
PHASE_RESET_DROP = 10.0


class ProgressTracker:
    """Accumulates progress lines into a monotonic 0-100 step percentage.

    Stateful and *not* thread-safe by design: ``update`` is called from the
    single output-callback coroutine chain of one WhisperX run, and callers
    that persist the result serialise their own writes.
    """

    def __init__(
        self,
        *,
        phase_spans: Iterable[tuple[float, float]] = DEFAULT_PHASE_SPANS,
        min_delta: float = 2.0,
    ) -> None:
        self._phase_spans = tuple(phase_spans)
        if not self._phase_spans:
            raise ValueError("phase_spans must not be empty")
        self._min_delta = float(min_delta)
        self._phase_index = 0
        self._last_raw: float | None = None
        self._percent = 0.0
        self._reported: float | None = None

    @property
    def percent(self) -> float:
        """Current overall step percentage, whether or not it was reported."""
        return self._percent

    def update(self, text: str) -> float | None:
        """Fold one output line in; return the percentage worth reporting.

        Returns ``None`` for a line that carries no progress reading, for a
        reading that does not advance the overall percentage, and for one that
        advances it by less than ``min_delta`` — so a caller can persist every
        returned value without throttling the write itself. The final value of
        a phase is always reported, so the bar lands on each phase boundary.
        """
        raw = self.parse(text)
        if raw is None:
            return None

        if self._last_raw is not None and raw < self._last_raw:
            if raw >= self._last_raw - PHASE_RESET_DROP:
                # A slight dip inside the same loop: ignore it rather than read
                # it as a restart, which would jump the bar a whole phase on.
                return None
            # The loop counted down to a small value again: the next phase began.
            self._phase_index = min(self._phase_index + 1, len(self._phase_spans) - 1)
        self._last_raw = raw

        start, end = self._phase_spans[self._phase_index]
        candidate = start + (end - start) * (raw / 100.0)
        # Never rewind: a later phase's floor is higher than an earlier phase's
        # ceiling, but a stray out-of-order line must not drag the bar back.
        percent = round(max(self._percent, min(candidate, end)), 2)
        if percent == self._percent and self._reported is not None:
            return None
        self._percent = percent

        is_phase_end = percent >= end
        if self._reported is not None and not is_phase_end and percent - self._reported < self._min_delta:
            return None
        self._reported = percent
        return percent

    @staticmethod
    def parse(text: str) -> float | None:
        """The raw percentage in one output line, or None if it carries none."""
        match = PROGRESS_PATTERN.search(str(text or ""))
        if match is None:
            return None
        try:
            value = float(match.group(1))
        except ValueError:  # pragma: no cover - regex guarantees a float literal
            return None
        return min(max(value, 0.0), 100.0)
