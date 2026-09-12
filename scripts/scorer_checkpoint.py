"""Crash checkpoints for the LLM scoring scripts.

A scorer makes a handful of expensive model calls and may be killed between
them — a restart, an OOM, an operator's Ctrl-C. Rather than pay for every
call again, a script writes its state to a sidecar file after each one and,
when it starts, resumes from that file if the *context* still matches: same
inputs (by path, size and mtime), same routing, same rubric shape. Anything
else about the run having changed invalidates the checkpoint, so a
half-finished sheet can never be spliced onto different inputs or a different
model's output.

The helpers are script-agnostic; each script names its own ``schema`` so an
assessor checkpoint is never mistaken for an adjudicator's.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from llm_bootstrap import write_text_atomic


class CheckpointMessage:
    """A completion restored from disk after a crash.

    Stands in for a live provider response, so the repair loop cannot tell the
    difference between resuming and having just made the call. It records which
    model produced the text as well: the output file names the model that
    actually scored the student, and a resumed run must not relabel that as
    whatever is configured today.
    """

    def __init__(self, content: str, model: str = "", provider_id: str = "") -> None:
        self.content = content
        self.model = model
        self.provider_id = provider_id


def checkpoint_path_for_output(output_path: Path) -> Path:
    return output_path.with_name(f".{output_path.name}.checkpoint.json")


def read_checkpoint(path: Path | None) -> dict[str, Any] | None:
    if path is None or not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def checkpoint_matches(payload: dict[str, Any] | None, context: dict[str, Any], *, schema: str) -> bool:
    if not isinstance(payload, dict):
        return False
    if payload.get("schema") != schema:
        return False
    saved_context = payload.get("context")
    return isinstance(saved_context, dict) and saved_context == context


def write_checkpoint(
    path: Path | None,
    context: dict[str, Any],
    state: dict[str, Any],
    *,
    schema: str,
) -> None:
    if path is None:
        return
    payload = {
        "schema": schema,
        "context": context,
        "state": state,
        "updated_at": time.time(),
    }
    write_text_atomic(path, json.dumps(payload, indent=2, ensure_ascii=False) + "\n")


def checkpoint_file_signature(path: Path) -> dict[str, Any]:
    stats = path.stat()
    return {
        "path": str(path.resolve()),
        "size": stats.st_size,
        "mtime_ns": stats.st_mtime_ns,
    }


__all__ = [
    "CheckpointMessage",
    "checkpoint_file_signature",
    "checkpoint_matches",
    "checkpoint_path_for_output",
    "read_checkpoint",
    "write_checkpoint",
]
