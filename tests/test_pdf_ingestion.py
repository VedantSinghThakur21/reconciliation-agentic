from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.ingestion.csv_loader import parse_payment_csv
from src.ingestion.pdf_loader import PDFParseError, parse_payment_pdf
from src.pipeline import run_pipeline


def test_pdf_tabular_parses_credits():
    pdf = ROOT / "data" / "synthetic" / "bank_statements" / "statement_a_tabular.pdf"
    rows = parse_payment_pdf(pdf)
    assert len(rows) >= 5
    amounts = sorted(r["amount"] for r in rows)
    assert 10000.0 in amounts
    assert 20000.0 in amounts
    # schema parity with CSV loader
    assert {"id", "senderName", "amount", "currency", "date", "reference", "status"} <= set(rows[0])


def test_pdf_lines_parse():
    pdf = ROOT / "data" / "synthetic" / "bank_statements" / "statement_b_lines.pdf"
    rows = parse_payment_pdf(pdf)
    assert len(rows) == 4
    assert any(r["id"] == "BNK-2001" for r in rows)


def test_pipeline_accepts_payments_pdf():
    result = run_pipeline(
        ROOT / "data" / "synthetic" / "sample_invoices.csv",
        payment_pdf=ROOT / "data" / "synthetic" / "bank_statements" / "statement_a_tabular.pdf",
    )
    assert result.summary()["total_decisions"] >= 1


def test_csv_path_unchanged():
    csv_rows = parse_payment_csv(ROOT / "data" / "synthetic" / "sample_payments.csv")
    assert len(csv_rows) == 5
    result = run_pipeline(
        ROOT / "data" / "synthetic" / "sample_invoices.csv",
        ROOT / "data" / "synthetic" / "sample_payments.csv",
    )
    assert result.summary()["auto_reconciled"] >= 2


def test_empty_text_pdf_errors_without_ocr(tmp_path):
    # Minimal PDF with no text content (blank page)
    from reportlab.pdfgen import canvas

    blank = tmp_path / "blank.pdf"
    c = canvas.Canvas(str(blank))
    c.showPage()
    c.save()
    try:
        parse_payment_pdf(blank)
        assert False, "expected PDFParseError"
    except PDFParseError as e:
        assert "OCR" in str(e) or "No extractable text" in str(e) or "could not parse" in str(e)
