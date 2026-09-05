#!/usr/bin/env python
"""Generate bank / payment_processor / ground_truth CSVs aligned to ERP stubs."""

from __future__ import annotations

import csv
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.finance_controller.utils import stub_erp_transactions  # noqa: E402

DEMO = ROOT / "data" / "demo"
DEMO.mkdir(parents=True, exist_ok=True)


def main() -> None:
    erp = stub_erp_transactions(24, mode="AR")
    bank_rows = []
    pp_rows = []
    gt = []

    for i, inv in enumerate(erp):
        # First 16 → exact bank matches
        if i < 16:
            row = {
                "transaction_id": f"BNK-{2001 + i}",
                "date": inv["date"],
                "amount": f"{inv['amount']:.2f}",
                "currency": "INR",
                "merchant": inv["merchant"],
                "reference": inv["reference"],
                "description": f"NEFT {inv['merchant']}",
            }
            bank_rows.append(row)
            gt.append({
                "transaction_id": row["transaction_id"],
                "expected_match_id": inv["transaction_id"],
                "expected_status": "MATCH",
                "exception_type": "",
            })
        # 16-18 → processor exact
        elif i < 19:
            row = {
                "transaction_id": f"PP-{3001 + (i - 16)}",
                "date": inv["date"],
                "amount": f"{inv['amount']:.2f}",
                "currency": "INR",
                "merchant": inv["merchant"],
                "reference": inv["reference"],
                "description": f"Settlement {inv['merchant']}",
            }
            pp_rows.append(row)
            gt.append({
                "transaction_id": row["transaction_id"],
                "expected_match_id": inv["transaction_id"],
                "expected_status": "MATCH",
                "exception_type": "",
            })
        # 19-20 → fuzzy merchant / slight date drift
        elif i < 21:
            row = {
                "transaction_id": f"BNK-{2100 + (i - 19)}",
                "date": inv["date"],
                "amount": f"{inv['amount']:.2f}",
                "currency": "INR",
                "merchant": inv["merchant"][:8],
                "reference": f"NEFT-FUZZY-{i}",
                "description": f"Transfer {inv['merchant'][:8]}",
            }
            bank_rows.append(row)
            gt.append({
                "transaction_id": row["transaction_id"],
                "expected_match_id": inv["transaction_id"],
                "expected_status": "MATCH",
                "exception_type": "",
            })
        # 21 → partial settlement 50%
        elif i == 21:
            half = round(inv["amount"] * 0.5, 2)
            row = {
                "transaction_id": f"PP-{3100}",
                "date": inv["date"],
                "amount": f"{half:.2f}",
                "currency": "INR",
                "merchant": inv["merchant"],
                "reference": "PARTIAL-50",
                "description": "Partial settlement",
            }
            pp_rows.append(row)
            gt.append({
                "transaction_id": row["transaction_id"],
                "expected_match_id": inv["transaction_id"],
                "expected_status": "EXCEPTION",
                "exception_type": "PARTIAL_SETTLEMENT",
            })
        # 22-23 left unmatched on ERP side (MISSING_BANK_RECORD)
        else:
            gt.append({
                "transaction_id": inv["transaction_id"],
                "expected_match_id": "",
                "expected_status": "EXCEPTION",
                "exception_type": "MISSING_BANK_RECORD",
            })

    # Unknown bank payment → MISSING_ERP_RECORD
    unk = {
        "transaction_id": "BNK-9999",
        "date": "2026-04-15",
        "amount": "4321.00",
        "currency": "INR",
        "merchant": "Unknown Payer",
        "reference": "UPI-MISS-1",
        "description": "Unidentified credit",
    }
    bank_rows.append(unk)
    gt.append({
        "transaction_id": "BNK-9999",
        "expected_match_id": "",
        "expected_status": "EXCEPTION",
        "exception_type": "MISSING_ERP_RECORD",
    })

    def write(path: Path, rows: list[dict], fields: list[str]) -> None:
        with path.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fields)
            w.writeheader()
            w.writerows(rows)

    write(
        DEMO / "bank.csv",
        bank_rows,
        ["transaction_id", "date", "amount", "currency", "merchant", "reference", "description"],
    )
    write(
        DEMO / "payment_processor.csv",
        pp_rows,
        ["transaction_id", "date", "amount", "currency", "merchant", "reference", "description"],
    )
    write(
        DEMO / "ground_truth.csv",
        gt,
        ["transaction_id", "expected_match_id", "expected_status", "exception_type"],
    )
    print(f"bank={len(bank_rows)} pp={len(pp_rows)} gt={len(gt)} erp_stubs=24")


if __name__ == "__main__":
    main()
