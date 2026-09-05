#!/usr/bin/env python3
"""Smoke-test AG-UI SSE: kickoff AMP (wait=false) then stream progress briefly."""
from __future__ import annotations

import json
import os
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
    line = line.strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    k, v = line.split("=", 1)
    os.environ.setdefault(k.strip(), v.strip())

from src.agents.crewai_client import CrewAIClient  # noqa: E402
from src.agui.stream import stream_kickoff_progress  # noqa: E402
import asyncio


async def main() -> None:
    client = CrewAIClient()
    kid = client.kickoff(
        {
            "user_request": "Run AR reconciliation on demo feeds (AG-UI stream smoke)",
            "ar_ap_mode": "AR",
            "bank_csv_path": "bank.csv",
            "payment_processor_csv_path": "payment_processor.csv",
            "ground_truth_csv_path": "ground_truth.csv",
        },
        meta={"source": "agui-smoke"},
    )
    print("kickoff_id", kid, flush=True)

    # Collect first ~12 SSE frames or until RUN_FINISHED (cap 45s for smoke)
    frames: list[str] = []
    async for frame in stream_kickoff_progress(kid, poll_interval_sec=3.0, max_wait_sec=45.0):
        frames.append(frame)
        line = frame.strip()
        if line.startswith("data: "):
            ev = json.loads(line[6:])
            print("EVENT", ev.get("type"), ev.get("delta") or ev.get("stepName") or ev.get("message") or "", flush=True)
        if len(frames) >= 12 or '"type": "RUN_FINISHED"' in frame or '"type": "RUN_ERROR"' in frame:
            if '"type": "RUN_FINISHED"' in frame or '"type": "RUN_ERROR"' in frame:
                break
            # keep going a bit if still early lifecycle only
            if len(frames) >= 8 and any("TEXT_MESSAGE_CONTENT" in f for f in frames):
                break

    out = ROOT / "data" / "uploads" / "_agui_smoke_frames.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    parsed = []
    for f in frames:
        if f.startswith("data: "):
            parsed.append(json.loads(f[6:].strip()))
    out.write_text(json.dumps(parsed, indent=2), encoding="utf-8")
    print("frames", len(parsed), "wrote", out, flush=True)
    types = [e.get("type") for e in parsed]
    print("types", types, flush=True)
    assert "RUN_STARTED" in types, types
    assert any(t == "TEXT_MESSAGE_CONTENT" for t in types), types
    print("SMOKE_OK", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
