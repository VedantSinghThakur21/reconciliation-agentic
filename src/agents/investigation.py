from __future__ import annotations

from typing import Any


def classify_exception(decision: dict[str, Any], invoices: dict[str, dict]) -> dict[str, Any]:
    """Enrich a pending decision with investigation context for HITL."""
    reason = decision.get("reviewReason") or decision.get("status") or "exception"
    payment_id = decision.get("paymentId")
    invoice_id = decision.get("invoiceId")
    confidence = float(decision.get("confidence") or 0)

    category = "unmatched"
    if reason in ("duplicate", "already_allocated") or "duplicate" in str(decision.get("reasoning", "")).lower():
        category = "duplicate"
    elif invoice_id and confidence > 0:
        category = "low_confidence"
    elif invoice_id and abs(float(decision.get("discrepancyAmount") or 0)) > 0.01:
        category = "amount_mismatch"
    elif not invoice_id:
        category = "missing_record"

    candidates: list[dict[str, Any]] = []
    if invoice_id and invoice_id in invoices:
        inv = invoices[invoice_id]
        candidates.append({
            "invoiceId": invoice_id,
            "customerName": inv.get("customerName"),
            "amount": inv.get("amount"),
            "reference": inv.get("referenceNumber"),
            "score": confidence,
            "selected": True,
        })

    # Suggest nearest amount matches when unmatched
    if not candidates and payment_id:
        pay_amt = float(decision.get("paymentAmount") or decision.get("amountApplied") or 0)
        scored = []
        for inv in invoices.values():
            bal = float(inv.get("remaining_balance", inv.get("amount") or 0))
            if bal <= 0:
                continue
            delta = abs(bal - pay_amt) if pay_amt else bal
            scored.append((delta, inv))
        scored.sort(key=lambda x: x[0])
        for delta, inv in scored[:3]:
            candidates.append({
                "invoiceId": inv["id"],
                "customerName": inv.get("customerName"),
                "amount": inv.get("amount"),
                "reference": inv.get("referenceNumber"),
                "score": max(0.0, 1.0 - (delta / max(pay_amt, 1))),
                "selected": False,
            })

    return {
        **decision,
        "category": category,
        "candidates": candidates,
        "confidenceBreakdown": {
            "overall": confidence,
            "reference": 1.0 if decision.get("tier") == "T1" else (0.7 if decision.get("tier") else 0.0),
            "amount": 0.9 if float(decision.get("amountApplied") or 0) > 0 else 0.0,
            "name": 0.85 if decision.get("tier") in ("T2", "T3") else 0.4,
            "tier": decision.get("tier") or "none",
        },
    }


def investigate_pending(
    pending: list[dict[str, Any]],
    invoices: dict[str, dict],
) -> list[dict[str, Any]]:
    return [classify_exception(p, invoices) for p in pending]
