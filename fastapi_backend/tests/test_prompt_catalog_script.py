"""Pins scripts/prompt_catalog.py against the live prompt-owning modules.

Guards the one way this catalog can silently drift: a prompt edit that bumps
a PROMPT_VERSION-style constant but the catalog script (or vice versa) falls
out of step, so the ledger records the wrong version for the wording it
snapshotted.
"""
from __future__ import annotations

import json
import runpy
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"


def _load_catalog() -> dict:
    sys.path.insert(0, str(SCRIPTS))
    try:
        module = runpy.run_path(str(SCRIPTS / "prompt_catalog.py"), run_name="prompt_catalog")
        entries = module["build_catalog"]()
    finally:
        sys.path.remove(str(SCRIPTS))
    return entries


def test_catalog_has_one_entry_per_declared_version_pair() -> None:
    sys.path.insert(0, str(SCRIPTS))
    try:
        cm = runpy.run_path(str(SCRIPTS / "content_marking.py"), run_name="content_marking")
        comm = runpy.run_path(str(SCRIPTS / "nvidia_osce_communication.py"), run_name="nvidia_osce_communication")
        preprocessor = runpy.run_path(
            str(SCRIPTS / "nemotron_transcript_preprocessor.py"), run_name="nemotron_transcript_preprocessor"
        )
    finally:
        sys.path.remove(str(SCRIPTS))

    entries = _load_catalog()
    by_key = {entry["key"]: entry for entry in entries}

    assert len(entries) == 11
    assert len(by_key) == 11  # every key unique

    content_marking_keys = {
        "content_marking.system",
        "content_marking.user",
        "content_marking.repair",
    }
    adjudication_keys = {
        "content_marking.adjudication_system",
        "content_marking.adjudication_user_template",
        "content_marking.feedback_merge_system",
        "content_marking.feedback_merge_user_template",
    }
    for key in content_marking_keys:
        assert by_key[key]["version"] == cm["PROMPT_VERSION"]
        assert by_key[key]["sourceScript"] == "content_marking.py"
    for key in adjudication_keys:
        assert by_key[key]["version"] == cm["ADJUDICATION_PROMPT_VERSION"]
        assert by_key[key]["sourceScript"] == "content_marking.py"

    communication_keys = {"communication.system", "communication.user_template", "communication.repair"}
    for key in communication_keys:
        assert by_key[key]["version"] == comm["COMMUNICATION_PROMPT_VERSION"]
        assert by_key[key]["sourceScript"] == "nvidia_osce_communication.py"

    assert by_key["preprocessor.system"]["version"] == preprocessor["PREPROCESSOR_PROMPT_VERSION"]
    assert by_key["preprocessor.system"]["sourceScript"] == "nemotron_transcript_preprocessor.py"
    assert by_key["preprocessor.system"]["text"] == preprocessor["SYSTEM_PROMPT"]

    for entry in entries:
        assert isinstance(entry["text"], str) and entry["text"]


def test_main_prints_one_line_of_valid_json_to_stdout(capsys) -> None:
    sys.path.insert(0, str(SCRIPTS))
    try:
        module = runpy.run_path(str(SCRIPTS / "prompt_catalog.py"), run_name="prompt_catalog_under_test")
        module["main"]()
    finally:
        sys.path.remove(str(SCRIPTS))

    captured = capsys.readouterr()
    lines = [line for line in captured.out.splitlines() if line.strip()]
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert len(payload["entries"]) == 11
