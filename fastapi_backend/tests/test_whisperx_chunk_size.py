"""VAD chunk size passed to the WhisperX CLI.

WhisperX merges VAD segments up to ``--chunk_size`` seconds before a single
decode. The CLI default of 30 spans several speaker turns in a consultation, so
every word in the chunk shares one decode context and one avg_logprob; the
pipeline asks for 20 instead.
"""
from __future__ import annotations

from pathlib import Path

from tests.test_whisperx_invocation import make_media, run_transcription


def test_default_chunk_size_is_twenty_seconds(tmp_path: Path) -> None:
    media, runner = make_media(tmp_path)
    run_transcription(media, tmp_path)

    args = runner.whisperx_args()
    assert args[args.index("--chunk_size") + 1] == "20"


def test_chunk_size_is_configurable(tmp_path: Path) -> None:
    media, runner = make_media(tmp_path, whisperx_chunk_size=15)
    run_transcription(media, tmp_path)

    args = runner.whisperx_args()
    assert args[args.index("--chunk_size") + 1] == "15"


def test_zero_chunk_size_falls_back_to_the_whisperx_default(tmp_path: Path) -> None:
    media, runner = make_media(tmp_path, whisperx_chunk_size=0)
    run_transcription(media, tmp_path)

    assert "--chunk_size" not in runner.whisperx_args()


def test_chunk_size_coexists_with_the_speaker_bounds(tmp_path: Path) -> None:
    media, runner = make_media(tmp_path)
    run_transcription(media, tmp_path)

    args = runner.whisperx_args()
    # Flags are positional pairs; a value must never be read as a flag name.
    assert args[args.index("--chunk_size") + 1] == "20"
    assert args[args.index("--min_speakers") + 1] == "2"
    assert args[args.index("--max_speakers") + 1] == "2"
    assert args.count("--chunk_size") == 1
