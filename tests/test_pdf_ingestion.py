from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.ingestion.csv_loader import parse_payment_csv
from src.ingestion.pdf_loader import PDFParseError, parse_payment_pdf
from src.pipeline import run_pipeline

PDF_A = ROOT / "data" / "synthetic" / "bank_statements" / "statement_a_tabular.pdf"
PDF_B = ROOT / "data" / "synthetic" / "bank_statements" / "statement_b_lines.pdf"

requires_pdf_a = pytest.mark.skipif(
    not PDF_A.exists(),
    reason=f"synthetic PDF fixture missing: {PDF_A.name} (run scripts/generate_synthetic_bank_pdfs.py)",
)
requires_pdf_b = pytest.mark.skipif(
    not PDF_B.exists(),
    reason=f"synthetic PDF fixture missing: {PDF_B.name} (run scripts/generate_synthetic_bank_pdfs.py)",
)


@requires_pdf_a
def test_pdf_tabular_parses_credits():
    rows = parse_payment_pdf(PDF_A)
    assert len(rows) >= 5
    amounts = sorted(r["amount"] for r in rows)
    assert 10000.0 in amounts
    assert 20000.0 in amounts
    # schema parity with CSV loader
    assert {"id", "senderName", "amount", "currency", "date", "reference", "status"} <= set(rows[0])


@requires_pdf_b
def test_pdf_lines_parse():
    rows = parse_payment_pdf(PDF_B)
    assert len(rows) == 4
    assert any(r["id"] == "BNK-2001" for r in rows)


@requires_pdf_a
def test_pipeline_accepts_payments_pdf():
    result = run_pipeline(
        ROOT / "data" / "synthetic" / "sample_invoices.csv",
        payment_pdf=PDF_A,
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
