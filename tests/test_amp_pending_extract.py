"""AMP Enterprise report → local pending records."""

from __future__ import annotations

from pathlib import Path

from src.finance_controller.amp_sync import (
    extract_pending_records_from_amp,
    extract_report_text,
    finalize_amp_run,
    parse_metrics_from_report,
)
from src.finance_controller.db import FinanceDB
from src.finance_controller.review_state import pending_review_items
from src.agui.builders import build_reconciliation_result

ENTERPRISE_REPORT = """# AI Finance Controller Final Report

## (1) Executive Summary
- **Process Mode:** Accounts Receivable
- **Decisions Applied:** **0**
- **Pending Human Count:** 4
- **Note:** **NO HUMAN DECISIONS HAVE BEEN APPLIED YET. All items in the review band and exception band are awaiting human action.**

## (4) ReconQ Confidence Band Results
| Band                          | Threshold | Count | Action        |
|-------------------------------|-----------|-------|---------------|
| Auto-Matched                  | 1.0       | 2     | Confirmed     |
| Human Review Required          | <0.80     | 1     | Review Needed |
| Pending Human Decisions        | N/A       | 4     | Awaiting Action |

## (5) Human Decisions Status
| Decision Type                     | Count |
|-----------------------------------|-------|
| APPROVED                          | 0     |
| REJECTED                          | 0     |
| RESOLVED                          | 0     |
| WRITE_OFF_AUTHORIZED              | 0     |
| ESCALATED                         | 0     |
| PENDING (awaiting action)         | 4     |

## (6) AI Recommendations vs Human Decisions
### AWAITING HUMAN DECISION (pending_human_count items)
- **Candidates Pending Review:**
  - PAYPAL (123456): Partial match found with a significant difference in amounts, requiring further scrutiny.
  - PAYPAL (partial): Partial match found with some unmatched amounts, requiring further review.

- **Pending Exceptions:**
  - Exception ID 3: No corresponding match candidate found for this record.
  - Exception ID 4: No corresponding match candidate found for this record.

## (10) Exception Analysis
| Type                    | Count | AI Recommended Action     |
|-------------------------|-------|---------------------------|
| PARTIAL_SETTLEMENT      | 2     | Needs Human Review        |
| MISSING_ERP_RECORD      | 2     | Needs Human Review        |

Human Review: ```json
{
  "decisions_applied": 0,
  "hr_table_row_count": 0,
  "pending_review_items": [
    {
      "candidate_id": "PAYPAL (123456)",
      "confidence_score": 0.884,
      "exception_type": "PARTIAL_SETTLEMENT",
      "reason": "Partial match found with a significant difference in amounts, requiring further scrutiny."
    },
    {
      "candidate_id": "PAYPAL (partial)",
      "confidence_score": 0.814,
      "exception_type": "PARTIAL_SETTLEMENT",
      "reason": "Partial match found with some unmatched amounts, requiring further review."
    }
  ],
  "pending_exception_items": [
    {
      "exception_id": "3",
      "exception_type": "MISSING_ERP_RECORD",
      "reason": "No corresponding match candidate found for this record."
    },
    {
      "exception_id": "4",
      "exception_type": "MISSING_ERP_RECORD",
      "reason": "No corresponding match candidate found for this record."
    }
  ]
}
```
"""


def test_extract_pending_from_enterprise_report_shape():
    amp = {"result": {"raw": ENTERPRISE_REPORT}, "state": "SUCCESS"}
    text = extract_report_text(amp)
    recs = extract_pending_records_from_amp(amp, text)
    metrics = parse_metrics_from_report(text)

    ids = {r["transaction_id"] for r in recs}
    assert len(recs) == 4
    assert "PAYPAL (123456)" in ids
    assert "PAYPAL (partial)" in ids
    assert "EXC-3" in ids
    assert "EXC-4" in ids
    assert metrics.get("pending_review_reported") == 4
    assert metrics.get("auto_reconciled") == 2
    assert metrics.get("exception_count") == 4

    paypal = next(r for r in recs if r["transaction_id"] == "PAYPAL (123456)")
    assert paypal["exception_type"] == "PARTIAL_SETTLEMENT"
    assert abs(float(paypal["confidence"]) - 0.884) < 0.001
    assert paypal["recommended_action"] == "REVIEW"


