from __future__ import annotations
"""
Strict Invoice-Matching Engine — deterministic pre-screener.

Runs BEFORE the LangGraph AI agent. Handles cases that don't need LLM reasoning.
Unmatched payments are passed through to the AI matcher as-is.

Tiers (highest confidence first):
  T1  Exact reference number match                         confidence=1.00 → auto-approve
  T2  Exact amount + exact customer name match             confidence=0.97 → auto-approve
  T3  Exact amount + fuzzy name >= threshold               confidence=0.93 → auto-approve
  T4  Reference substring found in payment narration       confidence=0.91 → HitL review
  T5  Amount within tolerance + MatchMemory rule hit       confidence=0.90 → HitL review
"""

import logging
import re
from dataclasses import dataclass
from typing import Optional

from src.rule_engine.name_matching import name_similarity

logger = logging.getLogger(__name__)


@dataclass
class MatchResult:
    """Result from the strict matcher for a single payment."""
    payment_id:      str
    invoice_id:      str
    confidence:      float
    tier:            str
    reasoning:       str
    status:          str
    amount_applied:  float
    discrepancy_amount: float = 0.0
    balance_before_allocation: float = 0.0
    payment_amount:  float = 0.0
    invoice_face_amount: float = 0.0
    invoice_ref:     str = ""
    review_reason:   Optional[str] = None


def _normalize_ref(ref: str) -> str:
    """Lowercase, strip, remove common noise chars for reference comparison."""
    return re.sub(r"[\s\-_/]", "", (ref or "").lower().strip())


