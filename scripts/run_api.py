from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
BACKEND_DIR = ROOT_DIR / "fastapi_backend"
sys.path.insert(0, str(BACKEND_DIR))

from app.core.asyncio_compat import configure_windows_selector_event_loop_policy  # noqa: E402


configure_windows_selector_event_loop_policy()


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the OSCE AI Marker FastAPI server.")
    parser.add_argument("--host", default=os.getenv("API_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("API_PORT", "8787")))
    parser.add_argument("--reload", action="store_true")
    args = parser.parse_args()

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
