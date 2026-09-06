#!/usr/bin/env python
"""Generate synthetic text-layer bank-statement PDFs for tests (safe demo data)."""

from __future__ import annotations

from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.units import inch
from reportlab.pdfgen import canvas
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "synthetic" / "bank_statements"
OUT.mkdir(parents=True, exist_ok=True)


def write_tabular(path: Path) -> None:
    doc = SimpleDocTemplate(str(path), pagesize=letter)
    styles = getSampleStyleSheet()
    data = [
        ["Transaction Date", "Transaction ID", "Description", "Reference", "Credit", "Debit"],
        ["2026-04-01", "TXN-1001", "Settlement from Acme Corp", "REF-ACME-001", "10000.00", "0.00"],
        ["2026-04-02", "TXN-1002", "Settlement from Test Vendor Ltd", "REF-TVL-002", "15000.00", "0.00"],
        ["2026-04-03", "TXN-1003", "Settlement from Globex Inc", "NEFT-99999", "20000.00", "0.00"],
        ["2026-04-04", "TXN-1004", "Settlement from Unknown Payer", "UPI-555", "4500.00", "0.00"],
        ["2026-04-05", "TXN-1005", "Settlement from INITECH SOL", "REF-INI-004", "8500.00", "0.00"],
    ]
    table = Table(data, colWidths=[1.1 * inch, 1.0 * inch, 2.2 * inch, 1.1 * inch, 0.8 * inch, 0.7 * inch])
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
                ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
                ("FONTSIZE", (0, 0), (-1, -1), 8),
            ]
        )
    )
    story = [
        Paragraph("Statement of Account — Tabular (synthetic)", styles["Heading2"]),
        Spacer(1, 12),
        table,
    ]
    doc.build(story)


def write_lines(path: Path) -> None:
    c = canvas.Canvas(str(path), pagesize=letter)
    c.setFont("Helvetica", 10)
    y = 750
    lines = [
        "Bank Statement — Line items (synthetic)",
        "Transaction Date Description Reference Amount",
        "2026-03-01 BNK-2001 Settlement from Acme Corp REF-ACME-2001 12500.50",
        "2026-03-02 BNK-2002 Settlement from Globex Inc NEFT-2002 9800.00",
        "2026-03-03 BNK-2003 Settlement from Initech UPI-2003 3200.75",
        "2026-03-04 BNK-2004 Settlement from Umbrella LLC REF-UMB-2004 1500.00",
    ]
    for line in lines:
        c.drawString(40, y, line)
        y -= 18
    c.showPage()
    c.save()


def main() -> None:
    write_tabular(OUT / "statement_a_tabular.pdf")
    write_lines(OUT / "statement_b_lines.pdf")
    print(f"wrote {OUT / 'statement_a_tabular.pdf'}")
    print(f"wrote {OUT / 'statement_b_lines.pdf'}")


if __name__ == "__main__":
    main()
