from __future__ import annotations
"""
Bank-statement PDF ingestion (text-layer only).

Produces the same payment dict schema as `parse_payment_csv` so StrictMatcher
and the rest of the pipeline stay unchanged.

OCR for scanned/image-only PDFs is intentionally OUT OF SCOPE for this branch:
if a PDF has no extractable text, we raise a clear error rather than guessing.
"""

import logging
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Same shape as csv_loader.parse_payment_csv outputs
PaymentDict = dict[str, Any]


class PDFParseError(Exception):
    pass


_DATE = r"(?P<date>\d{4}[-/]\d{1,2}[-/]\d{1,2}|\d{1,2}[-/]\d{1,2}[-/]\d{2,4})"
_AMOUNT = r"(?P<amount>[\d,]+\.\d{2})"
_TXN_ID = r"(?P<id>(?:TXN|PAY|BNK|NEFT|UPI)[-_][A-Z0-9]+)"
_REF = r"(?P<ref>(?:REF|NEFT|UPI|IMPS|CHQ)[-_][A-Z0-9-]+)"

# Prefer patterns that capture txn id before looser date/desc/amount forms
_LINE_PATTERNS = [
    re.compile(
        rf"^{_DATE}\s+{_TXN_ID}\s+(?P<desc>.+?)\s+{_REF}\s+{_AMOUNT}\s*$",
        re.I,
    ),
    re.compile(
        rf"^{_TXN_ID}\s+{_DATE}\s+(?P<desc>.+?)\s+{_AMOUNT}\s*$",
        re.I,
    ),
    re.compile(
        rf"^{_DATE}\s+(?P<desc>.+?)\s+{_REF}\s+{_AMOUNT}\s*$",
        re.I,
    ),
    re.compile(
        rf"^{_DATE}\s+(?P<desc>.+?)\s+{_AMOUNT}\s*$",
        re.I,
    ),
]


def _dec(val: str | None) -> Decimal:
    if val is None or str(val).strip() == "":
        return Decimal("0.00")
    cleaned = str(val).replace(",", "").replace("₹", "").replace("$", "").strip()
    # trailing CR/DR markers
    cleaned = re.sub(r"\s*(CR|DR|Cr|Dr)$", "", cleaned).strip()
    try:
        return Decimal(cleaned)
    except InvalidOperation as e:
        raise PDFParseError(f"Invalid amount: {val!r}") from e


def _norm_date(raw: str) -> str:
    raw = (raw or "").strip().replace("/", "-")
    parts = raw.split("-")
    if len(parts) != 3:
        return raw
    # DD-MM-YYYY → YYYY-MM-DD
    if len(parts[0]) <= 2 and len(parts[2]) == 4:
        d, m, y = parts
        return f"{y}-{int(m):02d}-{int(d):02d}"
    # YYYY-M-D
    if len(parts[0]) == 4:
        y, m, d = parts
        return f"{y}-{int(m):02d}-{int(d):02d}"
    return raw


def _guess_sender(desc: str) -> str:
    d = re.sub(r"\s+", " ", (desc or "").strip())
    d = re.sub(r"^(NEFT|RTGS|IMPS|UPI|TRANSFER|FROM|IN)\s+(from\s+)?", "", d, flags=re.I)
    # drop trailing ref tokens already captured elsewhere
    d = re.sub(r"\b(REF|NEFT|UPI|IMPS)[-_][A-Z0-9-]+\b", "", d, flags=re.I).strip(" -|")
    return d or "Unknown"


def extract_pdf_text(path: str | Path) -> tuple[str, list[list[list[str | None]]]]:
    """
    Extract plain text and table cell grids via pdfplumber.

    Returns (full_text, tables) where each table is a list of rows of cell strings.
    """
    try:
        import pdfplumber
    except ImportError as e:
        raise PDFParseError(
            "pdfplumber is required for PDF ingestion. Install with: pip install pdfplumber"
        ) from e

    path = Path(path)
    if not path.exists():
        raise PDFParseError(f"PDF not found: {path}")

    chunks: list[str] = []
    tables: list[list[list[str | None]]] = []
    with pdfplumber.open(path) as pdf:
        if not pdf.pages:
            raise PDFParseError(f"PDF has no pages: {path}")
        for page in pdf.pages:
            t = page.extract_text() or ""
            chunks.append(t)
            for tbl in page.extract_tables() or []:
                tables.append(tbl)

    full = "\n".join(chunks).strip()
    return full, tables


