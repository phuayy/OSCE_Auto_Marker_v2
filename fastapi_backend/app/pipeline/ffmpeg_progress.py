"""How far along an ffmpeg run is, read from its ``-progress pipe:1`` output.

ffmpeg writes a block of ``key=value`` lines to that pipe about twice a second:
``out_time_us=…`` (how much of the output has been written, in microseconds),
``out_time_ms=…`` (the same number — the name is a long-standing ffmpeg
mistake, it is not milliseconds), and a closing ``progress=continue`` or
``progress=end``. Dividing by the source's duration gives the fraction.

The parser is a pure state machine over lines so it can be tested without a
process; :func:`progress_output_handler` adapts it to ``CommandRunner``'s
``on_output`` callback, which is an observer run on the event loop.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from app.core.process import OutputCallback

# A finished run is reported by the caller once the file is published, so a
# reading never claims completion on its own.
MAX_RUNNING_FRACTION = 0.99

_OUT_TIME_KEYS = frozenset({"out_time_us", "out_time_ms"})


class FfmpegProgress:
    """Turns progress lines into a fraction of ``duration_seconds`` in [0, 0.99]."""

    def __init__(self, duration_seconds: float) -> None:
        self._duration_us = max(0.0, float(duration_seconds or 0)) * 1_000_000
        self._last: float | None = None

    def feed(self, text: str) -> float | None:
        """Consume output text; return the new fraction, or ``None`` when it
        carried no reading (other keys, ``N/A``, garbage, an unchanged value, or
        an unknown duration)."""
        if self._duration_us <= 0:
            return None
        fraction: float | None = None
        for line in text.splitlines():
            key, separator, value = line.strip().partition("=")
            if not separator:
                continue
            if key in _OUT_TIME_KEYS:
                try:
                    microseconds = int(value)
                except ValueError:
                    continue  # "N/A", empty, or a truncated line
                if microseconds < 0:
                    continue  # ffmpeg prints a negative sentinel before the first frame
                fraction = min(MAX_RUNNING_FRACTION, microseconds / self._duration_us)
            elif key == "progress" and value.strip() == "end":
                fraction = MAX_RUNNING_FRACTION
        if fraction is None or fraction == self._last:
            return None
        self._last = fraction
        return fraction


def progress_output_handler(
    duration_seconds: float, report: Callable[[float], Awaitable[None]]
) -> OutputCallback | None:
    """An ``on_output`` callback that calls ``report(fraction)`` as ffmpeg
    advances, or ``None`` when the duration is unknown (nothing to divide by,
    so the runner skips the per-line hand-off entirely)."""
    if not duration_seconds or duration_seconds <= 0:
        return None
    parser = FfmpegProgress(duration_seconds)

    async def handle(stream: str, text: str) -> None:
        if stream != "stdout":
            return
        fraction = parser.feed(text)
        if fraction is not None:
            await report(fraction)

    return handle
