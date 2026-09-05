#!/usr/bin/env python
"""Generate 50+ synthetic multi-source demo records + ground truth."""

from __future__ import annotations

import csv
import random
from datetime import date, timedelta
from pathlib import Path

random.seed(42)
DEMO = Path(__file__).resolve().parents[1] / "data" / "demo"
DEMO.mkdir(parents=True, exist_ok=True)

CUSTOMERS = [
    ("Acme Corp", "ACME"),
    ("Test Vendor Ltd", "TVL"),
    ("Globex Inc", "GLOBEX"),
    ("Initech Solutions", "INI"),
    ("Soylent Corp", "SOY"),
    ("Umbrella Ltd", "UMB"),
    ("Wayne Enterprises", "WAYNE"),
    ("Stark Industries", "STARK"),
    ("Oscorp", "OSC"),
    ("Wonka Industries", "WONKA"),
    ("Cyberdyne Systems", "CYB"),
    ("Hooli Inc", "HOOLI"),
    ("Pied Piper", "PIPER"),
    ("Massive Dynamic", "MASS"),
    ("Aperture Science", "APT"),
    ("Buy n Large", "BNL"),
    ("Dunder Mifflin", "DUND"),
    ("Prestige Worldwide", "PREST"),
    ("Vandelay Industries", "VAND"),
    ("Bluth Company", "BLUTH"),
]

AMOUNTS = [2500, 4500, 7500, 8500, 10000, 12000, 15000, 20000, 25000, 30000]

FUZZY = {
    "Initech Solutions": "INITECH SOL",
    "Soylent Corp": "Soylent Corporation",
    "Wayne Enterprises": "Wayne Ent",
    "Stark Industries": "Stark Ind",
    "Cyberdyne Systems": "Cyberdyne Sys",
    "Dunder Mifflin": "Dunder Mifflin Paper",
}


def write_csv(path: Path, rows: list[dict], fieldnames: list[str]) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)


def main() -> None:
    invoices: list[dict] = []
    for i in range(1, 56):
        cust, code = CUSTOMERS[(i - 1) % len(CUSTOMERS)]
        amt = round(random.choice(AMOUNTS) + random.randint(0, 99) + random.random(), 2)
        d = date(2026, 3, 1) + timedelta(days=i)
        invoices.append(
            {
                "Invoice_ID": f"INV-{1000 + i}",
                "Invoice_Date": d.isoformat(),
                "Customer_Name": cust,
                "Invoice_Amount": f"{amt:.2f}",
                "Due_Date": (d + timedelta(days=15)).isoformat(),
                "Status": "open",
                "Reference_Number": f"REF-{code}-{1000 + i}",
                "Source": "quickbooks",
            }
        )

    payments: list[tuple[str, dict]] = []
    gt: list[dict] = []

    def add_pay(
        pid: str,
        sender: str,
        ref: str,
        amount: str | float,
        source: str,
        expected_inv: str,
        expected_status: str,
        notes: str,
        day_off: int,
    ) -> None:
        d = date(2026, 3, 1) + timedelta(days=day_off)
        payments.append(
            (
                source,
                {
                    "Transaction_ID": pid,
                    "Transaction_Date": d.isoformat(),
                    "Description": f"Settlement from {sender}",
                    "Reference_Number": ref,
                    "Debit": "0",
                    "Credit": f"{float(amount):.2f}",
                    "Customer_Name": sender,
                    "Source": source,
                },
            )
        )
        gt.append(
            {
                "payment_id": pid,
                "expected_invoice_id": expected_inv,
                "expected_status": expected_status,
                "notes": notes,
            }
        )

    # 42 exact reference matches
    for i, inv in enumerate(invoices[:42]):
        source = "bank" if i % 3 else "payment_processor"
        pid = f"TXN-{2000 + i}" if source == "bank" else f"PP-{3000 + i}"
        add_pay(
            pid,
            inv["Customer_Name"],
            inv["Reference_Number"],
            inv["Invoice_Amount"],
            source,
            inv["Invoice_ID"],
            "reconciled",
            "Exact reference",
            20 + i,
        )

    # 6 fuzzy name + exact amount
    for j, inv in enumerate(invoices[42:48]):
        mangled = FUZZY.get(inv["Customer_Name"], inv["Customer_Name"][:10])
        add_pay(
            f"TXN-{2100 + j}",
            mangled,
            f"NEFT-FUZZY-{j}",
            inv["Invoice_Amount"],
            "bank",
            inv["Invoice_ID"],
            "reconciled",
            "Fuzzy name + amount",
            70 + j,
        )

    # 3 partial settlements
    for j, inv in enumerate(invoices[48:51]):
        half = float(inv["Invoice_Amount"]) / 2
        add_pay(
            f"PP-{3100 + j}",
            inv["Customer_Name"],
            f"PARTIAL-{j}",
            half,
            "payment_processor",
            inv["Invoice_ID"],
            "pending_review",
            "Partial settlement",
            80 + j,
        )

    # 4 unmatched unknowns
    for j in range(4):
        add_pay(
            f"TXN-{2200 + j}",
            f"Unknown Payer {j + 1}",
            f"UPI-MISS-{j}",
            3333.33 + j * 100,
            "bank",
            "",
            "pending_review",
            "No matching invoice",
            90 + j,
        )

    # 2 duplicates of already-matched invoices
    for j, inv in enumerate(invoices[:2]):
        add_pay(
            f"TXN-{2300 + j}",
            inv["Customer_Name"],
            inv["Reference_Number"],
            inv["Invoice_Amount"],
            "bank",
            inv["Invoice_ID"],
            "pending_review",
            "Duplicate / already allocated",
            100 + j,
        )

    bank_rows = [r for s, r in payments if s == "bank"]
    pp_rows = [r for s, r in payments if s == "payment_processor"]

    write_csv(DEMO / "quickbooks_invoices.csv", invoices, list(invoices[0].keys()))
    write_csv(DEMO / "bank_transactions.csv", bank_rows, list(bank_rows[0].keys()))
    write_csv(DEMO / "payment_processor.csv", pp_rows, list(pp_rows[0].keys()))
    write_csv(
        DEMO / "ground_truth.csv",
        gt,
        ["payment_id", "expected_invoice_id", "expected_status", "notes"],
    )

    print(f"invoices={len(invoices)} payments={len(payments)} gt={len(gt)}")
    print(f"bank={len(bank_rows)} processor={len(pp_rows)}")


if __name__ == "__main__":
    main()
