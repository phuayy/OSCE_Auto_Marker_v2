from __future__ import annotations

import asyncio
import sys

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
