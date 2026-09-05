from __future__ import annotations

from pathlib import Path
from typing import Any

from src.ingestion.csv_loader import parse_invoice_csv, parse_payment_csv

DEMO_DIR = Path(__file__).resolve().parents[2] / "data" / "demo"


def load_quickbooks_invoices(path: Path | None = None) -> list[dict[str, Any]]:
    p = path or (DEMO_DIR / "quickbooks_invoices.csv")
    invoices = parse_invoice_csv(p)
    for inv in invoices:
        # StrictMatcher only considers source_system == "invoice"
        inv["source_system"] = "invoice"
        inv["source"] = "quickbooks"
    return invoices


def load_bank_payments(path: Path | None = None) -> list[dict[str, Any]]:
    p = path or (DEMO_DIR / "bank_transactions.csv")
    payments = parse_payment_csv(p)
    for pay in payments:
        pay["source"] = "bank"
        pay["channel"] = "bank"
    return payments


def load_processor_payments(path: Path | None = None) -> list[dict[str, Any]]:
    p = path or (DEMO_DIR / "payment_processor.csv")
    payments = parse_payment_csv(p)
    for pay in payments:
        pay["source"] = "payment_processor"
        pay["channel"] = "payment_processor"
    return payments


def load_all_demo_sources() -> dict[str, Any]:
    """Load demo QuickBooks + Bank + Payment Processor feeds."""
    invoices = load_quickbooks_invoices()
    payments = load_bank_payments() + load_processor_payments()
    return {
        "sources": [
            {"id": "quickbooks", "label": "QuickBooks", "mode": "demo", "count": len(invoices), "kind": "invoices"},
            {"id": "bank", "label": "Bank", "mode": "demo", "count": len(load_bank_payments()), "kind": "payments"},
            {
                "id": "payment_processor",
                "label": "Payment Processor",
                "mode": "demo",
                "count": len(load_processor_payments()),
                "kind": "payments",
            },
        ],
        "invoices": invoices,
        "payments": payments,
    }


def source_status() -> list[dict[str, Any]]:
    data = load_all_demo_sources()
    return [
        {**s, "status": "connected", "live": False}
        for s in data["sources"]
    ]
