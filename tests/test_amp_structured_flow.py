"""CrewAI structured FlowOutput → local dashboard persistence."""

from __future__ import annotations

from pathlib import Path

from src.finance_controller.amp_sync import (
    extract_flow_output,
    finalize_amp_run,
    metrics_from_flow_output,
    pending_records_from_flow_output,
)
from src.finance_controller.db import FinanceDB
from src.finance_controller.review_state import pending_review_items

FILLED_FLOW = {
    "run_id": "RUN-20260905-demo",
    "ar_ap_mode": "AR",
    "status": "completed",
    "stages": [
        {"name": "ingest_and_validate", "status": "completed", "output_summary": {"total_records": 5}},
        {"name": "summary_report", "status": "completed", "output_summary": {}},
    ],
    "sources": [
        {"source": "erp", "record_count": 20, "total_amount": 0, "currency": "N/A"},
        {"source": "bank", "record_count": 3, "total_amount": 0, "currency": "N/A"},
    ],
    "candidates": [
        {
            "candidate_id": "CAND-001",
            "source_id": "ERP-1001",
            "bank_or_pp_id": "BNK-2001",
            "source_type": "bank",
            "strategy": "fuzzy",
            "confidence_score": 0.91,
            "amount_score": 1.0,
            "merchant_score": 0.85,
            "date_score": 0.9,
            "reference_score": 0.7,
            "source_amount": 5000,
            "matched_amount": 5000,
        }
    ],
    "candidates_count": 1,
    "results": [
        {
            "result_id": "RES-1",
            "erp_id": "ERP-1001",
            "matched_id": "BNK-2001",
            "final_status": "AUTO_MATCHED",
            "confidence_score": 0.97,
            "reconciled_amount": 5000,
            "strategy": "exact",
        },
        {
            "result_id": "RES-2",
            "erp_id": "ERP-1002",
            "matched_id": "BNK-2002",
            "final_status": "PENDING_HUMAN",
            "confidence_score": 0.82,
            "reconciled_amount": 0,
            "outstanding_balance": 1200,
            "decision_notes": "Partial settlement",
        },
    ],
    "results_count": 2,
    "exceptions": [
        {
            "exception_id": "EXC-9",
            "erp_id": "ERP-1003",
            "matched_id": "",
            "exception_type": "MISSING_BANK_RECORD",
            "reason": "No bank credit found",
            "recommended_action": "needs_human_review",
            "confidence_score": 0.4,
            "outstanding_balance": 800,
        }
    ],
    "exceptions_count": 1,
    "pending_review_items": [
        {
            "item_id": "ERP-1002",
            "item_type": "candidate",
            "amount": 1200,
            "merchant": "Acme",
            "date": "2026-09-01",
            "confidence_score": 0.82,
            "exception_type": "PARTIAL_SETTLEMENT",
            "reason": "Partial settlement",
            "recommended_action": "needs_human_review",
        },
        {
            "item_id": "ERP-1003",
            "item_type": "exception",
            "amount": 800,
            "merchant": "Beta",
            "date": "2026-09-02",
            "confidence_score": 0.4,
            "exception_type": "MISSING_BANK_RECORD",
            "reason": "No bank credit found",
            "recommended_action": "needs_human_review",
        },
    ],
    "pending_review_items_count": 2,
    "evaluation": {
        "accuracy": 0.88,
        "precision": 0.9,
        "recall": 0.85,
        "f1_score": 0.87,
        "match_rate": 0.5,
        "false_match_rate": 0.05,
        "true_positives": 1,
        "false_positives": 0,
        "false_negatives": 1,
        "true_negatives": 0,
        "pending_human_count": 2,
        "metrics_note": "",
    },
    "persistence": {
        "total_results": 2,
        "results_by_status": {"AUTO_MATCHED": 1, "PENDING_HUMAN": 1},
        "total_reconciled_amount": 5000,
        "total_unmatched_amount": 2000,
        "total_pending_human_amount": 2000,
        "cross_check_passed": True,
    },
    "journals": [
        {
            "entry_id": "JE-1",
            "erp_id": "ERP-1001",
            "journal_type": "AR",
            "debit_account": "1000",
            "credit_account": "1200",
            "amount": 5000,
            "currency": "INR",
            "description": "Auto match",
            "status": "POSTED",
        }
    ],
    "journals_count": 1,
    "human_decisions": [],
    "human_decisions_count": 0,
    "audit_trail": [
        {
            "log_id": "A1",
            "event_type": "AUTO_MATCH",
            "entity_id": "ERP-1001",
            "old_status": "",
            "new_status": "AUTO_MATCHED",
            "actor": "SYSTEM_AUTO_MATCH",
            "notes": "exact",
        }
    ],
    "audit_trail_count": 1,
    "data_integrity_warnings": [],
    "final_report": "# OK\n- Pending Human Count: 2\n",
}

