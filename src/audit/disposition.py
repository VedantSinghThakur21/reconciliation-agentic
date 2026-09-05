from __future__ import annotations
"""
Accounting classification and audit trail for a reconciliation allocation.

LLM output never enters these functions — classification is pure arithmetic.
"""


def compute_accounting_treatment(
    status: str,
    amount_applied: float,
    invoice_amount: float,
    payment_amount: float,
    short_payment_tolerance: float = 500.0,
    short_payment_pct_cap: float = 2.0,
    match_tolerance: float = 0.01,
) -> str:
    """
    Deterministic accounting treatment per IND AS 109 / IFRS 9 / IFRS 15.

    Returns one of:
      full_settlement      — payment covered the invoice (AR extinguished)
      partial_receipt      — payment < invoice, shortfall > materiality threshold
      short_payment        — payment < invoice, shortfall within materiality
      over_receipt         — payment > invoice
      unallocated_suspense — no invoice match
    """
    if not invoice_amount or status == "unreconciled" or amount_applied <= match_tolerance:
        return "unallocated_suspense"

    shortfall = invoice_amount - amount_applied

    if shortfall <= match_tolerance:
        if payment_amount > invoice_amount + match_tolerance:
            return "over_receipt"
        return "full_settlement"

    pct_cap = invoice_amount * (short_payment_pct_cap / 100.0)
    materiality_limit = min(short_payment_tolerance, pct_cap)
    if shortfall <= materiality_limit:
        return "short_payment"
    return "partial_receipt"


def build_audit_trail(
    payment_amount: float,
    invoice_ref: str,
    invoice_face_amount: float,
    balance_owed: float,
    amount_applied: float,
    treatment: str | None,
) -> str:
    """
    Reperformance record for one allocation, appended to the proposal's reasoning.
    """
    balance_after = balance_owed - amount_applied
    return (
        f" [AUDIT: Payment ₹{payment_amount:,.2f} → Invoice {invoice_ref}"
        f" (face value ₹{invoice_face_amount:,.2f}, owed ₹{balance_owed:,.2f} before this allocation)"
        f" | Applied: ₹{amount_applied:,.2f}"
        f" | Invoice balance after: ₹{max(0, balance_after):,.2f}"
        f" | Treatment: {treatment}]"
    )