def test_finalize_amp_materializes_pending_for_agui(tmp_path: Path):
    db = FinanceDB(tmp_path / "amp.db")
    amp = {"result": {"raw": ENTERPRISE_REPORT}, "state": "SUCCESS"}
    out = finalize_amp_run(
        db,
        run_id="AMP-TESTPEND",
        kickoff_id="kick-test-pending",
        amp_status=amp,
        user_request="reconcile",
    )
    assert out["run_id"] == "AMP-TESTPEND"
    items = pending_review_items(db, "AMP-TESTPEND")
    assert len(items) == 4
    assert {i["transaction_id"] for i in items} == {
        "PAYPAL (123456)",
        "PAYPAL (partial)",
        "EXC-3",
        "EXC-4",
    }
    assert all(i["human_decision"] is None for i in items)
    assert all(i["status"] == "HUMAN_REVIEW" for i in items)

    ui = build_reconciliation_result(db, "AMP-TESTPEND")
    state = ui.as_state()
    assert state["pending_human_count"] == 4
    assert len(state["pending_review_items"]) == 4
    table = next(c for c in ui.components if c.type == "exception_table")
    assert table.data.get("title") == "Needs Your Attention"
    assert len(table.data.get("rows") or []) == 4

    # Metrics must reflect identified records, not a floating count
    assert (out.get("metrics") or {}).get("pending_review") == 4
    assert (out.get("metrics") or {}).get("auto_reconciled") == 2
    # Demo hydration restores invoice/payment workspace counts
    sources = db.fetch_sources("AMP-TESTPEND")
    assert sum(1 for s in sources if s["source"] == "erp") >= 20
    assert sum(1 for s in sources if s["source"] in ("bank", "payment_processor")) >= 4
    assert (out.get("metrics") or {}).get("match_rate") is not None


MULTILINE_EXCEPTION_REPORT = """# AI Finance Controller Report

## (1) Executive Summary
- **Pending Human Count:** 3

## (3) Data Ingestion
| Source                | Records Loaded |
|-----------------------|----------------|
| ERP                   | 20             |
| Bank                  | 3              |
| Payment Processor      | 3              |

## (4) ReconQ Confidence Band Results
| Band                   | Threshold  | Count | Action        |
|------------------------|------------|-------|---------------|
| Exception (0 - 0.79)   | 0 - 0.79   | 0     | Needs Review  |
| Human Review (0.80 - 0.94) | 0.80 - 0.94 | 2     | Review Needed |
| Auto-Match (0.95 - 1.0)   | 0.95 - 1.0 | 3     | No Action Needed |

## (6) AI Recommendations vs Human Decisions
### AWAITING HUMAN DECISION (pending_human_count items)
- Exception ID: E001
- ERP ID: P002
- Matched ID: B002
- Exception Type: MISSING_BANK_RECORD
- Exception ID: E002
- ERP ID: I002
- Matched ID: B003
- Exception Type: PARTIAL_SETTLEMENT
- Exception ID: E003
- ERP ID: I002
- Matched ID: B003
- Exception Type: PARTIAL_SETTLEMENT

## (12) Evaluation Metrics
| Metric              | Value |
|---------------------|-------|
| Accuracy            | Not calculated |
| Match Rate          | Not calculated |
"""