def _payments_from_tables(tables: list[list[list[str | None]]]) -> list[PaymentDict]:
    """Parse tabular bank extracts (header row + data rows)."""
    payments: list[PaymentDict] = []
    seen: set[str] = set()

    for tbl in tables:
        if not tbl or len(tbl) < 2:
            continue
        header = [re.sub(r"\s+", " ", (c or "").strip().lower()) for c in tbl[0]]

        def col(*names: str) -> int | None:
            """Prefer exact / token matches; avoid naive substring (e.g. 'id' in 'credit')."""
            for n in names:
                n = n.lower()
                for i, h in enumerate(header):
                    if h == n:
                        return i
                for i, h in enumerate(header):
                    tokens = h.replace("_", " ").split()
                    if n in tokens or n.replace(" ", "_") == h.replace(" ", "_"):
                        return i
                for i, h in enumerate(header):
                    if len(n) >= 3 and (h.startswith(n) or n in h):
                        return i
            return None

        i_date = col("transaction date", "value date", "txn date", "date")
        i_desc = col("description", "narration", "particulars", "details")
        i_ref = col("reference", "reference number", "ref no", "cheque")
        i_credit = col("credit", "deposit", "cr amount")
        i_debit = col("debit", "withdrawal")
        i_amount = col("amount", "value")
        i_id = col("transaction id", "txn id", "txn_id", "payment id", "id")
        i_sender = col("customer name", "customer", "sender", "payer", "party")

        if i_date is None and i_amount is None and i_credit is None:
            continue

        for ridx, row in enumerate(tbl[1:], start=1):
            cells = [(c or "").strip() for c in row]
            if not any(cells):
                continue

            def cell(idx: int | None) -> str:
                if idx is None or idx >= len(cells):
                    return ""
                return cells[idx]

            credit_s = cell(i_credit)
            debit_s = cell(i_debit)
            amount_s = credit_s or cell(i_amount)
            if not amount_s:
                continue
            try:
                amount = _dec(amount_s)
                debit = _dec(debit_s) if debit_s else Decimal("0")
            except PDFParseError:
                continue
            # Skip pure debits when credit column is present/empty-zero
            if amount <= 0:
                continue
            if i_credit is not None and _dec(credit_s) <= 0 and debit > 0:
                continue

            pay_id = cell(i_id) or f"PDF-TBL-{len(payments) + 1}"
            if pay_id in seen:
                pay_id = f"{pay_id}-{ridx}"
            seen.add(pay_id)

            desc = cell(i_desc)
            if re.search(r"\b(ATM|WDL|WITHDRAWAL)\b", desc, re.I):
                continue
            sender = cell(i_sender) or _guess_sender(desc)
            payments.append({
                "id": pay_id,
                "senderName": sender or "Unknown",
                "amount": float(amount),
                "currency": "INR",
                "date": _norm_date(cell(i_date)),
                "reference": cell(i_ref) or desc,
                "status": "unreconciled",
                "source_system": "pdf_bank_statement",
            })
    return payments


def _payments_from_text(text: str) -> list[PaymentDict]:
    payments: list[PaymentDict] = []
    seen: set[str] = set()
    for line in text.splitlines():
        line = re.sub(r"\s+", " ", line).strip()
        if not line or len(line) < 8:
            continue
        # Skip obvious headers / footers
        low = line.lower()
        if any(
            x in low
            for x in (
                "statement of account",
                "opening balance",
                "closing balance",
                "page ",
                "transaction date",
                "description",
                "debit",
                "credit",
            )
        ):
            # allow data lines that merely contain the word credit as amount label elsewhere
            if re.search(r"^\d", line) is None and "txn-" not in low:
                continue

        matched = None
        for pat in _LINE_PATTERNS:
            m = pat.match(line)
            if m:
                matched = m
                break
        if not matched:
            continue

        gd = matched.groupdict()
        try:
            amount = _dec(gd.get("amount"))
        except PDFParseError:
            continue
        if amount <= 0:
            continue

        desc = (gd.get("desc") or "").strip()
        if re.search(r"\b(ATM|WDL|WITHDRAWAL|POS\s*DEBIT)\b", desc, re.I):
            continue
        pay_id = (gd.get("id") or "").strip() or f"PDF-LN-{len(payments) + 1}"
        if pay_id in seen:
            pay_id = f"{pay_id}-{len(payments) + 1}"
        seen.add(pay_id)
        ref = (gd.get("ref") or "").strip() or desc
        payments.append({
            "id": pay_id,
            "senderName": _guess_sender(desc),
            "amount": float(amount),
            "currency": "INR",
            "date": _norm_date(gd.get("date") or ""),
            "reference": ref,
            "status": "unreconciled",
            "source_system": "pdf_bank_statement",
        })
    return payments


def parse_payment_pdf(path: str | Path) -> list[PaymentDict]:
    """
    Parse a text-layer bank statement PDF into engine-ready payment dicts.

    Prefer table extraction; fall back to line regex on plain text.
    Raises PDFParseError if the PDF has no extractable text (scanned / image-only).
    """
    text, tables = extract_pdf_text(path)
    if not text and not any(tables):
        raise PDFParseError(
            f"No extractable text in {path}. "
            "OCR for scanned PDFs is out of scope on this branch — "
            "use a text-based statement PDF or the CSV payment path."
        )

    from_tables = _payments_from_tables(tables)
    from_text = _payments_from_text(text)

    # Prefer table extraction when it yields rows (more reliable field split)
    if from_tables:
        payments = from_tables
    else:
        payments = from_text

    payments.sort(key=lambda p: (p.get("date") or "", p.get("id") or ""))

    if not payments:
        raise PDFParseError(
            f"Extracted text from {path} but could not parse any credit transactions. "
            "Expected dated lines or a table with Date/Description/Credit columns."
        )

    logger.info(
        "[ingestion:pdf] Parsed %d payments from %s (tables=%d, text_len=%d)",
        len(payments),
        path,
        len(tables),
        len(text),
    )
    return payments
