from __future__ import annotations

import asyncio
import logging
import os
import contextlib
import subprocess
import threading
import time
from collections.abc import Awaitable, Callable
from concurrent.futures import Future
from dataclasses import dataclass
from pathlib import Path
from typing import Any


logger = logging.getLogger(__name__)

OutputCallback = Callable[[str, str], Awaitable[None] | None]

# Bound the captured stdout/stderr per stream so a chatty long-running process
# (e.g. WhisperX) cannot grow memory without limit. The most recent output is
# retained (the tail), where trailing JSON and error detail live. 5 MB is far
# larger than any scorer's JSON payload, so output parsing is unaffected.
_DEFAULT_MAX_CAPTURE_CHARS = 5_000_000

# Bound the list of in-flight output-callback futures; completed ones are pruned
# during the run instead of accumulating one-per-line until the process exits.
_DEFAULT_MAX_PENDING_CALLBACKS = 2_000

# Watchdog for a child that never exits. Deliberately generous — a CPU WhisperX
# run on a long recording legitimately takes hours — because its job is to end
# a *hung* process, not to bound a slow one. Without it a stuck child pins the
# job in "running" forever: no error, no retry, no recovery short of a restart.
_DEFAULT_TIMEOUT_SECONDS = 4 * 60 * 60

# Sentinel distinguishing "caller passed nothing" (use the default) from an
# explicit ``timeout_seconds=None`` (this command has no watchdog).
_UNSET_TIMEOUT: Any = object()


@dataclass(frozen=True)
class CommandResult:
    stdout: str
    stderr: str


class CommandRunner:
    def __init__(
        self,
        cwd: Path,
        *,
        max_capture_chars: int = _DEFAULT_MAX_CAPTURE_CHARS,
        max_pending_callbacks: int = _DEFAULT_MAX_PENDING_CALLBACKS,
        default_timeout_seconds: float | None = _DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self.cwd = cwd
        self.max_capture_chars = max(1, max_capture_chars)
        self.max_pending_callbacks = max(1, max_pending_callbacks)
        # None disables the watchdog entirely; anything <= 0 is read as None so
        # a misconfigured "0" cannot make every command time out instantly.
        self.default_timeout_seconds = (
            default_timeout_seconds if (default_timeout_seconds or 0) > 0 else None
        )

    async def run(
        self,
        command: str,
        args: list[str],
        label: str,
        *,
        env: dict[str, str] | None = None,
        on_output: OutputCallback | None = None,
        timeout_seconds: float | None = _UNSET_TIMEOUT,
    ) -> CommandResult:
        stdout_chunks: list[str] = []
        stderr_chunks: list[str] = []
        process_env = {**os.environ, **(env or {})}
        creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        loop = asyncio.get_running_loop()
        started_at = time.monotonic()
        logger.info("▶ %s — starting (%s)", label, os.path.basename(str(command)))
        callback_futures: list[Future[None]] = []
        callback_lock = threading.Lock()
        process_holder: dict[str, subprocess.Popen[bytes]] = {}

        def submit_output(stream_name: str, text: str) -> None:
            if on_output is None:
                return

            async def invoke_callback() -> None:
                result = on_output(stream_name, text)
                if asyncio.iscoroutine(result):
                    await result

            future = asyncio.run_coroutine_threadsafe(invoke_callback(), loop)
            with callback_lock:
                callback_futures.append(future)
                if len(callback_futures) > self.max_pending_callbacks:
                    # Prune already-delivered callbacks to bound memory; the
                    # remaining (in-flight) futures are still awaited at the end.
                    callback_futures[:] = [pending for pending in callback_futures if not pending.done()]

        def drain(stream, stream_name: str, chunks: list[str]) -> None:
            if stream is None:
                return
            # Per-stream local byte budget; each stream is drained by its own
            # thread with its own ``chunks`` list, so no locking is needed here.
            captured = 0
            while True:
                chunk = stream.readline()
                if not chunk:
                    break
                text = chunk.decode(errors="replace")
                chunks.append(text)
                captured += len(text)
                while captured > self.max_capture_chars and len(chunks) > 1:
                    captured -= len(chunks.pop(0))
                submit_output(stream_name, text)

        def run_blocking() -> int:
            try:
                proc = subprocess.Popen(
                    [command, *args],
                    cwd=str(self.cwd),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    env=process_env,
                    creationflags=creationflags,
                )
            except FileNotFoundError as error:
                windows_hint = (
                    " On Windows, restart VS Code/terminal after PATH changes or set the executable env var to an absolute path."
                    if os.name == "nt"
                    else ""
                )
                raise RuntimeError(f'{label} failed because "{command}" was not found in PATH.{windows_hint}') from error

            process_holder["process"] = proc
            drain_threads = [
                threading.Thread(target=drain, args=(proc.stdout, "stdout", stdout_chunks), daemon=True),
                threading.Thread(target=drain, args=(proc.stderr, "stderr", stderr_chunks), daemon=True),
            ]
            for thread in drain_threads:
                thread.start()
            exit_code = proc.wait()
            for thread in drain_threads:
                thread.join()
            return exit_code

        timeout = (
            self.default_timeout_seconds
            if timeout_seconds is _UNSET_TIMEOUT
            else (timeout_seconds if (timeout_seconds or 0) > 0 else None)
        )
        run_task = asyncio.create_task(asyncio.to_thread(run_blocking))
        try:
            exit_code = await (asyncio.wait_for(run_task, timeout) if timeout else run_task)
        except asyncio.TimeoutError:
            # A hung child is the failure mode with no other recovery: the job
            # would sit in "running" forever, holding a worker slot, with no
            # error to retry or report. Kill it and surface a real failure so
            # the queue's retry/fail path can take over.
            proc = process_holder.get("process")
            if proc is not None:
                await asyncio.to_thread(self._terminate_process, proc)
            # to_thread cannot be cancelled; terminating the child is what lets
            # the drain threads finish and the task complete.
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await run_task
            elapsed = time.monotonic() - started_at
            logger.error("✗ %s — timed out after %.1fs", label, elapsed)
            raise RuntimeError(
                f"{label} timed out after {timeout:.0f}s and was terminated. "
                "Raise SUBPROCESS_TIMEOUT_SECONDS if this workload legitimately runs longer."
            ) from None
        except asyncio.CancelledError:
            proc = process_holder.get("process")
            if proc is not None:
                await asyncio.to_thread(self._terminate_process, proc)
            run_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await run_task
            raise

        with callback_lock:
            pending_callbacks = list(callback_futures)
            callback_futures.clear()
        if pending_callbacks:
            await asyncio.gather(*(asyncio.wrap_future(future) for future in pending_callbacks))

        stdout = "".join(stdout_chunks).strip()
        stderr = "".join(stderr_chunks).strip()
        elapsed = time.monotonic() - started_at
        if exit_code == 0:
            logger.info("✓ %s — done in %.1fs", label, elapsed)
            return CommandResult(stdout=stdout, stderr=stderr)

        logger.error("✗ %s — failed after %.1fs (exit code %d)", label, elapsed, exit_code)
        detail = stderr or stdout or "No command output."
        raise RuntimeError(f"{label} failed with exit code {exit_code}.\n{detail}")

    @staticmethod
    def _terminate_process(proc: subprocess.Popen[bytes]) -> None:
        if proc.poll() is not None:
            return
        try:
            proc.terminate()
        except ProcessLookupError:
            return
        try:
            proc.wait(timeout=10)
            return
        except subprocess.TimeoutExpired:
            pass
        if proc.poll() is None:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            with contextlib.suppress(subprocess.TimeoutExpired):
                proc.wait(timeout=5)
