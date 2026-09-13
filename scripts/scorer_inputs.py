"""Input resolution shared by the scorer subprocesses.

A scorer is handed every path it reads. It used to fall back to guessing when
a flag was omitted: the newest ``storage/sessions/*.json`` for the session id,
the raw WhisperX ``.srt`` over the normalised transcript, the newest PDF in the
upload folder for the rubric. Each guess is right on a developer's machine with
one session and wrong in production, where two sessions score in parallel and
"newest" belongs to someone else — and the result looks like a real score.

So: a required input that is missing is a usage error, reported with the flag
and the path, exit code 2. Nothing here searches a directory.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


class InputError(Exception):
    """A required input was not given or does not exist. Exit code 2."""


EXIT_USAGE = 2


def required_file(value: str | None, *, flag: str, label: str) -> Path:
    """The existing file named by ``value``, or :class:`InputError` naming ``flag``."""
    if not value or not str(value).strip():
        raise InputError(f"{label} is required: pass {flag} <path>.")
    path = Path(str(value)).expanduser().resolve()
    if not path.is_file():
        raise InputError(f"{label} does not exist: {flag} {path}")
    return path


def optional_file(value: str | None, *, flag: str, label: str) -> Path | None:
    """``None`` when the flag was omitted; the existing file when it was given.

    An optional input that *was* named but is missing is still an error — the
    caller meant to provide it, and silently scoring without it would change
    the result without changing the report.
    """
    if not value or not str(value).strip():
        return None
    return required_file(value, flag=flag, label=label)


def optional_directory(value: str | None, *, create: bool = False) -> Path | None:
    """``None`` when the flag was omitted; the directory when it was given.

    Unlike :func:`optional_file` this does not fail when the directory is
    missing. The only caller is the rubric cache, which is an optimisation: a
    cache directory that does not exist yet (or cannot be created) means the
    work is done in-process, never that the run stops.
    """
    if not value or not str(value).strip():
        return None
    path = Path(str(value)).expanduser().resolve()
    if create:
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError:
            return None
    return path


def session_id_from(args: argparse.Namespace, fallback_path: Path) -> str:
    """``--session-id`` when given, else the stem of the primary input file.

    The id only names the output file and log lines; nothing is looked up by it.
    """
    explicit = str(getattr(args, "session_id", "") or "").strip()
    return explicit or fallback_path.stem


def run_main(main, *, script_name: str) -> None:
    """Entry-point wrapper giving every scorer the same exit-code contract.

    0 success · 1 runtime failure (model, parsing, I/O) · 2 usage: an input the
    caller had to supply is missing. The API's job queue does not retry a 2 —
    the same flags will be just as absent next time.
    """
    try:
        raise SystemExit(main())
    except InputError as error:
        print(f"[{script_name}] input error: {error}", file=sys.stderr)
        raise SystemExit(EXIT_USAGE) from None
    except SystemExit:
        raise
    except Exception as error:  # noqa: BLE001 - the process boundary is where everything is reported
        print(f"Error: {error}", file=sys.stderr)
        raise SystemExit(1) from None


__all__ = [
    "EXIT_USAGE",
    "InputError",
    "optional_directory",
    "optional_file",
    "required_file",
    "run_main",
    "session_id_from",
]
