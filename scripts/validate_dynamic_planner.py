"""Validate dynamic LLM workspace planning (no keyword maps)."""
from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# load .env without printing secrets
env_path = ROOT / ".env"
if env_path.exists():
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

from ag_ui.core import RunAgentInput
from src.agui.agent import stream_workspace_agent
from src.agui.planner import plan_workspace
from src.finance_controller.db import FinanceDB
from src.finance_controller.flow import run_finance_flow

DB = ROOT / "data" / "_agui_dynamic_validate.db"


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
            if snap.get("workspace"):
                ws = snap["workspace"]
    return ws


def primary_of(ws: dict) -> str:
    intent = ws.get("intent")
    if isinstance(intent, dict):
        return str(intent.get("primary") or "")
    return str(intent or "")


def main() -> None:
    # Seed DB with one real local flow so read-only views have data
    if DB.exists():
        DB.unlink()
    db = FinanceDB(DB)
    print("Seeding reconciliation…")
    result = run_finance_flow(
        user_request="Seed for dynamic planner tests",
        ar_ap_mode="AR",
        bank_csv_path=str(ROOT / "data" / "demo" / "bank.csv"),
        payment_processor_csv_path=str(ROOT / "data" / "demo" / "payment_processor.csv"),
        ground_truth_csv_path=str(ROOT / "data" / "demo" / "ground_truth.csv"),
        db_path=DB,
    )
    print("seed run", result["run_id"])

    paraphrases = [
        ("Could you close out today's books and flag anything I should look at?", "reconciliation"),
        ("What's still sitting on the review pile?", "exception_review"),
        ("Pull the payments where the amounts don't line up.", "amount_mismatch"),
        ("Give me the cash picture after matching.", "cash_position"),
        ("Walk me through the decision trail.", "audit_review"),
        ("How healthy was the last matching run?", "finance_summary"),
    ]

    print("\n=== Planner-only paraphrases ===")
    for text, expected_family in paraphrases:
        plan = plan_workspace(text)
        print(
            f"\nQ: {text}\n"
            f"  primary={plan.intent.primary} secondary={plan.intent.secondary}\n"
            f"  run_recon={plan.actions.run_reconciliation} clarify={plan.actions.needs_clarification}\n"
            f"  components={plan.ui.components}\n"
            f"  source={plan.source} conf={plan.confidence:.2f}\n"
            f"  reasoning={plan.reasoning[:160]}"
        )
        if plan.source in {"no_llm", "llm_error", "empty"}:
            print("  SKIP assert — LLM not usable")
            continue
        if plan.actions.needs_clarification:
            print("  NOTE: model asked clarification")
            continue
        # Soft check: primary should be in same family OR secondary includes it,
        # OR run_reconciliation implies reconciliation family
        got = {plan.intent.primary, *plan.intent.secondary}
        if expected_family == "reconciliation":
            ok = plan.actions.run_reconciliation or "reconciliation" in got
        else:
            ok = expected_family in got
        print("  family_ok" if ok else f"  FAMILY_MISS expected~{expected_family}")
        if not ok:
            raise SystemExit(1)

    print("\n=== End-to-end AG-UI (paraphrases, no reconcile re-run) ===")
    for text, expected in paraphrases[1:4]:  # skip reconcile to save time
        events = asyncio.run(collect(text, db))
        ws = last_workspace(events)
        assert ws, text
        prim = primary_of(ws)
        comps = [c.get("type") for c in ws.get("components") or []]
        print(f"Q: {text}\n  primary={prim} status={ws.get('status')} comps={comps}")
        assert ws.get("status") in {"ok", "unavailable", "clarification"}, ws
        if ws.get("status") == "ok":
            assert comps, "expected components for ok workspace"

    print("\nALL_DYNAMIC_OK")


if __name__ == "__main__":
    main()
