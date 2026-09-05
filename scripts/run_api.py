#!/usr/bin/env python
"""Start the AI Finance Controller API (CrewAI Flow backend)."""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

import uvicorn

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
os.environ["PYTHONPATH"] = str(ROOT)
os.environ["CREWAI_TRACING_ENABLED"] = "false"
# Avoid Windows cp1252 crashes on CrewAI emoji logs.
os.environ.setdefault("PYTHONIOENCODING", "utf-8")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)


def main() -> None:
    port = int(os.environ.get("RECONQ_API_PORT", "8003"))
    uvicorn.run(
        "src.api.app:app",
        host="127.0.0.1",
        port=port,
        reload=True,
        reload_dirs=[str(ROOT / "src")],
    )


if __name__ == "__main__":
    main()