def test_multiline_exception_blocks_and_derived_match_rate():
    amp = {"result": {"raw": MULTILINE_EXCEPTION_REPORT}, "state": "SUCCESS"}
    text = extract_report_text(amp)
    recs = extract_pending_records_from_amp(amp, text)
    metrics = parse_metrics_from_report(text)

    ids = {r["transaction_id"] for r in recs}
    assert ids == {"EXC-E001", "EXC-E002", "EXC-E003"}
    assert metrics.get("auto_reconciled") == 3
    assert metrics.get("pending_review_reported") == 3
    # Band totals: 3 auto + 2 human review + 0 exception = 5
    assert metrics.get("match_rate") == round(3 / 5, 4)
    assert metrics.get("ingestion", {}).get("erp") == 20


THRESHOLD_BAND_REPORT = """# Financial Report Generated by AI Finance Controller

## Executive Summary
- Process Mode: **AR**
- Pending Human Count: 15
**NO HUMAN DECISIONS HAVE BEEN APPLIED YET.**

## Data Ingestion
| Source               | Records Loaded |
|----------------------|----------------|
| ERP                  | 20             |
| Bank                 | 3              |
| Payment Processor     | 3              |

## ReconQ Confidence Band Results
| Band             | Threshold       | Count | Action       |
|-------------------|-----------------|-------|--------------|
| 0.0-0.79         | 0.79            | 2     | Review Needed |
| 0.80-0.94        | 0.80            | 0     | -            |
| 0.95-1.0         | 0.95            | 1     | Auto-Match   |

## Human Decisions Status
| Decision Type            | Count |
|--------------------------|-------|
| PENDING (awaiting action) | 15    |

## AI Recommendations vs Human Decisions
### AUTO-MATCHED BY SYSTEM (no human needed)
- Candidate ID: 1

### AWAITING HUMAN DECISION (15 items)
- Exception IDs: 1, 3, 4

## Exception Analysis
| Type                          | Count | AI Recommended Action      |
|-------------------------------|-------|-----------------------------|
| MISMATCH                      | 1     | needs_human_review          |
| PARTIAL_SETTLEMENT            | 1     | needs_human_review          |
| MISSING_ERP_RECORD            | 1     | needs_human_review          |

## Evaluation Metrics
| Metric           | Value |
|------------------|-------|
| Accuracy         | N/A   |
| Match Rate       | N/A   |
| Pending Human Count | 15  |

## Data Integrity Check
- Total Source Records: 26
- Total Results: 0
"""


def test_threshold_band_report_and_exception_id_list():
    amp = {"result": {"raw": THRESHOLD_BAND_REPORT}, "state": "SUCCESS"}
    text = extract_report_text(amp)
    recs = extract_pending_records_from_amp(amp, text)
    metrics = parse_metrics_from_report(text)

    assert {r["transaction_id"] for r in recs} == {"EXC-1", "EXC-3", "EXC-4"}
    assert metrics.get("auto_reconciled") == 1
    assert metrics.get("pending_review_reported") == 15
    assert metrics.get("exception_count") == 3  # from exception analysis (or low band)
    # Prefer band totals: 1 auto / (1 + 0 + 3) — exception analysis wins over low-band 2
    assert metrics.get("match_rate") == round(1 / 4, 4)
    assert metrics.get("ingestion", {}).get("bank") == 3


def test_finalize_threshold_band_hydrates_dashboard(tmp_path: Path):
    db = FinanceDB(tmp_path / "amp2.db")
    out = finalize_amp_run(
        db,
        run_id="AMP-THRESHOLD",
        kickoff_id="kick-threshold",
        amp_status={"result": {"raw": THRESHOLD_BAND_REPORT}, "state": "SUCCESS"},
    )
    assert (out.get("metrics") or {}).get("auto_reconciled") == 1
    assert (out.get("metrics") or {}).get("pending_review") == 3
    assert (out.get("metrics") or {}).get("match_rate") is not None
    sources = db.fetch_sources("AMP-THRESHOLD")
    assert sum(1 for s in sources if s["source"] == "erp") >= 20
    assert sum(1 for s in sources if s["source"] in ("bank", "payment_processor")) >= 4
    assert len(pending_review_items(db, "AMP-THRESHOLD")) == 3
