"""Per-session SSE costs nothing when nobody can listen.

``SESSION_SSE_ENABLED`` defaults to false — the browser drives live state from
the change feed — yet every scorer, marker and diariser still installed a
per-line ``on_output`` callback that forwarded each line of subprocess output
into ``EventService.publish``. ``publish`` returned immediately, but getting
there was not free: ``CommandRunner`` drains stdout and stderr on their own
threads, so every line paid an ``asyncio.run_coroutine_threadsafe`` hop into the
event loop to reach that early return. A WhisperX or scorer run emits thousands.

The producers now ask ``EventService.log_sink`` for the callback, and it hands
back ``None`` when the stream is disabled — which ``CommandRunner`` reads as
"there is no callback", skipping the hand-off entirely.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from app.core.config import Settings
from app.core.process import CommandRunner
from app.services.event_service import EventService


def _service(enabled: bool) -> EventService:
    return EventService(Settings(session_sse_enabled=enabled))


def test_a_disabled_stream_hands_out_no_callback() -> None:
    assert _service(False).enabled is False
    assert _service(False).log_sink("session-1", "scorer") is None


def test_an_enabled_stream_forwards_every_line_under_its_own_source() -> None:
    service = _service(True)
    sink = service.log_sink("session-1", "scorer")
    assert sink is not None

    asyncio.run(sink("stdout", "marking criterion 3"))
    asyncio.run(sink("stderr", "warning: slow"))

    history = service.get_state("session-1").history
    assert [name for name, _ in history] == ["log", "log"]
    assert history[0][1]["source"] == "scorer-stdout"
    assert history[0][1]["message"] == "marking criterion 3"
    assert history[1][1]["source"] == "scorer-stderr"


def test_command_runner_makes_no_per_line_hop_without_a_callback() -> None:
    """The saving is in ``CommandRunner``, so assert it there: with
    ``on_output=None`` the drain threads never schedule anything on the loop."""
    scheduled: list[str] = []
    real_run_coroutine_threadsafe = asyncio.run_coroutine_threadsafe

    def counting(coro, loop):
        scheduled.append("hop")
        return real_run_coroutine_threadsafe(coro, loop)

    async def _run() -> None:
        runner = CommandRunner(Path.cwd())
        asyncio.run_coroutine_threadsafe = counting
        try:
            await runner.run(
                sys.executable,
                ["-c", "print('a'); print('b'); print('c')"],
                "chatty child",
                on_output=_service(False).log_sink("session-1", "scorer"),
            )
        finally:
            asyncio.run_coroutine_threadsafe = real_run_coroutine_threadsafe

    asyncio.run(_run())
    assert scheduled == []


def test_command_runner_still_delivers_lines_when_the_stream_is_live() -> None:
    """The other half of the contract: an enabled stream sees the output."""
    service = _service(True)

    async def _run() -> None:
        runner = CommandRunner(Path.cwd())
        await runner.run(
            sys.executable,
            ["-c", "print('hello from the scorer')"],
            "chatty child",
            on_output=service.log_sink("session-1", "scorer"),
        )

    asyncio.run(_run())
    messages = [payload["message"] for name, payload in service.get_state("session-1").history if name == "log"]
    assert any("hello from the scorer" in message for message in messages)
