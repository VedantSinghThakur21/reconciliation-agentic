"""Live Groq planner + AG-UI smoke (no Gemini)."""
from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
    line = line.strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    k, v = line.split("=", 1)
    os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

os.environ["CREWAI_TRACING_ENABLED"] = "false"

from ag_ui.core import RunAgentInput
from src.agui.agent import stream_workspace_agent
from src.agui.planner import plan_workspace, _call_gemini
from src.finance_controller.db import FinanceDB

DB = ROOT / "data" / "_groq_smoke.db"


async def collect(text: str, db: FinanceDB) -> list[dict]:
    inp = RunAgentInput.model_validate(
        {
            "threadId": f"t_{uuid.uuid4().hex[:8]}",
            "runId": f"r_{uuid.uuid4().hex[:8]}",
            "state": {},
            "messages": [{"id": "m1", "role": "user", "content": text}],
            "tools": [],
            "context": [],
            "forwardedProps": {},
        }
    )
    events: list[dict] = []
    async for frame in stream_workspace_agent(inp, db=db):
        if frame.startswith("data: "):
            events.append(json.loads(frame[6:].strip()))
    return events


def main() -> None:
    assert os.getenv("GROQ_API_KEY"), "GROQ_API_KEY missing"
    assert os.getenv("GROQ_MODEL") == "openai/gpt-oss-120b" or os.getenv("GROQ_MODEL")

    q = "Reconcile today's transactions and tell me what needs my attention."

    # Prove Gemini is not invoked when Groq succeeds
    with patch("src.agui.planner._call_gemini", wraps=_call_gemini) as gemini_spy:
        plan = plan_workspace(q)
        print("PLAN_SOURCE", plan.source)
        print("PRIMARY", plan.intent.primary)
        print("SECONDARY", plan.intent.secondary)
        print("RUN_RECON", plan.actions.run_reconciliation)
        print("COMPONENTS", plan.ui.components)
        print("REASONING", (plan.reasoning or "")[:200])
        assert plan.source == "groq", plan.source
        assert plan.actions.run_reconciliation or plan.intent.primary == "reconciliation"
        gemini_spy.assert_not_called()
        print("GEMINI_CALLS", gemini_spy.call_count)

    if DB.exists():
        DB.unlink()
    db = FinanceDB(DB)

    print("AGUI_STREAM…")
    with patch("src.agui.planner._call_gemini", wraps=_call_gemini) as gemini_spy:
        events = asyncio.run(collect(q, db))
        gemini_spy.assert_not_called()

    types = [e.get("type") for e in events]
    assert "RUN_STARTED" in types
    assert "STATE_SNAPSHOT" in types
    assert "RUN_FINISHED" in types or "RUN_ERROR" in types
    ws = None
    for e in events:
        if e.get("type") == "STATE_SNAPSHOT":
            snap = e.get("snapshot") or {}
            if snap.get("workspace"):
                ws = snap["workspace"]
    assert ws is not None
    intent = ws.get("intent")
    primary = intent.get("primary") if isinstance(intent, dict) else intent
    comps = [c.get("type") for c in ws.get("components") or []]
    print("WS_STATUS", ws.get("status"), "PRIMARY", primary, "COMPS", comps)
    print("STEPS", [e.get("stepName") for e in events if e.get("type") == "STEP_STARTED"][:12])
    assert ws.get("status") in {"ok", "unavailable", "clarification"}
    if ws.get("status") == "ok":
        assert comps
    print("SMOKE_OK")


if __name__ == "__main__":
    main()
