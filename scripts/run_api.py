#!/usr/bin/env python
"""Start the AI Finance Controller API (CrewAI Flow backend)."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import uvicorn

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
os.environ["PYTHONPATH"] = str(ROOT)
os.environ["CREWAI_TRACING_ENABLED"] = "false"


def main() -> None:
    uvicorn.run(
        "src.api.app:app",
        host="127.0.0.1",
        port=8002,
        reload=True,
        reload_dirs=[str(ROOT / "src")],
    )


if __name__ == "__main__":
    main()
