#!/usr/bin/env python
"""Generate bank / payment_processor / ground_truth CSVs aligned to ERP stubs.

Produces a 50+ labeled evaluation batch for Track 04 judging.
"""

from __future__ import annotations

import csv
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.finance_controller.utils import DEMO_ERP_COUNT, stub_erp_transactions  # noqa: E402

DEMO = ROOT / "data" / "demo"
DEMO.mkdir(parents=True, exist_ok=True)

# Slice plan for DEMO_ERP_COUNT=51 ERP stubs → 52 ground-truth labels:
#   0-39   exact bank matches          (40)
#   40-44  processor exact matches     (5)
#   45-47  fuzzy merchant / date drift (3)
#   48     partial settlement 50%      (1)
#   49-50  ERP with no bank/PP (MISSING_BANK_RECORD) (2)
#   + BNK-9999 unknown payer (MISSING_ERP_RECORD)    (1)
N_EXACT_BANK = 40
N_EXACT_PP = 5
N_FUZZY = 3
N_PARTIAL = 1
N_MISSING_BANK = 2


def main() -> None:
    assert DEMO_ERP_COUNT == (
        N_EXACT_BANK + N_EXACT_PP + N_FUZZY + N_PARTIAL + N_MISSING_BANK
    ), "DEMO_ERP_COUNT must match generator slice plan"

    erp = stub_erp_transactions(DEMO_ERP_COUNT, mode="AR")
    bank_rows: list[dict] = []
    pp_rows: list[dict] = []
    gt: list[dict] = []

    for i, inv in enumerate(erp):
        if i < N_EXACT_BANK:
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
        elif i < N_EXACT_BANK + N_EXACT_PP:
            j = i - N_EXACT_BANK
            row = {
                "transaction_id": f"PP-{3001 + j}",
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
        elif i < N_EXACT_BANK + N_EXACT_PP + N_FUZZY:
            j = i - N_EXACT_BANK - N_EXACT_PP
            row = {
                "transaction_id": f"BNK-{2100 + j}",
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
        elif i == N_EXACT_BANK + N_EXACT_PP + N_FUZZY:
            half = round(inv["amount"] * 0.5, 2)
            row = {
                "transaction_id": "PP-3100",
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
        else:
            # Remaining ERP rows: no counterparty payment → MISSING_BANK_RECORD
            gt.append({
                "transaction_id": inv["transaction_id"],
                "expected_match_id": "",
                "expected_status": "EXCEPTION",
                "exception_type": "MISSING_BANK_RECORD",
            })

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

    fields = [
        "transaction_id",
        "date",
        "amount",
        "currency",
        "merchant",
        "reference",
        "description",
    ]
    write(DEMO / "bank.csv", bank_rows, fields)
    write(DEMO / "payment_processor.csv", pp_rows, fields)
    write(
        DEMO / "ground_truth.csv",
        gt,
        ["transaction_id", "expected_match_id", "expected_status", "exception_type"],
    )

    sources = DEMO_ERP_COUNT + len(bank_rows) + len(pp_rows)
    print(
        f"bank={len(bank_rows)} pp={len(pp_rows)} gt={len(gt)} "
        f"erp_stubs={DEMO_ERP_COUNT} total_sources={sources}"
    )
    assert len(gt) >= 50, f"ground_truth must be 50+, got {len(gt)}"
    assert sources >= 50, f"sources must be 50+, got {sources}"


if __name__ == "__main__":
    main()