EMPTY_PENDING_FLOW = {
    "run_id": "RUN-empty-pending",
    "ar_ap_mode": "AR",
    "status": "completed",
    "sources": [{"source": "erp", "record_count": 20, "total_amount": 0, "currency": "N/A"}],
    "candidates": [],
    "results": [],
    "exceptions": [],
    "pending_review_items": [],
    "pending_review_items_count": 0,
    "evaluation": {
        "accuracy": "N/A",
        "match_rate": "N/A",
        "f1_score": "N/A",
        "pending_human_count": 15,
        "metrics_note": "N/A",
    },
    "persistence": {
        "total_results": 0,
        "results_by_status": {"AUTO_MATCHED": 0, "PENDING_HUMAN": 0},
        "total_reconciled_amount": 0,
        "total_unmatched_amount": 0,
    },
    "journals": [],
    "human_decisions": [],
    "audit_trail": [],
    "data_integrity_warnings": [],
    "final_report": "Pending Human Count: 15\nPending item IDs: []",
}


def test_extract_flow_output_from_result_dict():
    amp = {"state": "SUCCESS", "result": FILLED_FLOW}
    flow = extract_flow_output(amp)
    assert flow is not None
    assert flow["run_id"] == "RUN-20260905-demo"
    metrics = metrics_from_flow_output(flow)
    assert metrics["match_rate"] == 0.5
    assert metrics["f1"] == 0.87
    assert metrics["auto_reconciled"] == 1
    pending = pending_records_from_flow_output(flow)
    assert {p["transaction_id"] for p in pending} == {"ERP-1002", "ERP-1003"}


def test_finalize_structured_flow_populates_dashboard(tmp_path: Path):
    db = FinanceDB(tmp_path / "flow.db")
    out = finalize_amp_run(
        db,
        run_id="AMP-STRUCT1",
        kickoff_id="kick-struct",
        amp_status={"state": "SUCCESS", "result": FILLED_FLOW},
    )
    assert out["structured_flow"] is True
    assert out["metrics"]["match_rate"] == 0.5
    assert out["metrics"]["accuracy"] == 0.88
    assert len(pending_review_items(db, "AMP-STRUCT1")) == 2
    assert len(db.fetch_candidates("AMP-STRUCT1")) == 1
    assert len(db.fetch_results("AMP-STRUCT1")) == 2
    assert len(db.fetch_journals("AMP-STRUCT1")) == 1
    # Aggregate sources only → demo hydrate still fills invoices/payments
    sources = db.fetch_sources("AMP-STRUCT1")
    assert sum(1 for s in sources if s["source"] == "erp") >= 20
    run = db.get_run("AMP-STRUCT1")
    assert (run["summary"] or {}).get("persistence", {}).get("total_reconciled_amount") == 5000


def test_fenced_json_flow_output_is_detected():
    fenced = """```json
{
  "run_id": "RUN-fence",
  "ar_ap_mode": "AR",
  "status": "completed",
  "pending_review_items": [{"item_id": "ERP-1", "item_type": "exception", "amount": 10, "recommended_action": "needs_human_review"}],
  "pending_review_items_count": 1,
  "evaluation": {"match_rate": 0.2, "pending_human_count": 1},
  "persistence": {"total_reconciled_amount": 0, "results_by_status": {"AUTO_MATCHED": 0}},
  "results": [],
  "exceptions": [],
  "candidates": [],
  "sources": [{"source": "erp", "record_count": 2, "total_amount": 0, "currency": "INR"}],
  "final_report": "ok"
}
```"""
    flow = extract_flow_output({"state": "SUCCESS", "result": fenced})
    assert flow is not None
    assert flow["run_id"] == "RUN-fence"
    assert len(pending_records_from_flow_output(flow)) == 1


def test_empty_pending_array_does_not_invent_items(tmp_path: Path):
    db = FinanceDB(tmp_path / "empty.db")
    out = finalize_amp_run(
        db,
        run_id="AMP-EMPTY",
        kickoff_id="kick-empty",
        amp_status={"state": "SUCCESS", "result": EMPTY_PENDING_FLOW},
    )
    assert out["metrics"]["pending_review"] == 0
    assert len(pending_review_items(db, "AMP-EMPTY")) == 0
    warnings = out.get("data_integrity_warnings") or []
    assert any("pending_human_count=15" in w for w in warnings)
