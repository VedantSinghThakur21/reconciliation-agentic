"""Local flow fallback when AMP dashboard payload is hollow."""

from __future__ import annotations

from pathlib import Path

from src.finance_controller.amp_sync import (
    amp_run_is_hollow,
    finalize_amp_run,
    run_local_fallback_after_amp,
)
from src.finance_controller.db import FinanceDB


HOLLOW_FLOW = {
    "run_id": "RUN-hollow",
    "ar_ap_mode": "AR",
    "status": "completed",
    "sources": [{"source": "erp", "record_count": 20, "total_amount": 0, "currency": "N/A"}],
    "candidates": [],
    "results": [],
    "exceptions": [],
    "pending_review_items": [],
    "evaluation": {
        "accuracy": "N/A",
        "match_rate": "N/A",
        "f1_score": "N/A",
        "pending_human_count": 15,
    },
    "persistence": {
        "total_results": 0,
        "results_by_status": {"AUTO_MATCHED": 0},
        "total_reconciled_amount": 0,
    },
    "journals": [],
    "final_report": "Pending Human Count: 15",
}


def test_hollow_amp_detected(tmp_path: Path):
    db = FinanceDB(tmp_path / "hollow.db")
    finalize_amp_run(
        db,
        run_id="AMP-HOLLOW1",
        kickoff_id="kick-hollow",
        amp_status={"state": "SUCCESS", "result": HOLLOW_FLOW},
    )
    assert amp_run_is_hollow(db, "AMP-HOLLOW1") is True


def test_local_fallback_fills_dashboard(tmp_path: Path):
    db = FinanceDB(tmp_path / "fallback.db")
    finalize_amp_run(
        db,
        run_id="AMP-HOLLOW2",
        kickoff_id="kick-hollow-2",
        amp_status={"state": "SUCCESS", "result": HOLLOW_FLOW},
    )
    assert amp_run_is_hollow(db, "AMP-HOLLOW2")

    local = run_local_fallback_after_amp(
        db,
        user_request="Reconcile demo feeds",
        amp_run_id="AMP-HOLLOW2",
        kickoff_id="kick-hollow-2",
    )
    rid = local["run_id"]
    assert rid.startswith("FLOW-")
    assert amp_run_is_hollow(db, rid) is False
    sources = db.fetch_sources(rid)
    assert sum(1 for s in sources if s["source"] == "erp") >= 20
    assert sum(1 for s in sources if s["source"] in ("bank", "payment_processor")) >= 4
    metrics = db.get_metrics(rid) or {}
    assert metrics.get("match_rate") is not None
    summary = (db.get_run(rid) or {}).get("summary") or {}
    assert summary.get("amp_local_fallback") is True
    assert summary.get("fallback_from_amp") == "AMP-HOLLOW2"
