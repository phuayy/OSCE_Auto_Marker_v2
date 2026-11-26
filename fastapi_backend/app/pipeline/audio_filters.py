"""The one rule for what may be handed to ``ffmpeg -af``.

An ffmpeg filter graph is a small programming language: ``amovie`` / ``movie``
read any local file or URL the server can reach, ``ladspa`` / ``lv2`` load
shared libraries, and a ``;`` or ``[label]`` wires several of them together.
The transcription engines' ``audioFilters`` option feeds this string straight
to ffmpeg, so it is restricted to what an audio clean-up actually needs: a
plain comma-separated chain of allow-listed filters, each ``name`` or
``name=key=value:key=value`` with values made of characters that cannot open a
graph, quote, or escape. No filter in ``ALLOWED_FILTERS`` opens a file.

An empty chain is valid and means "unfiltered". The check is for everyone,
administrators included — an admin's typo should not become a file read either.
"""

from __future__ import annotations

import re

ALLOWED_FILTERS: frozenset[str] = frozenset(
    {
        "acompressor",
        "afftdn",
        "agate",
        "aformat",
        "anlmdn",
        "aresample",
        "bandpass",
        "bandreject",
        "compand",
        "dynaudnorm",
        "equalizer",
        "highpass",
        "loudnorm",
        "lowpass",
        "pan",
        "silenceremove",
        "speechnorm",
        "volume",
    }
)

MAX_CHAIN_LENGTH = 500

# Characters a filter option may contain. Excluded on purpose: ``;`` (a second
# chain), ``[`` / ``]`` (pad labels that wire filters together), quotes and
# backslash (ffmpeg's escaping), whitespace, and ``,`` (the chain separator).
_OPTION_PATTERN = re.compile(r"^[A-Za-z0-9_.+\-*/:=|]+$")
_NAME_PATTERN = re.compile(r"^[a-z0-9_]+$")


def validate_filter_chain(chain: str) -> str:
    """Return ``chain`` stripped if it is a safe filter chain, else raise
    ``ValueError`` naming what is wrong. ``""`` is the valid "no filters"."""
    text = str(chain or "").strip()
    if not text:
        return ""
    if len(text) > MAX_CHAIN_LENGTH:
        raise ValueError(f"The filter chain is longer than {MAX_CHAIN_LENGTH} characters.")
    for part in text.split(","):
        name, separator, options = part.partition("=")
        if not _NAME_PATTERN.match(name):
            raise ValueError(f"'{part}' is not a plain 'filter' or 'filter=option=value' entry.")
        if name not in ALLOWED_FILTERS:
            allowed = ", ".join(sorted(ALLOWED_FILTERS))
            raise ValueError(f"The ffmpeg filter '{name}' is not allowed. Allowed filters: {allowed}.")
        if separator and not _OPTION_PATTERN.match(options):
            raise ValueError(
                f"The options of '{name}' may only use letters, digits and . _ + - * / : = | "
                "(no quotes, brackets, semicolons or spaces)."
            )
    return text
