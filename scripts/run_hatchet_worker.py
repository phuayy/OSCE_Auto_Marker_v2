from __future__ import annotations

import sys
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
BACKEND_DIR = ROOT_DIR / "fastapi_backend"
sys.path.insert(0, str(BACKEND_DIR))

from app.queue.hatchet_worker import main  # noqa: E402


if __name__ == "__main__":
    main()
