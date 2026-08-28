from __future__ import annotations

import argparse
import os
import socket
import sys
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
BACKEND_DIR = ROOT_DIR / "fastapi_backend"
sys.path.insert(0, str(BACKEND_DIR))

from app.core.asyncio_compat import configure_windows_selector_event_loop_policy  # noqa: E402


configure_windows_selector_event_loop_policy()


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

    if not args.allow_port_conflict and port_already_serving(args.host, args.port):
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

    import uvicorn

    uvicorn.run(
        "app.main:app",
        app_dir=str(BACKEND_DIR),
        host=args.host,
        port=args.port,
        reload=args.reload,
        # On Windows, uvicorn's default loop setup ("auto") forcibly installs the
        # WindowsProactorEventLoopPolicy, which async psycopg cannot use. In the
        # non-reload path the server runs in THIS process, so we set loop="none"
        # to keep the WindowsSelectorEventLoopPolicy configured above; uvicorn's
        # asyncio.run() then uses the Selector loop. The reload path runs the
        # server in a subprocess where "auto" is fine, so leave it untouched.
        loop="auto" if args.reload else "none",
        # Prevent connection drops between sequential part-upload requests;
        # 5 s default is too short when disk I/O is heavy.
        timeout_keep_alive=120,
        # Force-close lingering connections after 10 s during shutdown/reload.
        # Without this, the long-lived SSE streams (/api/sessions/{id}/events)
        # never drain on their own, so a --reload restart blocks forever waiting
        # for them — leaving the worker holding the port while it stops serving
        # requests, which manifests as the frontend hanging on "Loading...".
        timeout_graceful_shutdown=10,
    )


if __name__ == "__main__":
    main()
