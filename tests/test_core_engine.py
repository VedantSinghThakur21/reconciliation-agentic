from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.rule_engine.name_matching import is_same_customer, name_similarity
from src.rule_engine.strict_matcher import StrictMatcher
from src.audit.disposition import build_audit_trail, compute_accounting_treatment
from src.verification.balance_check import validate_balanced_entry
from src.pipeline import run_pipeline

CONFIG = {"fuzzy_name_threshold": 0.65, "auto_approve_threshold": 0.93}


def _invoice(inv_id, customer, amount, ref="", number=""):
    return {
        "id": inv_id,
        "customerName": customer,
        "amount": amount,
        "remaining_balance": amount,
        "status": "open",
        "source_system": "invoice",
        "referenceNumber": ref,
        "invoiceNumber": number or inv_id,
    }


def _payment(pay_id, sender, amount, ref=""):
    return {
        "id": pay_id,
        "senderName": sender,
        "amount": amount,
        "reference": ref,
        "date": None,
    }


def test_name_similarity_truncation_and_suffix():
    assert is_same_customer("GLOBAL SOLUTION", "Global Solutions Pvt Ltd")
    assert is_same_customer("DYNAMIC ENTERPR", "Dynamic Enterprises")
    assert not is_same_customer("GLOBAL SOLUTION", "Dynamic Enterprises")
    assert name_similarity("TECH INDUSTRIES", "Royal Industries") < 0.65


def test_strict_matcher_no_double_allocation():
    invoices = {"INV-1": _invoice("INV-1", "Acme Corp", 54705.09)}
    payments = [
        _payment("PAY-1", "ACME CORP", 54705.09),
        _payment("PAY-2", "ACME CORP", 54705.09),
    ]
    matches, leftover = StrictMatcher(CONFIG, []).run(payments, invoices)
    claims = [m for m in matches if m.invoice_id == "INV-1"]
    assert len(claims) == 1
    assert len(matches) + len(leftover) == 2


def test_strict_match_audit_trail():
    invoices = {"INV-1": _invoice("INV-1", "Acme Corp", 10000.00, ref="REF-9")}
    matches, _ = StrictMatcher(CONFIG, []).run(
        [_payment("PAY-1", "ACME CORP", 10000.00)], invoices
    )
    sr = matches[0]
    treatment = compute_accounting_treatment(
        status=sr.status,
        amount_applied=sr.amount_applied,
        invoice_amount=sr.balance_before_allocation,
        payment_amount=sr.payment_amount,
    )
    assert treatment == "full_settlement"
    trail = build_audit_trail(
        payment_amount=sr.payment_amount,
        invoice_ref=sr.invoice_ref,
        invoice_face_amount=sr.invoice_face_amount,
        balance_owed=sr.balance_before_allocation,
        amount_applied=sr.amount_applied,
        treatment=treatment,
    )
    assert "[AUDIT:" in trail and "full_settlement" in trail


def test_validate_balanced_entry():
    assert validate_balanced_entry([
        {"debit": 1000.00, "credit": 0},
        {"debit": 0, "credit": 1000.00},
    ])
    assert not validate_balanced_entry([
        {"debit": 1000.00, "credit": 0},
        {"debit": 0, "credit": 999.00},
    ])


def test_pipeline_on_sample_csv():
    root = Path(__file__).resolve().parents[1]
    result = run_pipeline(
        root / "data" / "synthetic" / "sample_invoices.csv",
        root / "data" / "synthetic" / "sample_payments.csv",
        use_ai_graph=False,
    )
    summary = result.summary()
    assert summary["total_decisions"] >= 3
    assert summary["auto_reconciled"] >= 2
    assert len(result.review_queue.pending()) >= 1