class StrictMatcher:
    """Deterministic 5-tier invoice matcher (single-tenant)."""

    def __init__(self, config: dict, memory_rules: list[dict]) -> None:
        self.config = config
        self.memory_rules = memory_rules
        self.amount_tolerance_pct: float = float(
            config.get("amount_tolerance_pct", 0.05)
        )
        self.fuzzy_name_threshold: float = float(
            config.get("fuzzy_name_threshold", 0.85)
        )
        self.auto_approve_threshold: float = float(
            config.get("auto_approve_threshold", 0.93)
        )
        self.materiality_threshold: Optional[float] = (
            float(config["materiality_threshold"])
            if config.get("materiality_threshold") is not None
            else None
        )

    def run(
        self,
        payments: list[dict],
        invoices: dict[str, dict],
    ) -> tuple[list[MatchResult], list[dict]]:
        strict_matches: list[MatchResult] = []
        leftover: list[dict] = []

        for payment in payments:
            result = self._match_payment(payment, invoices)
            if result:
                is_high_value = (
                    self.materiality_threshold is not None
                    and float(result.amount_applied or payment.get("amount", 0) or 0) > self.materiality_threshold
                )
                if is_high_value:
                    status = "pending_review"
                    result.reasoning = (
                        f"{result.reasoning} [Materiality Gate: Transaction amount exceeds "
                        f"materiality threshold of ₹{self.materiality_threshold:,.2f}; "
                        f"routed for HITL review.]"
                    )
                    result.review_reason = "materiality_threshold"
                else:
                    status = "reconciled" if result.confidence >= self.auto_approve_threshold else "pending_review"
                    if status == "pending_review" and result.review_reason is None:
                        result.review_reason = "low_confidence"
                result.status = status
                strict_matches.append(result)

                inv = invoices.get(result.invoice_id)
                if inv is not None:
                    balance_before = float(inv.get("remaining_balance", inv["amount"]))
                    result.balance_before_allocation = balance_before
                    result.payment_amount = float(payment.get("amount", 0) or 0)
                    result.invoice_face_amount = float(inv.get("amount", 0) or 0)
                    result.invoice_ref = inv.get("referenceNumber") or result.invoice_id
                    inv["remaining_balance"] = round(balance_before - result.amount_applied, 2)

                logger.info(
                    f"[StrictMatcher:{result.tier}] payment={payment['id']} "
                    f"→ invoice={result.invoice_id} conf={result.confidence:.2f} status={status}"
                )
            else:
                leftover.append(payment)

        logger.info(
            f"[StrictMatcher] {len(strict_matches)} strict matches, "
            f"{len(leftover)} sent to AI matcher"
        )
        return strict_matches, leftover

    def _parse_date(self, d_str: str | None):
        if not d_str:
            return None
        import datetime
        for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%m/%d/%Y", "%Y/%m/%d", "%d/%m/%Y"):
            try:
                return datetime.datetime.strptime(str(d_str).strip(), fmt).date()
            except ValueError:
                continue
        return None

    def _match_payment(
        self, payment: dict, invoices: dict[str, dict]
    ) -> Optional[MatchResult]:
        result = self._match_payment_raw(payment, invoices)
        if not result:
            return None

        inv = invoices.get(result.invoice_id)
        if not inv:
            return result

        p_date = self._parse_date(payment.get("date"))
        inv_date = self._parse_date(inv.get("invoiceDate"))

        if p_date and inv_date and p_date < inv_date:
            result.confidence = 0.70
            result.status = "pending_review"
            result.review_reason = "date_sequence_violation"
            result.reasoning = (
                f"Low Confidence: Date-sequence violation detected. "
                f"Payment date ({payment.get('date')}) is before Invoice date "
                f"({inv.get('invoiceDate')}). Requires manual review."
            )

        return result

    def _match_payment_raw(
        self, payment: dict, invoices: dict[str, dict]
    ) -> Optional[MatchResult]:
        p_ref = _normalize_ref(payment.get("reference", ""))
        p_amount = float(payment.get("amount", 0))
        p_sender = (payment.get("senderName") or "").lower().strip()

        # Single-tenant: open invoices with remaining balance only.
        open_invoices = [
            inv for inv in invoices.values()
            if inv.get("status") not in ("paid", "reconciled")
            and inv.get("source_system", "invoice") == "invoice"
            and float(inv.get("remaining_balance", inv.get("amount", 0)) or 0) > 0.01
        ]

        # ── Tier 1: Exact reference match ──────────────────────────────
        for inv in open_invoices:
            inv_ref = _normalize_ref(inv.get("referenceNumber", ""))
            inv_num = _normalize_ref(inv.get("invoiceNumber", ""))
            if inv_ref and p_ref and (p_ref == inv_ref or p_ref == inv_num):
                return MatchResult(
                    payment_id=payment["id"],
                    invoice_id=inv["id"],
                    confidence=1.00,
                    tier="T1",
                    reasoning=f"T1: Exact reference match — '{payment.get('reference')}'",
                    status="reconciled",
                    amount_applied=min(p_amount, float(inv.get("remaining_balance", inv["amount"]))),
                )

        # ── Tier 2: Exact amount + exact customer name ─────────────────
        for inv in open_invoices:
            inv_amount = float(inv.get("remaining_balance", inv["amount"]))
            inv_name = (inv.get("customerName") or "").lower().strip()
            if (
                abs(p_amount - inv_amount) < 0.01
                and p_sender
                and inv_name
                and p_sender == inv_name
            ):
                return MatchResult(
                    payment_id=payment["id"],
                    invoice_id=inv["id"],
                    confidence=0.97,
                    tier="T2",
                    reasoning=f"T2: Exact amount ({p_amount}) + exact name match ('{p_sender}')",
                    status="reconciled",
                    amount_applied=p_amount,
                )

        # ── Tier 3: Exact amount + fuzzy name >= threshold ─────────────
        for inv in open_invoices:
            inv_amount = float(inv.get("remaining_balance", inv["amount"]))
            inv_name = (inv.get("customerName") or "").lower().strip()
            similarity = name_similarity(p_sender, inv_name)
            if (
                abs(p_amount - inv_amount) < 0.01
                and p_sender
                and similarity >= self.fuzzy_name_threshold
            ):
                return MatchResult(
                    payment_id=payment["id"],
                    invoice_id=inv["id"],
                    confidence=0.93,
                    tier="T3",
                    reasoning=(
                        f"T3: Exact amount ({p_amount}) + fuzzy name match "
                        f"'{p_sender}' ≈ '{inv_name}' (similarity={similarity:.2f})"
                    ),
                    status="reconciled",
                    amount_applied=p_amount,
                )

        # ── Tier 4: Reference substring in payment narration ───────────
        p_narration = _normalize_ref(
            (payment.get("reference", "") + " " + (payment.get("senderName") or ""))
        )
        for inv in open_invoices:
            inv_ref = _normalize_ref(inv.get("referenceNumber", ""))
            inv_num = _normalize_ref(inv.get("invoiceNumber", ""))
            target = inv_ref or inv_num
            if target and len(target) >= 4 and target in p_narration:
                inv_amount = float(inv.get("remaining_balance", inv["amount"]))
                return MatchResult(
                    payment_id=payment["id"],
                    invoice_id=inv["id"],
                    confidence=0.91,
                    tier="T4",
                    reasoning=(
                        f"T4: Invoice reference '{inv.get('referenceNumber')}' "
                        f"found in payment narration"
                    ),
                    status="pending_review",
                    amount_applied=min(p_amount, inv_amount),
                    discrepancy_amount=max(0.0, p_amount - inv_amount),
                )

        # ── Tier 5: Amount within tolerance + MatchMemory rule ─────────
        for rule in self.memory_rules:
            pattern = rule.get("pattern_regex", "")
            target_customer = rule.get("target_invoice_customer", "")
            try:
                if pattern and p_sender and re.search(pattern, p_sender, re.IGNORECASE):
                    for inv in open_invoices:
                        inv_name = (inv.get("customerName") or "").lower().strip()
                        if name_similarity(target_customer.lower(), inv_name) >= 0.80:
                            inv_amount = float(inv.get("remaining_balance", inv["amount"]))
                            tolerance = inv_amount * self.amount_tolerance_pct
                            if abs(p_amount - inv_amount) <= tolerance:
                                return MatchResult(
                                    payment_id=payment["id"],
                                    invoice_id=inv["id"],
                                    confidence=0.90,
                                    tier="T5",
                                    reasoning=(
                                        f"T5: MatchMemory rule '{pattern}' → '{target_customer}'; "
                                        f"amount within {self.amount_tolerance_pct*100:.0f}% tolerance"
                                    ),
                                    status="pending_review",
                                    amount_applied=min(p_amount, inv_amount),
                                    discrepancy_amount=max(0.0, p_amount - inv_amount),
                                )
            except re.error:
                continue

        return None
