from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.ingestion.pdf_loader import parse_payment_pdf


def _key(p: dict) -> tuple:
    return (
        round(float(p["amount"]), 2),
        str(p.get("reference") or "").upper(),
        str(p.get("date") or ""),
    )


def score_file(pdf_path: Path, expected: list[dict]) -> dict:
    parsed = parse_payment_pdf(pdf_path)
    exp_by_amt_ref = {_key(e): e for e in expected}
    # expected uses senderName/reference/date/amount
    exp_keys = {
        (
            round(float(e["amount"]), 2),
            str(e.get("reference") or "").upper(),
            str(e.get("date") or ""),
        ): e
        for e in expected
    }
    got_keys = {_key(p): p for p in parsed}

    correct = []
    for k, e in exp_keys.items():
        if k in got_keys:
            correct.append({"expected": e, "got": got_keys[k]})
    missed = [e for k, e in exp_keys.items() if k not in got_keys]
    extras = [p for k, p in got_keys.items() if k not in exp_keys]

    # id match bonus reporting
    return {
        "pdf": pdf_path.name,
        "expected": len(expected),
        "parsed": len(parsed),
        "correct": len(correct),
        "missed": missed,
        "misread_or_extra": extras,
        "parsed_rows": parsed,
    }


def main() -> int:
    gt_path = ROOT / "data" / "synthetic" / "bank_statements" / "ground_truth.json"
    gt = json.loads(gt_path.read_text(encoding="utf-8"))
    print("=== PDF bank statement extraction report ===\n")
    totals = {"expected": 0, "correct": 0, "missed": 0, "extra": 0}
    for item in gt:
        pdf = ROOT / "data" / "synthetic" / "bank_statements" / item["pdf"]
        # expected_credits entries may include 'file' key from generator
        expected = [
            {
                "id": r["id"],
                "date": r["date"],
                "senderName": r["senderName"],
                "amount": r["amount"],
                "reference": r["reference"],
            }
            for r in item["expected_credits"]
        ]
        rep = score_file(pdf, expected)
        totals["expected"] += rep["expected"]
        totals["correct"] += rep["correct"]
        totals["missed"] += len(rep["missed"])
        totals["extra"] += len(rep["misread_or_extra"])
        print(f"## {rep['pdf']}")
        print(f"  expected={rep['expected']} parsed={rep['parsed']} correct={rep['correct']}")
        print(f"  missed={len(rep['missed'])} extras/misreads={len(rep['misread_or_extra'])}")
        if rep["missed"]:
            print("  MISS:", json.dumps(rep["missed"], indent=2))
        if rep["misread_or_extra"]:
            print("  EXTRA:", json.dumps(rep["misread_or_extra"], indent=2))
        print()
    print("=== TOTALS ===")
    print(json.dumps(totals, indent=2))
    if totals["expected"]:
        print(f"recall={totals['correct'] / totals['expected']:.1%}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
