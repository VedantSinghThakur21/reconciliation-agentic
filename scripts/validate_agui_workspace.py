"""Integration tests for AG-UI workspace agent (real flow + DB)."""
from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path

from ag_ui.core import RunAgentInput

from src.agui.agent import stream_workspace_agent
from src.finance_controller.db import FinanceDB

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "_agui_validate.db"


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


def last_workspace(events: list[dict]) -> dict | None:
    ws = None
    for e in events:
        if e.get("type") == "STATE_SNAPSHOT":
            snap = e.get("snapshot") or {}
            if "workspace" in snap:
                ws = snap["workspace"]
    return ws


def main() -> None:
    if DB.exists():
        DB.unlink()
    db = FinanceDB(DB)

    print("TEST1 reconcile…")
    ev1 = asyncio.run(
        collect(
            "Reconcile today's transactions and tell me what needs my attention.",
            db,
        )
    )
    types1 = [e.get("type") for e in ev1]
    steps = [e.get("stepName") for e in ev1 if e.get("type") == "STEP_STARTED"]
    ws1 = last_workspace(ev1)
    assert "RUN_STARTED" in types1
    assert "RUN_FINISHED" in types1 or "RUN_ERROR" in types1
    assert "STATE_SNAPSHOT" in types1
    assert ws1 is not None
    assert ws1.get("intent") == "reconciliation"
    assert ws1.get("status") == "ok", ws1
    assert ws1.get("run_id")
    comps = [c.get("type") for c in ws1.get("components") or []]
    assert "metric_grid" in comps or "kpi" in comps, comps
    print("  steps:", steps)
    print("  components:", comps)
    print("  run_id:", ws1.get("run_id"))
    print("TEST1 OK")

    print("TEST2 amount mismatches…")
    ev2 = asyncio.run(collect("Show me all amount mismatches.", db))
    ws2 = last_workspace(ev2)
    assert ws2 and ws2.get("intent") == "amount_mismatch"
    assert ws2.get("status") == "ok", ws2
    assert any(c.get("type") == "transaction_table" for c in ws2.get("components") or [])
    print("TEST2 OK", [c.get("type") for c in ws2["components"]])

    print("TEST3 TXN_0047…")
    ev3 = asyncio.run(collect("Why did TXN_0047 fail?", db))
    ws3 = last_workspace(ev3)
    assert ws3 and ws3.get("intent") == "transaction_investigation"
    # Real DB has no TXN-0047 → unavailable (not fabricated)
    assert ws3.get("status") == "unavailable", ws3
    print("TEST3 OK unavailable as expected")

    print("TEST4 attention…")
    ev4 = asyncio.run(collect("Show me what needs my attention.", db))
    ws4 = last_workspace(ev4)
    assert ws4 and ws4.get("intent") == "exception_review"
    assert ws4.get("status") == "ok", ws4
    assert any(c.get("type") == "exception_table" for c in ws4.get("components") or [])
    rows = []
    for c in ws4["components"]:
        if c.get("type") == "exception_table":
            rows = c["data"].get("rows") or []
    assert all(r.get("human_decision") is None for r in rows if r.get("status") == "pending" or True)
    # pending rows should have null human_decision
    pending = [r for r in rows if r.get("human_decision") is None]
    print("  pending with null human_decision:", len(pending))
    print("TEST4 OK")

    print("TEST5 human approve…")
    if not pending:
        print("TEST5 SKIP — no pending exceptions in this run")
    else:
        exc_id = pending[0]["id"]
        before = len([e for e in db.fetch_audit(200) if e.get("event_type") == "exception_resolved"])
        from src.finance_controller.utils import new_id

        db.insert_reviews(
            [
                {
                    "id": new_id("REV"),
                    "transaction_id": pending[0]["transaction_id"],
                    "decision": "APPROVED",
                    "reason": "TEST5 human approve",
                    "confidence": pending[0].get("confidence") or 0,
                }
            ]
        )
        db.audit(
            "exception_resolved",
            f"{exc_id} → APPROVED",
            entity_id=exc_id,
            payload={"decision": "APPROVED"},
        )
        after = len([e for e in db.fetch_audit(200) if e.get("event_type") == "exception_resolved"])
        assert after == before + 1
        reviews = [r for r in db.fetch_reviews() if r["transaction_id"] == pending[0]["transaction_id"]]
        assert len(reviews) == 1
        assert reviews[0]["decision"] == "APPROVED"
        print("TEST5 OK single APPROVED + audit")

    print("ALL_OK")


if __name__ == "__main__":
    main()
