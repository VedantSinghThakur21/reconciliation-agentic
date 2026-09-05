#!/usr/bin/env python3
"""Generate synthetic text-layer bank statement PDFs for PDF ingestion tests."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet

OUT = ROOT / "data" / "synthetic" / "bank_statements"
OUT.mkdir(parents=True, exist_ok=True)


def pdf_tabular() -> list[dict]:
    """Statement A: clean table (same 5 credits as sample_payments.csv)."""
    path = OUT / "statement_a_tabular.pdf"
    rows_gt = [
        {"id": "TXN-1001", "date": "2026-04-01", "senderName": "Acme Corp", "amount": 10000.00, "reference": "REF-ACME-001"},
        {"id": "TXN-1002", "date": "2026-04-02", "senderName": "Test Vendor Ltd", "amount": 15000.00, "reference": "REF-TVL-002"},
        {"id": "TXN-1003", "date": "2026-04-03", "senderName": "Globex Inc", "amount": 20000.00, "reference": "NEFT-99999"},
        {"id": "TXN-1004", "date": "2026-04-04", "senderName": "Unknown Payer", "amount": 4500.00, "reference": "UPI-555"},
        {"id": "TXN-1005", "date": "2026-04-05", "senderName": "INITECH SOL", "amount": 8500.00, "reference": "REF-INI-004"},
    ]
    doc = SimpleDocTemplate(str(path), pagesize=A4)
    styles = getSampleStyleSheet()
    data = [["Date", "Transaction ID", "Description", "Reference", "Debit", "Credit"]]
    for r in rows_gt:
        data.append([
            r["date"],
            r["id"],
            f"NEFT from {r['senderName']}",
            r["reference"],
            "0.00",
            f"{r['amount']:.2f}",
        ])
    # include one debit row that should be skipped
    data.append(["2026-04-06", "TXN-DBIT", "ATM WDL", "ATM-1", "500.00", "0.00"])

    table = Table(data, colWidths=[70, 70, 150, 90, 55, 55])
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#0f766e")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.grey),
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
    ]))
    story = [
        Paragraph("Statement of Account — HDFC ****1234", styles["Title"]),
        Paragraph("Period: 01-Apr-2026 to 06-Apr-2026", styles["Normal"]),
        Spacer(1, 8),
        table,
    ]
    doc.build(story)
    return [{"file": path.name, **r} for r in rows_gt]


def pdf_line_oriented() -> list[dict]:
    """Statement B: line-oriented machine statement."""
    path = OUT / "statement_b_lines.pdf"
    rows_gt = [
        {"id": "BNK-2001", "date": "2026-03-01", "senderName": "Acme Corp", "amount": 12500.50, "reference": "REF-ACME-2001"},
        {"id": "BNK-2002", "date": "2026-03-02", "senderName": "Globex Inc", "amount": 9800.00, "reference": "NEFT-2002"},
        {"id": "BNK-2003", "date": "2026-03-03", "senderName": "Initech", "amount": 3200.75, "reference": "UPI-2003"},
        {"id": "BNK-2004", "date": "2026-03-04", "senderName": "Umbrella LLC", "amount": 1500.00, "reference": "REF-UMB-2004"},
    ]
    c = canvas.Canvas(str(path), pagesize=A4)
    w, h = A4
    y = h - 40
    c.setFont("Helvetica-Bold", 14)
    c.drawString(40, y, "Bank Statement (electronic) — ICICI ****8899")
    y -= 24
    c.setFont("Helvetica", 9)
    c.drawString(40, y, "Opening Balance 50,000.00")
    y -= 20
    c.setFont("Courier", 8)
    for r in rows_gt:
        line = (
            f"{r['date']}  {r['id']}  NEFT from {r['senderName']}  "
            f"{r['reference']}  {r['amount']:.2f}"
        )
        c.drawString(40, y, line)
        y -= 14
    c.setFont("Helvetica", 9)
    y -= 10
    c.drawString(40, y, "Closing Balance 81,001.25")
    c.save()
    return [{"file": path.name, **r} for r in rows_gt]


def pdf_messy_dates() -> list[dict]:
    """Statement C: DD/MM/YYYY dates + description/amount lines (no explicit txn id in text)."""
    path = OUT / "statement_c_ddmmyyyy.pdf"
    rows_gt = [
        {"id": "PDF-LN-1", "date": "2026-02-10", "senderName": "Acme Corp", "amount": 7777.00, "reference": "REF-ACME-777"},
        {"id": "PDF-LN-2", "date": "2026-02-11", "senderName": "Test Vendor Ltd", "amount": 3333.33, "reference": "REF-TVL-333"},
        {"id": "PDF-LN-3", "date": "2026-02-12", "senderName": "Globex Inc", "amount": 1010.10, "reference": "NEFT-1010"},
    ]
    c = canvas.Canvas(str(path), pagesize=A4)
    w, h = A4
    y = h - 50
    c.setFont("Helvetica-Bold", 12)
    c.drawString(40, y, "Account Statement — Axis Bank")
    y -= 28
    c.setFont("Courier", 9)
    # IDs not in PDF — parser will assign PDF-LN-N; ground truth uses those expected ids
    lines = [
        "10/02/2026  NEFT from Acme Corp  REF-ACME-777  7,777.00",
        "11/02/2026  NEFT from Test Vendor Ltd  REF-TVL-333  3,333.33",
        "12/02/2026  NEFT from Globex Inc  NEFT-1010  1,010.10",
        "13/02/2026  ATM WDL  ATM-99  200.00",  # may parse as credit wrongly if no debit marker — amount only
    ]
    # Last line is ambiguous (no CR/DR). We'll note it as a known risk in the report.
    for line in lines:
        c.drawString(40, y, line)
        y -= 16
    c.save()
    return [{"file": path.name, **r} for r in rows_gt]


def main() -> int:
    gt = {
        "statement_a_tabular.pdf": pdf_tabular(),
        "statement_b_lines.pdf": pdf_line_oriented(),
        "statement_c_ddmmyyyy.pdf": pdf_messy_dates(),
    }
    # flatten note: pdf_tabular returns list with file key each row
    flat = []
    for fname, rows in gt.items():
        flat.append({"pdf": fname, "expected_credits": rows})
    out_path = OUT / "ground_truth.json"
    out_path.write_text(json.dumps(flat, indent=2), encoding="utf-8")
    print(f"Wrote PDFs + {out_path}")
    for item in flat:
        print(f"  {item['pdf']}: {len(item['expected_credits'])} expected credits")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
