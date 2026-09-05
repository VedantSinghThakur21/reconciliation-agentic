from __future__ import annotations
"""
Decision logging helpers for reconq-core.

Wraps disposition.build_audit_trail and stamps confidence / treatment onto
match result dicts. No hash-chain activity audit (SaaS platform feature).
"""

from typing import Any

from src.audit.disposition import build_audit_trail, compute_accounting_treatment
from src.rule_engine.strict_matcher import MatchResult


def stamp_strict_match(match: MatchResult) -> dict[str, Any]:
    """Convert a StrictMatcher MatchResult into an auditable decision record."""
    treatment = compute_accounting_treatment(
        status=match.status,
        amount_applied=match.amount_applied,
        invoice_amount=match.balance_before_allocation or match.invoice_face_amount,
        payment_amount=match.payment_amount or match.amount_applied,
    )
    trail = build_audit_trail(
        payment_amount=match.payment_amount or match.amount_applied,
        invoice_ref=match.invoice_ref or match.invoice_id,
        invoice_face_amount=match.invoice_face_amount,
        balance_owed=match.balance_before_allocation,
        amount_applied=match.amount_applied,
        treatment=treatment,
    )
    return {
        "paymentId": match.payment_id,
        "invoiceId": match.invoice_id,
        "status": match.status,
        "confidence": match.confidence,
        "tier": match.tier,
        "reasoning": f"{match.reasoning}{trail}",
        "amountApplied": match.amount_applied,
        "discrepancyAmount": match.discrepancy_amount,
        "accountingTreatment": treatment,
        "reviewReason": match.review_reason,
        "source": "strict_matcher",
    }


def stamp_proposal(proposal: dict[str, Any], invoices: dict[str, dict]) -> dict[str, Any]:
    """Stamp accounting treatment + audit trail onto a graph proposal dict."""
    inv_id = proposal.get("invoiceId")
    inv = invoices.get(inv_id) if inv_id else None
    balance_owed = float(
        proposal.get("balanceBeforeAllocation")
        or (inv.get("remaining_balance", inv.get("amount", 0)) if inv else 0)
        or 0
    )
    face = float(inv.get("amount", balance_owed) if inv else balance_owed)
    applied = float(proposal.get("amountApplied") or 0)
    pay_amt = float(proposal.get("paymentAmount") or applied)
    treatment = compute_accounting_treatment(
        status=proposal.get("status") or "pending_review",
        amount_applied=applied,
        invoice_amount=balance_owed or face,
        payment_amount=pay_amt,
    )
    ref = (inv.get("referenceNumber") if inv else None) or inv_id or "N/A"
    trail = build_audit_trail(
        payment_amount=pay_amt,
        invoice_ref=str(ref),
        invoice_face_amount=face,
        balance_owed=balance_owed or face,
        amount_applied=applied,
        treatment=treatment,
    )
    out = dict(proposal)
    out["accountingTreatment"] = treatment
    out["reasoning"] = f"{proposal.get('reasoning', '')}{trail}"
    out["source"] = proposal.get("source") or "graph"
    return out
