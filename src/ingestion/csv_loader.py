from __future__ import annotations
"""
CSV / tabular ingestion for reconq-core.

PDF/Docling extraction is out of scope — place structured CSVs here and map
columns via a simple header heuristic (or an explicit field_map).
"""

import csv
import io
import logging
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class CSVParseError(Exception):
    pass


# Canonical internal fields → header synonyms (lowercase substrings)
_INVOICE_SYNONYMS: dict[str, list[str]] = {
    "id": ["invoice_id", "invoice_no", "invoice number", "inv_no", "id"],
    "customer_name": ["customer_name", "customer name", "customer", "party", "payer"],
    "amount": ["grand_total", "total_amount", "invoice_amount", "amount", "total"],
    "due_date": ["due_date", "due date"],
    "invoice_date": ["invoice_date", "invoice date", "date"],
    "reference_number": ["reference_number", "reference", "ref", "po_reference"],
    "currency": ["currency", "ccy"],
    "status": ["status"],
}

_PAYMENT_SYNONYMS: dict[str, list[str]] = {
    "id": ["transaction_id", "txn_id", "payment_id", "id"],
    "sender_name": ["customer_name", "sender_name", "sender", "payer", "particulars"],
    "amount": ["credit", "amount", "value"],
    "debit": ["debit"],
    "date": ["transaction_date", "value_date", "date"],
    "reference": ["reference_number", "reference", "ref", "narration", "description"],
    "currency": ["currency", "ccy"],
}


def _norm_header(h: str) -> str:
    return re.sub(r"\s+", " ", (h or "").strip().lower())


def _build_header_map(headers: list[str], synonyms: dict[str, list[str]]) -> dict[str, str]:
    """Map internal field → actual CSV header."""
    norm_to_raw = {_norm_header(h): h for h in headers if h}
    mapping: dict[str, str] = {}
    for field, cands in synonyms.items():
        for cand in cands:
            for nh, raw in norm_to_raw.items():
                if cand == nh or cand in nh:
                    mapping[field] = raw
                    break
            if field in mapping:
                break
    return mapping


def _dec(val: str | None) -> Decimal:
    if val is None or str(val).strip() == "":
        return Decimal("0.00")
    cleaned = str(val).replace(",", "").replace("₹", "").replace("$", "").strip()
    try:
        return Decimal(cleaned)
    except InvalidOperation as e:
        raise CSVParseError(f"Invalid amount: {val!r}") from e


def load_csv_rows(path_or_text: str | Path, *, is_path: bool = True) -> list[dict[str, str]]:
    if is_path:
        text = Path(path_or_text).read_text(encoding="utf-8-sig")
    else:
        text = str(path_or_text)
    if not text.strip():
        raise CSVParseError("CSV is empty")
    reader = csv.DictReader(io.StringIO(text))
    return [{(k or "").strip(): (v or "").strip() for k, v in row.items()} for row in reader]


def parse_invoice_csv(path: str | Path, field_map: dict[str, str] | None = None) -> list[dict[str, Any]]:
    """
    Parse an invoice register CSV into engine-ready invoice dicts.

    Output keys match what StrictMatcher / LangGraph nodes expect
    (camelCase customerName, remaining_balance, source_system, …).
    """
    rows = load_csv_rows(path)
    if not rows:
        return []
    headers = list(rows[0].keys())
    mapping = field_map or _build_header_map(headers, _INVOICE_SYNONYMS)

    invoices: list[dict[str, Any]] = []
    for i, row in enumerate(rows, start=1):
        def g(field: str, default: str = "") -> str:
            raw = mapping.get(field)
            return (row.get(raw, default) if raw else default) or default

        inv_id = g("id") or f"INV-ROW-{i}"
        amount = _dec(g("amount", "0"))
        invoices.append({
            "id": inv_id,
            "customerName": g("customer_name") or "Unknown Customer",
            "amount": float(amount),
            "remaining_balance": float(amount),
            "currency": g("currency", "INR") or "INR",
            "dueDate": g("due_date"),
            "invoiceDate": g("invoice_date"),
            "status": (g("status") or "open").lower(),
            "referenceNumber": g("reference_number") or inv_id,
            "invoiceNumber": inv_id,
            "source_system": "invoice",
        })
    logger.info("[ingestion] Parsed %d invoices from %s", len(invoices), path)
    return invoices


def parse_payment_csv(path: str | Path, field_map: dict[str, str] | None = None) -> list[dict[str, Any]]:
    """
    Parse a bank/payment CSV into engine-ready payment dicts.

    Credits are inflows (AR receipts). Rows with only debit and no credit are skipped
    unless an explicit amount column is mapped.
    """
    rows = load_csv_rows(path)
    if not rows:
        return []
    headers = list(rows[0].keys())
    mapping = field_map or _build_header_map(headers, _PAYMENT_SYNONYMS)

    payments: list[dict[str, Any]] = []
    for i, row in enumerate(rows, start=1):
        def g(field: str, default: str = "") -> str:
            raw = mapping.get(field)
            return (row.get(raw, default) if raw else default) or default

        credit = _dec(g("credit") or g("amount"))
        debit = _dec(g("debit"))
        # Prefer credit (receipt); skip pure debits
        if credit <= 0 and debit > 0:
            continue
        amount = credit if credit > 0 else _dec(g("amount"))
        if amount <= 0:
            continue

        pay_id = g("id") or f"PAY-ROW-{i}"
        payments.append({
            "id": pay_id,
            "senderName": g("sender_name") or "Unknown",
            "amount": float(amount),
            "currency": g("currency", "INR") or "INR",
            "date": g("date"),
            "reference": g("reference"),
            "status": "unreconciled",
        })
    logger.info("[ingestion] Parsed %d payments from %s", len(payments), path)
    return payments
