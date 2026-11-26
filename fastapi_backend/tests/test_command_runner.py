from __future__ import annotations

import asyncio
import sys
import time

import pytest

from app.core.process import CommandRunner


def test_captured_output_is_bounded_to_tail(tmp_path) -> None:
    runner = CommandRunner(tmp_path, max_capture_chars=200)
    code = "import sys\n" "for i in range(2000):\n" "    print('line%04d' % i)\n"
    result = asyncio.run(runner.run(sys.executable, ["-c", code], "cap-test"))

    # The full output is ~18 KB; capture is bounded near the configured budget.
    assert len(result.stdout) < 2000
    # The tail (most recent output) is retained, the head is dropped.
    assert "line1999" in result.stdout
    assert "line0000" not in result.stdout


def test_all_output_callbacks_are_delivered(tmp_path) -> None:
    runner = CommandRunner(tmp_path, max_capture_chars=200, max_pending_callbacks=16)
    received: list[str] = []

    def on_output(_stream: str, text: str) -> None:
        received.append(text)

    code = "import sys\n" "for i in range(500):\n" "    print('row%03d' % i)\n"
    asyncio.run(runner.run(sys.executable, ["-c", code], "callback-test", on_output=on_output))

    # Pruning completed callbacks during the run must not drop delivery: every
    # printed line is delivered even though the futures list is capped at 16.
    joined = "".join(received)
    assert joined.count("row") == 500


def test_nonzero_exit_raises_with_detail(tmp_path) -> None:
    runner = CommandRunner(tmp_path)
    code = "import sys\n" "sys.stderr.write('boom\\n')\n" "sys.exit(3)\n"
    try:
        asyncio.run(runner.run(sys.executable, ["-c", code], "fail-test"))
    except RuntimeError as error:
        assert "exit code 3" in str(error)
        assert "boom" in str(error)
    else:  # pragma: no cover
        raise AssertionError("expected RuntimeError on non-zero exit")


def test_a_timed_out_command_also_kills_its_grandchild(tmp_path) -> None:
    """A watchdog kill must reach the whole process tree, not just the one PID
    ``Popen`` tracks. WhisperX and the scorer scripts routinely shell out to
    ffmpeg or a helper of their own; without a tree-kill that grandchild kept
    running orphaned after the parent was killed, still holding the GPU/CPU
    and racing the next run for the same output files.

    The grandchild is itself time-bounded (a few seconds) so a regression
    here never leaks a runaway process out of the test suite — it only makes
    this assertion fail.
    """

    async def scenario() -> None:
        heartbeat = tmp_path / "heartbeat.txt"
        heartbeat_repr = repr(str(heartbeat))
        child_code = (
            "import time\n"
            "deadline = time.time() + 8\n"
            "while time.time() < deadline:\n"
            f"    open({heartbeat_repr}, 'w').write(str(time.time()))\n"
            "    time.sleep(0.2)\n"
        )
        parent_code = "import subprocess, sys, time\n" f"subprocess.Popen([sys.executable, '-c', {child_code!r}])\n" "time.sleep(60)\n"

        runner = CommandRunner(tmp_path, default_timeout_seconds=0.5)
        with pytest.raises(RuntimeError):
            await runner.run(sys.executable, ["-c", parent_code], "Tree-kill test")

        # Let the grandchild actually start before we look for its heartbeat.
        for _ in range(50):
            if heartbeat.exists():
                break
            await asyncio.sleep(0.1)
        assert heartbeat.exists(), "grandchild never started"

        last_seen = heartbeat.read_text()
        await asyncio.sleep(1.0)
        assert heartbeat.read_text() == last_seen, "grandchild kept running after the parent was killed"

    asyncio.run(scenario())


# --- callbacks are observers: they never decide the run's outcome -----------

_THREE_LINES = "print('a'); print('b'); print('c')"


def test_a_callback_error_does_not_fail_a_successful_run(tmp_path, caplog) -> None:
    runner = CommandRunner(tmp_path)
    seen: list[str] = []

    def on_output(_stream: str, text: str) -> None:
        seen.append(text)
        if len(seen) == 1:
            raise RuntimeError("409: session is being updated")

    with caplog.at_level("WARNING", logger="app.core.process"):
        result = asyncio.run(runner.run(sys.executable, ["-c", _THREE_LINES], "observer", on_output=on_output))

    assert result.stdout.split() == ["a", "b", "c"]
    assert len(seen) == 3  # the run kept delivering after the error
    assert "session is being updated" in caplog.text


def test_an_async_callback_error_is_also_swallowed(tmp_path) -> None:
    runner = CommandRunner(tmp_path)

    async def on_output(_stream: str, _text: str) -> None:
        raise RuntimeError("write conflict")

    result = asyncio.run(runner.run(sys.executable, ["-c", _THREE_LINES], "observer", on_output=on_output))
    assert result.stdout.split() == ["a", "b", "c"]


def test_a_callback_failing_on_every_line_is_logged_a_bounded_number_of_times(tmp_path, caplog) -> None:
    runner = CommandRunner(tmp_path)

    def on_output(_stream: str, _text: str) -> None:
        raise RuntimeError("always broken")

    code = "for i in range(300): print(i)"
    with caplog.at_level("WARNING", logger="app.core.process"):
        result = asyncio.run(runner.run(sys.executable, ["-c", code], "observer", on_output=on_output))

    assert result.stdout.count("\n") == 299
    errors = [record for record in caplog.records if "always broken" in record.getMessage()]
    assert 1 <= len(errors) <= 5
    assert any("suppressed" in record.getMessage() for record in caplog.records)


def test_a_failing_subprocess_is_still_a_failure_when_its_callback_also_fails(tmp_path) -> None:
    runner = CommandRunner(tmp_path)

    def on_output(_stream: str, _text: str) -> None:
        raise RuntimeError("observer broke")

    code = "import sys; print('x'); sys.stderr.write('real failure\\n'); sys.exit(4)"
    with pytest.raises(RuntimeError, match="exit code 4") as raised:
        asyncio.run(runner.run(sys.executable, ["-c", code], "observer", on_output=on_output))
    assert "real failure" in str(raised.value)


def test_cancellation_still_stops_the_process_despite_callbacks(tmp_path) -> None:
    runner = CommandRunner(tmp_path)

    def on_output(_stream: str, _text: str) -> None:
        raise RuntimeError("noisy observer")

    code = "import time\nprint('up', flush=True)\ntime.sleep(60)\n"

    async def scenario() -> float:
        task = asyncio.create_task(runner.run(sys.executable, ["-c", code], "cancel", on_output=on_output))
        await asyncio.sleep(1.0)
        started = time.monotonic()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return time.monotonic() - started

    assert asyncio.run(scenario()) < 20
