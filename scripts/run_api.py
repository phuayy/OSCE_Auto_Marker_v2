from __future__ import annotations

import argparse
import logging
import os
import socket
import sys
import time
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
BACKEND_DIR = ROOT_DIR / "fastapi_backend"
sys.path.insert(0, str(BACKEND_DIR))

from app.core.asyncio_compat import configure_windows_selector_event_loop_policy  # noqa: E402


configure_windows_selector_event_loop_policy()

# Directories whose Python files should trigger a reload. Scoped deliberately:
# watching the repository root (uvicorn's default) means walking .venv,
# node_modules and storage/ on every scan, and a "uv sync" or a pipeline
# artifact write then restarts the server for no reason.
RELOAD_DIRS = (BACKEND_DIR / "app", ROOT_DIR / "scripts")

# How long to keep re-probing a busy port before refusing to start. A restart
# hands the port over from the outgoing process, so a brief overlap is normal;
# anything still serving after this is a genuinely separate instance.
PORT_WAIT_SECONDS = 5.0


def port_already_serving(host: str, port: int) -> bool:
    """True when something already accepts connections on host:port.

    A bind test is useless here: uvicorn sets SO_REUSEADDR, which on Windows lets a
    second server bind an address another process is already listening on. Both then
    appear in netstat and requests are split between them at random. Connecting is
    unambiguous - if the handshake completes, a server owns the port.
    """
    probe_host = "127.0.0.1" if host in {"0.0.0.0", ""} else host
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(1.0)
        try:
            return probe.connect_ex((probe_host, port)) == 0
        except OSError:
            return False


def wait_for_free_port(host: str, port: int, timeout: float = PORT_WAIT_SECONDS) -> bool:
    """True once nothing serves host:port, False if it is still busy at ``timeout``."""
    deadline = time.monotonic() + timeout
    while True:
        if not port_already_serving(host, port):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.25)


def serve(host: str, port: int) -> None:
    """Run the server in this process. Also the reload worker's entry point."""
    # Under the reloader this runs in a spawned child, where the module-level
    # configure_windows_selector_event_loop_policy() above has already run on
    # re-import - so the Selector policy is in place here too.
    configure_windows_selector_event_loop_policy()

    import uvicorn

    uvicorn.run(
        "app.main:app",
        app_dir=str(BACKEND_DIR),
        host=host,
        port=port,
        # Reloading is handled by watchfiles in the parent process (see
        # run_with_reload); uvicorn always runs as a plain single server here.
        reload=False,
        # On Windows, uvicorn's default loop setup ("auto") forcibly installs the
        # WindowsProactorEventLoopPolicy, which async psycopg cannot use. loop="none"
        # keeps the WindowsSelectorEventLoopPolicy configured above; uvicorn's
        # asyncio.run() then uses the Selector loop.
        loop="none",
        # Prevent connection drops between sequential part-upload requests;
        # 5 s default is too short when disk I/O is heavy.
        timeout_keep_alive=120,
        # Force-close lingering connections after 10 s during shutdown. Without
        # this, the long-lived SSE streams (/api/sessions/{id}/events) never drain
        # on their own, so a restart blocks waiting for them - leaving the worker
        # holding the port while it stops serving requests, which manifests as the
        # frontend hanging on "Loading...".
        timeout_graceful_shutdown=10,
    )


def run_with_reload(host: str, port: int) -> None:
    """Restart ``serve`` in a child process whenever a watched .py file changes.

    Deliberately NOT uvicorn's own --reload. On Windows uvicorn restarts its
    worker with os.kill(pid, CTRL_C_EVENT), and a console control event cannot be
    addressed to one process: Windows delivers it to every process attached to the
    console. Under "npm run dev" that is also node (scripts/dev.mjs), vite and npm,
    which see a Ctrl+C they never asked for and tear the whole dev stack down - the
    server appearing to shut itself down on save. watchfiles instead stops the
    child with os.kill(pid, SIGINT), which on Windows is a TerminateProcess of that
    single process, so nothing else on the console is disturbed.
    """
    from watchfiles import run_process
    from watchfiles.filters import PythonFilter

    logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s:%(message)s")
    logger = logging.getLogger("run_api.reload")
    watched = [str(path) for path in RELOAD_DIRS if path.exists()]
    logger.info("Watching for changes in %s", ", ".join(watched))

    def on_reload(changes) -> None:
        files = sorted({os.path.relpath(path, ROOT_DIR) for _change, path in changes})
        logger.warning("Detected changes in %s. Reloading...", ", ".join(files))

    run_process(
        *watched,
        target=serve,
        args=(host, port),
        watch_filter=PythonFilter(),
        callback=on_reload,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the OSCE AI Marker FastAPI server.")
    parser.add_argument("--host", default=os.getenv("API_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("API_PORT", "8787")))
    parser.add_argument("--reload", action="store_true")
    parser.add_argument(
        "--allow-port-conflict",
        action="store_true",
        help="Start even when the port is already served (not recommended).",
    )
    args = parser.parse_args()

    if not args.allow_port_conflict and not wait_for_free_port(args.host, args.port):
        message = "\n".join(
            [
                f"Refusing to start: {args.host}:{args.port} is already serving.",
                "Another API instance is running (npm run dev, npm run dev:api, or a",
                "stray process). Two instances on one port split requests unpredictably.",
                f"Find it with:  netstat -ano | findstr :{args.port}",
                "Stop that one first, or pass --allow-port-conflict to override.",
            ]
        )
        print(message, file=sys.stderr)
        raise SystemExit(1)

    if args.reload:
        run_with_reload(args.host, args.port)
    else:
        serve(args.host, args.port)


if __name__ == "__main__":
    main()
