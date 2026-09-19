#!/usr/bin/env python3
"""Prints the current wording of every LLM prompt this pipeline uses.

Run as its own subprocess (same convention as every scorer), never imported
by the API process — importing ``nvidia_osce_communication`` or
``nemotron_transcript_preprocessor`` runs their module-level ``load_env_file``
call, which is meant for a standalone script, not a long-lived server.

Each entry captures the *static wording* a ``PROMPT_VERSION``-style constant
already stands for, not one run's rendered output: builder functions that take
runtime data (rubric criteria, transcript text, disputes) are called with
small placeholders so the dynamic sections collapse to an empty/representative
stand-in and only the wording around them is captured. This is exactly what
"bump the version when the wording changes" has always meant.

Prints ``{"entries": [{"key", "version", "text", "sourceScript"}, ...]}`` as
one line of JSON to stdout; diagnostics go to stderr.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import content_marking as cm  # noqa: E402
import nemotron_transcript_preprocessor as preprocessor  # noqa: E402
import nvidia_osce_communication as comm  # noqa: E402

_PLACEHOLDER_RUBRIC_CRITERIA = [{"label": "<rubric criterion label>", "is_critical": False}]
_PLACEHOLDER_COMMUNICATION_RUBRIC = {
    "criteria": [
        {
            "id": 1,
            "label": "<rubric criterion label>",
            "section": "<section>",
            "indicators": ["<observable indicator>"],
        }
    ]
}


def _content_marking_entries() -> list[dict[str, str]]:
    return [
        {
            "key": "content_marking.system",
            "version": cm.PROMPT_VERSION,
            "text": cm.build_system_prompt(_PLACEHOLDER_RUBRIC_CRITERIA),
        },
        {
            "key": "content_marking.user",
            "version": cm.PROMPT_VERSION,
            "text": cm.build_user_prompt(
                session_id="<session_id>",
                transcript_path=Path("<transcript_path>"),
                case_study_path=Path("<case_study_path>"),
                rubric_criteria=_PLACEHOLDER_RUBRIC_CRITERIA,
                transcript_text="<transcript text>",
                case_study_context_text="<case study context>",
                rubric_section_text="<rubric section>",
            ),
        },
        {
            "key": "content_marking.repair",
            "version": cm.PROMPT_VERSION,
            "text": cm.CONTENT_REPAIR_PREAMBLE,
        },
        {
            "key": "content_marking.adjudication_system",
            "version": cm.ADJUDICATION_PROMPT_VERSION,
            "text": cm.build_adjudication_system_prompt(),
        },
        {
            "key": "content_marking.adjudication_user_template",
            "version": cm.ADJUDICATION_PROMPT_VERSION,
            "text": cm.build_adjudication_user_prompt("<session_id>", []),
        },
        {
            "key": "content_marking.feedback_merge_system",
            "version": cm.ADJUDICATION_PROMPT_VERSION,
            "text": cm.build_feedback_merge_system_prompt(),
        },
        {
            "key": "content_marking.feedback_merge_user_template",
            "version": cm.ADJUDICATION_PROMPT_VERSION,
            "text": cm.build_feedback_merge_user_prompt("<session_id>", [], []),
        },
    ]


def _communication_entries() -> list[dict[str, str]]:
    return [
        {
            "key": "communication.system",
            "version": comm.COMMUNICATION_PROMPT_VERSION,
            "text": comm.build_system_prompt(_PLACEHOLDER_COMMUNICATION_RUBRIC),
        },
        {
            "key": "communication.user_template",
            "version": comm.COMMUNICATION_PROMPT_VERSION,
            "text": comm.build_user_prompt(
                session_id="<session_id>",
                transcript_path=Path("<transcript_path>"),
                audio_prof_path=None,
                rubric_source_pdf=None,
                rubric=_PLACEHOLDER_COMMUNICATION_RUBRIC,
                transcript_text="<transcript text>",
                audio_prof_text="<audio professionalism JSON>",
                opensmile_text="<opensmile features JSON>",
            ),
        },
        {
            "key": "communication.repair",
            "version": comm.COMMUNICATION_PROMPT_VERSION,
            "text": comm.build_repair_prompt([], "", 0),
        },
    ]


def _preprocessor_entries() -> list[dict[str, str]]:
    return [
        {
            "key": "preprocessor.system",
            "version": preprocessor.PREPROCESSOR_PROMPT_VERSION,
            "text": preprocessor.SYSTEM_PROMPT,
        },
    ]


def build_catalog() -> list[dict[str, str]]:
    entries = [
        *_content_marking_entries(),
        *_communication_entries(),
        *_preprocessor_entries(),
    ]
    for entry in entries:
        entry["sourceScript"] = {
            "content_marking": "content_marking.py",
            "communication": "nvidia_osce_communication.py",
            "preprocessor": "nemotron_transcript_preprocessor.py",
        }[entry["key"].split(".", 1)[0]]
    return entries


def main() -> int:
    entries = build_catalog()
    print(f"[prompt_catalog] built {len(entries)} prompt catalog entries", file=sys.stderr)
    print(json.dumps({"entries": entries}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
