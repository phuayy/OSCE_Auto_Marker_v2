"""Diarisation speaker bounds passed to the WhisperX CLI.

An OSCE station records a known cast, so the clustering is told the count
instead of estimating it — unconstrained pyannote regularly splits one person
across two labels mid-consultation. These tests pin the flags that carry that
constraint, and the escape hatches for stations with a different cast.
"""
from __future__ import annotations

from pathlib import Path

from tests.test_whisperx_invocation import make_media, run_transcription


def flag_value(args: list[str], flag: str) -> str | None:
    return args[args.index(flag) + 1] if flag in args else None


def test_defaults_constrain_diarization_to_two_speakers(tmp_path: Path) -> None:
    media, runner = make_media(tmp_path)
    run_transcription(media, tmp_path)

    args = runner.whisperx_args()
    assert flag_value(args, "--min_speakers") == "2"
    assert flag_value(args, "--max_speakers") == "2"


def test_bounds_are_configurable_for_stations_with_an_examiner(tmp_path: Path) -> None:
    media, runner = make_media(tmp_path, whisperx_min_speakers=2, whisperx_max_speakers=3)
    run_transcription(media, tmp_path)

    args = runner.whisperx_args()
    assert flag_value(args, "--min_speakers") == "2"
    assert flag_value(args, "--max_speakers") == "3"


def test_zero_bounds_restore_whisperx_speaker_estimation(tmp_path: Path) -> None:
    media, runner = make_media(tmp_path, whisperx_min_speakers=0, whisperx_max_speakers=0)
    run_transcription(media, tmp_path)

    args = runner.whisperx_args()
    assert "--min_speakers" not in args
    assert "--max_speakers" not in args


def test_each_bound_can_be_dropped_independently(tmp_path: Path) -> None:
    media, runner = make_media(tmp_path, whisperx_min_speakers=0, whisperx_max_speakers=2)
    run_transcription(media, tmp_path)

    args = runner.whisperx_args()
    assert "--min_speakers" not in args
    assert flag_value(args, "--max_speakers") == "2"


def test_negative_bound_is_treated_as_unset(tmp_path: Path) -> None:
    media, runner = make_media(tmp_path, whisperx_min_speakers=-1, whisperx_max_speakers=2)
    run_transcription(media, tmp_path)

    assert "--min_speakers" not in runner.whisperx_args()


def test_minimum_above_maximum_is_clamped_not_forwarded(tmp_path: Path) -> None:
    # The CLI would reject this only after loading the models, so the
    # misconfiguration is resolved before the process starts.
    media, runner = make_media(tmp_path, whisperx_min_speakers=4, whisperx_max_speakers=2)
    run_transcription(media, tmp_path)

    args = runner.whisperx_args()
    assert flag_value(args, "--min_speakers") == "2"
    assert flag_value(args, "--max_speakers") == "2"


def test_bounds_do_not_disturb_the_existing_flags(tmp_path: Path) -> None:
    media, runner = make_media(tmp_path)
    session = {"id": "session-1", "corpus": {"name": "URTI", "terms": ["paracetamol"]}}
    run_transcription(media, tmp_path, session)

    args = runner.whisperx_args()
    assert flag_value(args, "--model") == "large-v3"
    assert flag_value(args, "--hotwords") == "paracetamol"
    assert "--diarize" in args
