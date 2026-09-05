from __future__ import annotations
"""
Reconciliation LangGraph Nodes — Financial-Grade Matching Logic

Matching Strategy (in priority order):
  1. Deterministic Reference Match — exact ref match → auto-approve
  2. Memory Matcher — learned sender→customer pattern → auto-approve
  3. Amount-Based Pre-filter — when sender is unknown, try exact/near amount match
  4. AI Semantic Matcher — LLM reasoning over customer-scoped (or amount-scoped) invoices

Customer Isolation Rules:
  - When sender name IS known: only match invoices for that customer (fuzzy name)
  - When sender name is UNKNOWN: AI matches by amount/reference, capped confidence → pending_review
  - Cross-customer auto-approval is NEVER allowed
"""

import asyncio
import json
import logging
import re
from difflib import SequenceMatcher
from typing import Optional

from .state import ReconciliationState
from src.audit.disposition import build_audit_trail, compute_accounting_treatment
from src.rule_engine.name_matching import (
    MIN_TRUNCATION_PREFIX,
    is_same_customer,
    name_similarity,
    normalize_name,
    strip_legal_suffix,
)
from src.llm import get_llm_provider

# Single-tenant extract: entity isolation is a no-op (multi-entity is future work).
ENTITY_UNRESOLVED = "entity_unresolved"

def entity_of(record: dict):
    return record.get("entity_id") or "default"

def same_entity(payment: dict, invoice: dict) -> bool:
    return True

def has_entity(payment: dict) -> bool:
    return True

logger = logging.getLogger(__name__)

from typing import Any

# Dynamic config helper
def _get_config_param(config: dict, name: str, default: Any) -> Any:
    if not isinstance(config, dict):
        return default
    # Support both snake_case and camelCase keys
    alias_map = {
        "fuzzy_name_threshold": ["fuzzy_name_threshold", "fuzzyNameThreshold"],
        "auto_approve_confidence": ["auto_approve_confidence", "autoApproveConfidence", "auto_approve_threshold", "autoApproveThreshold"],
        "amount_tolerance_pct": ["amount_tolerance_pct", "amountTolerancePct"],
        "no_sender_confidence_cap": ["no_sender_confidence_cap", "noSenderConfidenceCap"],
        "short_payment_tolerance": ["short_payment_tolerance", "shortPaymentTolerance"],
        "short_payment_pct_cap": ["short_payment_pct_cap", "shortPaymentPctCap"],
    }
    keys_to_check = alias_map.get(name, [name])
    for k in keys_to_check:
        if k in config and config[k] is not None:
            return config[k]
    return default


# ─────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────


# Accounting classification lives in one place (see disposition.py) so the strict
# pre-screener, which never reaches this node, stamps its matches identically.
_compute_accounting_treatment = compute_accounting_treatment


# Customer-name matching lives in one place (see name_matching.py) — the strict
# matcher and this module previously each had their own, disagreeing version.
# Kept as module-level aliases so existing call sites and tests are unaffected.
_strip_legal_suffix = strip_legal_suffix
_normalize_name = normalize_name
_name_similarity = name_similarity
_is_same_customer = is_same_customer


def _is_zero_signal_payment(payment: dict) -> bool:
    """
    Checks if a payment has no matching signal (e.g. senderName/reference are empty or generic like 'unknown').
    """
    sender = str(payment.get("senderName") or "").strip().lower()
    ref = str(payment.get("reference") or "").strip().lower()

    # Generic placeholders
    generic_terms = {"unknown", "n/a", "none", "null", "deposit", "payment", "bank transfer", "wire transfer"}

    sender_empty = not sender or sender in generic_terms
    ref_empty = not ref or ref in generic_terms

    # Also if sender is purely numeric, it has no customer name signal
    if sender.replace("-", "").replace(".", "").replace(" ", "").isdigit():
        sender_empty = True

    return sender_empty and ref_empty


def _get_customer_invoices(sender: str, all_invoices: dict, fuzzy_threshold: float = 0.65) -> list[dict]:
    """
    Return the open invoices of the single customer that best matches sender.

    A payment has exactly one payer, so testing each invoice independently and
    keeping every one above the threshold is wrong: two unrelated customers can
    both clear it — "PREMIER TRADERS" scores 1.00 against "Premier Traders" and
    still 0.67 against "Supreme Trading Co" — which hands a stranger's invoices
    to the caller and invites cross-customer allocation. Resolve the customer
    first, then take only that customer's invoices, the same two-step the
    payment grouping already applies.
    """
    def _is_open_invoice(inv: dict) -> bool:
        return inv.get("source_system") == "invoice" and float(inv.get("remaining_balance", 0)) > 0

    best_customer = None
    best_score = 0.0
    for inv in all_invoices.values():
        if not _is_open_invoice(inv):
            continue
        customer = inv.get("customerName") or ""
        score = _name_similarity(sender, customer)
        if score >= fuzzy_threshold and score > best_score:
            best_score = score
            best_customer = customer

    if not best_customer:
        return []

    matched = [
        inv for inv in all_invoices.values()
        if _is_open_invoice(inv)
        and (inv.get("customerName") or "").lower() == best_customer.lower()
    ]
    # Sort oldest due date first (pay oldest debts first — standard practice)
    matched.sort(key=lambda x: x.get("dueDate") or "9999-12-31")
    return matched


def _get_amount_matched_invoices(pay_amount: float, all_invoices: dict, amount_tolerance: float = 0.05) -> list[dict]:
    """
    When sender is unknown, find invoices whose remaining balance is within
    amount_tolerance of the payment amount. Returns sorted by closeness.
    """
    candidates = []
    for inv in all_invoices.values():
        if inv.get("source_system") != "invoice":
            continue
        rem = float(inv.get("remaining_balance", 0))
        if rem <= 0:
            continue
        diff_pct = abs(pay_amount - rem) / rem if rem > 0 else 1.0
        if diff_pct <= amount_tolerance or abs(pay_amount - rem) <= 1.0:
            candidates.append((diff_pct, inv))

    candidates.sort(key=lambda x: x[0])
    return [inv for _, inv in candidates]


def _extract_customer_from_description(description: str, all_invoices: dict) -> list[dict]:
    """
    Scan the payment narration/description for tokens that match known customer names.
    This is the key fallback when `sender_name` is null but the narration carries
    partial company identifiers (e.g. "NEFT/PAYMENT/DE/2702" may contain fragments).

    Matching strategy:
    1. Tokenise the description into meaningful words (≥4 chars, skip stop words)
    2. For each customer name in the invoice pool, check if any significant token
       from the description appears in the customer name (substring match)
    3. Additionally run fuzzy similarity between the entire description and each customer name
    Returns candidate invoices sorted by match confidence (desc), oldest due date first.
    """
    if not description:
        return []

    STOP_WORDS = {
        "neft", "rtgs", "imps", "upi", "online", "payment", "transfer", "credit",
        "debit", "bank", "hdfc", "icici", "sbi", "axis", "kotak", "from", "to",
        "by", "via", "ref", "txn", "chq", "dep", "cr", "dr", "ach", "ecs",
        "ltd", "pvt", "inc", "corp", "llp", "atm", "pos", "the", "and", "for"
    }

    # Clean and tokenise description
    tokens = re.findall(r'[a-zA-Z]{4,}', description.upper())
    meaningful_tokens = [t for t in tokens if t.lower() not in STOP_WORDS]

    scored: list[tuple[float, dict]] = []

    for inv in all_invoices.values():
        if inv.get("source_system") != "invoice":
            continue
        if float(inv.get("remaining_balance", 0)) <= 0:
            continue

        customer = inv.get("customerName", "")
        if not customer:
            continue

        customer_upper = customer.upper()
        customer_tokens = re.findall(r'[a-zA-Z]{4,}', customer_upper)
        customer_tokens = [t for t in customer_tokens if t.lower() not in STOP_WORDS]

        # Signal 1: Token overlap — description tokens that appear in customer name
        overlap = sum(1 for t in meaningful_tokens if t in customer_upper)
        # Signal 2: Reverse overlap — customer tokens that appear in description
        reverse_overlap = sum(1 for ct in customer_tokens if ct in description.upper())
        # Signal 3: Substring — does description contain a significant part of customer name?
        stripped_cust = _strip_legal_suffix(customer)
        substring_hit = 1.0 if (stripped_cust and len(stripped_cust) >= 4
                                 and stripped_cust.upper() in description.upper()) else 0.0

        # Signal 4: Direct Invoice ID or Reference match in description
        inv_id = inv.get("id", "").upper()
        inv_ref = inv.get("reference", "").upper() if inv.get("reference") else ""
        desc_upper = description.upper()
        direct_ref_hit = 0.0
        if inv_id and inv_id in desc_upper:
            direct_ref_hit += 3.0
        if inv_ref and inv_ref in desc_upper:
            direct_ref_hit += 2.0

        score = (overlap * 0.4) + (reverse_overlap * 0.4) + substring_hit + direct_ref_hit

        if score > 0:
            scored.append((score, inv))
            logger.debug(
                f"[DescMatch] '{customer}' score={score:.2f} "
                f"(overlap={overlap}, rev={reverse_overlap}, substr={substring_hit}) "
                f"for description: '{description[:80]}'"
            )

    if not scored:
        return []

    # Sort by score desc, then by oldest due date
    scored.sort(key=lambda x: (-x[0], x[1].get("dueDate") or "9999-12-31"))
    top_score = scored[0][0]

    # Only return invoices with a meaningful match (within 50% of the top score)
    filtered = [inv for score, inv in scored if score >= top_score * 0.5]
    return filtered
async def node_init(state: ReconciliationState) -> ReconciliationState:
    """Initializes proposals list and results lists for batch reconciliation."""
    state["current_match_proposals"] = []
    state["reconciliation_results"] = []
    state["pending_reviews"] = []
    state["original_balances"] = {
        inv_id: float(inv.get("remaining_balance", 0.0))
        for inv_id, inv in state.get("open_invoices", {}).items()
    }
    state["in_run_allocated"] = {}  # Cumulative allocation ledger for this run
    return state


async def node_advance_classification(state: ReconciliationState) -> ReconciliationState:
    """No-op in reconq-core: customer advances / PO context are out of scope (AR cash-app only)."""
    return state


async def node_rule_matcher(state: ReconciliationState) -> ReconciliationState:
    """
    Stage 1 — Deterministic Reference Matching (Batch Mode).
    Runs exact reference number match for all payments.
    Matches are placed in current_match_proposals and the invoice balance is updated.
    """
    remaining_payments = []
    proposals = state.get("current_match_proposals", []) or []

    for payment in state.get("unreconciled_payments", []):
        pay_amount = float(payment["amount"])
        ref = (payment.get("reference") or "").strip().lower()

        if not ref:
            remaining_payments.append(payment)
            continue

        ref_clean_alphanum = re.sub(r'[^a-z0-9]', '', ref)
        matched = False

        for inv_id, inv in state["open_invoices"].items():
            if inv.get("source_system") != "invoice":
                continue
            # A shared reference number across entities must not settle the wrong one.
            if not same_entity(payment, inv):
                continue
            inv_ref = (inv.get("referenceNumber") or "").strip().lower()
            inv_num = (inv.get("invoiceNumber") or "").strip().lower()
            rem_bal = float(inv.get("remaining_balance", 0))

            if rem_bal <= 0:
                continue

            reason = ""
            if inv_ref and (ref == inv_ref or ref in inv_ref or inv_ref in ref):
                matched = True
                reason = f"Deterministic match: payment reference '{payment.get('reference')}' matches invoice reference '{inv.get('referenceNumber')}' exactly."
            elif inv_num:
                inv_num_alphanum = re.sub(r'[^a-z0-9]', '', inv_num)
                if (inv_num in ref or ref in inv_num or
                    (inv_num_alphanum and inv_num_alphanum in ref_clean_alphanum) or
                    (ref_clean_alphanum and ref_clean_alphanum in inv_num_alphanum)):
                    matched = True
                    reason = f"Deterministic match: payment reference '{payment.get('reference')}' matches invoice number '{inv.get('invoiceNumber')}' exactly."

            if matched:
                applied = min(pay_amount, rem_bal)
                discrepancy = round(max(0.0, pay_amount - rem_bal), 2)
                stat = "reconciled" if discrepancy <= 0.01 else "partial"

                proposals.append({
                    "paymentId": payment["id"],
                    "invoiceId": inv_id,
                    "status": stat,
                    "confidence": 1.0,
                    "reasoning": f"{reason} Applied {applied:.2f}.",
                    "discrepancyAmount": discrepancy,
                    "amountApplied": applied,
                    "balanceBeforeAllocation": rem_bal,
                })
                # Immediately update balance for other matches in the same run
                state["open_invoices"][inv_id]["remaining_balance"] = round(rem_bal - applied, 2)
                logger.info(f"[RuleMatcher] ✅ Exact match (Batch): {payment['id']} → {inv_id}")
                break

        if not matched:
            remaining_payments.append(payment)

    state["current_match_proposals"] = proposals
    return state


async def node_memory_matcher(state: ReconciliationState) -> ReconciliationState:
    """
    Stage 2 — Self-Learning Memory Matching (Batch Mode).
    Uses historically learned sender→customer mappings for all remaining payments.
    """
    proposals = state.get("current_match_proposals", []) or []
    matched_pids = {p["paymentId"] for p in proposals}

    for payment in state.get("unreconciled_payments", []):
        if payment["id"] in matched_pids:
            continue

        sender = (payment.get("senderName") or "").strip()
        pay_amount = float(payment["amount"])

        if not sender:
            continue

        matched_rule = None
        for rule in state.get("match_memory_rules", []):
            if re.search(rule["pattern_regex"], sender, re.IGNORECASE):
                matched_rule = rule
                break

        if matched_rule:
            target_customer = matched_rule["target_invoice_customer"]

            # Get oldest-first invoices for this specific customer
            matching = [
                (inv_id, inv)
                for inv_id, inv in state["open_invoices"].items()
                if (
                    inv.get("source_system") == "invoice"
                    and inv.get("customerName", "").lower() == target_customer.lower()
                    and float(inv.get("remaining_balance", 0)) > 0
                )
            ]
            matching.sort(key=lambda x: x[1].get("dueDate") or "9999-12-31")

            if not matching:
                continue

            remaining_payment = pay_amount
            for inv_id, inv in matching:
                if remaining_payment <= 0:
                    break
                rem_bal = float(inv["remaining_balance"])
                applied = min(remaining_payment, rem_bal)
                discrepancy = round(max(0.0, remaining_payment - rem_bal), 2)
                stat = "reconciled" if discrepancy <= 0.01 else "partial"

                proposals.append({
                    "paymentId": payment["id"],
                    "invoiceId": inv_id,
                    "status": stat,
                    "confidence": float(matched_rule["confidence_weight"]),
                    "reasoning": (
                        f"Memory match: '{sender}' → '{target_customer}'. "
                        f"Applied {applied:.2f} to invoice {inv.get('invoiceNumber') or inv.get('referenceNumber') or inv_id}."
                    ),
                    "discrepancyAmount": discrepancy,
                    "amountApplied": applied,
                    "balanceBeforeAllocation": rem_bal,
                })
                # Immediately update balance
                state["open_invoices"][inv_id]["remaining_balance"] = round(rem_bal - applied, 2)
                remaining_payment -= applied

            logger.info(f"[MemoryMatcher] ✅ Memory match (Batch): {payment['id']} for '{sender}' → '{target_customer}'")
            matched_pids.add(payment["id"])

    state["current_match_proposals"] = proposals
    return state


def _group_payments_by_customer(payments: list[dict], open_invoices: dict, fuzzy_threshold: float = 0.65) -> dict[str, dict]:
    """
    Groups payments by customer.
    Returns a dict mapping customer_name -> {
        "payments": list[dict],
        "invoices": list[dict],
        "group_type": "customer" | "amount_proximity" | "no_match"
    }
    """
    groups = {}

    for payment in payments:
        sender = (payment.get("senderName") or "").strip()
        description = (payment.get("description") or payment.get("reference") or "").strip()
        pay_amount = float(payment["amount"])

        # Entity guard. Every candidate pool below is derived from this scoped view,
        # so a customer holding open invoices in two legal entities yields one group
        # per entity instead of one pool the FIFO waterfall can drain across the
        # boundary. A payment with no resolvable entity gets an empty pool and is
        # routed to review by the caller — it is unattributable, not universal.
        entity_invoices = {
            inv_id: inv for inv_id, inv in open_invoices.items()
            if same_entity(payment, inv)
        }
        all_customers = {
            inv["customerName"] for inv in entity_invoices.values()
            if inv.get("source_system") == "invoice"
            and float(inv.get("remaining_balance", 0)) > 0
            and inv.get("customerName")
        }

        matched_customer = None

        # Tier 1: Fuzzy sender name match
        if sender:
            best_cust = None
            best_score = 0.0
            for cust in all_customers:
                score = _name_similarity(sender, cust)
                if score >= fuzzy_threshold and score > best_score:
                    best_score = score
                    best_cust = cust
            if best_cust:
                matched_customer = best_cust

        # Tier 2: Search description / narration keyword matching
        if not matched_customer and description:
            desc_candidates = _extract_customer_from_description(description, entity_invoices)
            if desc_candidates:
                matched_customer = desc_candidates[0].get("customerName")

        if matched_customer:
            # Keyed by entity as well as customer; `customer_name` carries the clean
            # name on to the LLM prompt and the logs.
            group_key = f"{matched_customer}::{entity_of(payment)}"
            if group_key not in groups:
                cust_invoices = [
                    inv for inv in entity_invoices.values()
                    if inv.get("source_system") == "invoice"
                    and inv.get("customerName", "").lower() == matched_customer.lower()
                    and float(inv.get("remaining_balance", 0)) > 0
                ]
                groups[group_key] = {
                    "payments": [],
                    "invoices": cust_invoices,
                    "group_type": "customer",
                    "customer_name": matched_customer,
                }
            groups[group_key]["payments"].append(payment)
        else:
            # Tier 3: Amount proximity matching (±5%)
            amt_candidates = _get_amount_matched_invoices(pay_amount, entity_invoices, amount_tolerance=0.05)
            if amt_candidates:
                top_cust = amt_candidates[0].get("customerName") or "Unknown"
                group_key = f"Amount Proximity Match - {top_cust}::{entity_of(payment)}"
                if group_key not in groups:
                    groups[group_key] = {
                        "payments": [],
                        "invoices": amt_candidates,
                        "group_type": "amount_proximity",
                        "customer_name": f"Amount Proximity Match - {top_cust}",
                    }
                groups[group_key]["payments"].append(payment)
            else:
                # No match found at all
                group_key = "Unreconciled / No Matches"
                if group_key not in groups:
                    groups[group_key] = {
                        "payments": [],
                        "invoices": [],
                        "group_type": "no_match"
                    }
                groups[group_key]["payments"].append(payment)
                
    logger.info(f"[AIMatcher] Grouping: {len(groups)} customer groups")
    return groups


# Module-level constant to throttle concurrent LLM requests and stay within rate limits.
AI_MATCHER_CONCURRENCY_LIMIT = 5

# Fixed confidence for fifo_apply proposals — see _allocate_fifo_deterministic.
# Not LLM-scored: the customer is already confirmed and the invoice pick is
# deterministic, so there's no real judgment left to assign a variable number to.
FIFO_APPLY_CONFIDENCE = 0.85


def _allocate_fifo_deterministic(
    payments: list[dict],
    invoices: list[dict],
    decisions_by_payment: dict[str, dict],
    shared_balances: dict[str, float],
) -> list[dict]:
    """
    Deterministic FIFO waterfall allocator (Rules B/C/E/F computed in code, not by the LLM).

    The LLM's job ends at classification: for each payment, WHICH invoice(s) does the
    evidence point to (matchType + targetInvoiceIds)? This function does the arithmetic —
    running balances, FIFO tie-breaks, discrepancy amounts. Same inputs always produce the
    same output, which is what makes reconciliation reproducible across runs/tenants for
    identical data (the LLM's token sampling was previously doing this math itself, which
    is why identical payments/invoices produced different allocations run to run).

    Mutates `shared_balances` in place (cross-group ledger owned by the caller).
    """
    MATCH_TOLERANCE = 0.01
    SHORT_PAY_TOLERANCE = 500.0

    invoice_by_id = {inv["id"]: inv for inv in invoices}
    valid_inv_ids = set(invoice_by_id.keys())
    id_by_ref: dict[str, str] = {}
    for inv in invoices:
        if inv.get("referenceNumber"):
            id_by_ref.setdefault(inv["referenceNumber"], inv["id"])
        if inv.get("invoiceNumber"):
            id_by_ref.setdefault(inv["invoiceNumber"], inv["id"])

    # Oldest-due-date-first, tie-broken by id for full determinism when dates match.
    fifo_queue = sorted(
        valid_inv_ids,
        key=lambda iid: (
            invoice_by_id[iid].get("dueDate") or invoice_by_id[iid].get("invoiceDate") or "9999-99-99",
            iid,
        ),
    )
    # Payments applied in receipt order, tie-broken by id — not LLM/DB iteration order.
    ordered_payments = sorted(payments, key=lambda p: (p.get("date") or "", p["id"]))

    def resolve(raw_id):
        if raw_id in valid_inv_ids:
            return raw_id
        return id_by_ref.get(raw_id)

    proposals: list[dict] = []

    for payment in ordered_payments:
        pay_id = payment["id"]
        pay_amount = float(payment["amount"])
        decision = decisions_by_payment.get(pay_id) or {}
        match_type = decision.get("matchType") or "fifo_apply"
        reasoning = (decision.get("reasoning") or "").strip()

        # fifo_apply has nothing left for the LLM to score: customer identity is already
        # confirmed deterministically (fuzzy name match, before the LLM saw this payment),
        # and the invoice pick is deterministic too (oldest-due-date FIFO, computed below).
        # Asking the LLM for a confidence number here just invites sampling noise on a
        # routine case — fix the number in code instead of re-calibrating a prompt forever.
        if match_type == "fifo_apply":
            confidence = FIFO_APPLY_CONFIDENCE
        else:
            confidence = float(decision.get("confidence") or 0.0)

        if match_type in ("no_match", "advance_payment", "manual_review"):
            proposals.append({
                "paymentId": pay_id,
                "invoiceId": None,
                "status": "unreconciled",
                "confidence": confidence,
                "reasoning": reasoning or f"No allocation ({match_type}).",
                "amountApplied": 0.0,
                "discrepancyAmount": None,
                "match_type": match_type,
            })
            continue

        target_ids: list[str] = []
        if match_type in ("exact_reference", "corroborated_match", "consolidated_batch"):
            for raw in decision.get("targetInvoiceIds") or []:
                inv_id = resolve(raw)
                if inv_id and inv_id not in target_ids:
                    target_ids.append(inv_id)

        remaining = round(pay_amount, 2)
        visited: set[str] = set()
        emitted: list[dict] = []

        def take(inv_id: str, evidence_backed: bool) -> None:
            nonlocal remaining
            if inv_id in visited:
                return
            balance_before = shared_balances.get(inv_id, 0.0)
            if balance_before <= MATCH_TOLERANCE or remaining <= MATCH_TOLERANCE:
                return
            applied = round(min(remaining, balance_before), 2)
            shared_balances[inv_id] = round(balance_before - applied, 2)
            remaining = round(remaining - applied, 2)
            visited.add(inv_id)

            shortfall = round(balance_before - applied, 2)
            inv_amount = float(invoice_by_id[inv_id].get("amount", balance_before))
            materiality = min(SHORT_PAY_TOLERANCE, inv_amount * 0.02)
            status = "reconciled" if shortfall <= materiality else "partial"
            inv_label = (
                invoice_by_id[inv_id].get("invoiceNumber")
                or invoice_by_id[inv_id].get("referenceNumber")
                or inv_id
            )

            emitted.append({
                "paymentId": pay_id,
                "invoiceId": inv_id,
                "status": status,
                "confidence": confidence,
                "reasoning": (
                    f"{reasoning} [Deterministic FIFO: applied ₹{applied:,.2f} to invoice {inv_label} "
                    f"(balance ₹{balance_before:,.2f} → ₹{shared_balances[inv_id]:,.2f}); matchType={match_type}]"
                ).strip(),
                "amountApplied": applied,
                "discrepancyAmount": shortfall,
                "balanceBeforeAllocation": balance_before,
                # The strategy that picked THIS invoice, not the strategy the LLM
                # named for the payment. A payment matched by reference whose
                # remainder spills into the FIFO queue must not record the spill
                # as reference-backed — the evidence only covers the named invoice.
                "match_type": match_type if evidence_backed else "fifo_apply",
            })

        # 1. Evidence-backed targets first (reference match / corroborated / consolidated batch).
        for inv_id in target_ids:
            take(inv_id, evidence_backed=True)
        # 2. Any remainder (or a plain fifo_apply payment) spills into the FIFO queue.
        if remaining > MATCH_TOLERANCE:
            for inv_id in fifo_queue:
                take(inv_id, evidence_backed=False)

        if not emitted:
            proposals.append({
                "paymentId": pay_id,
                "invoiceId": None,
                "status": "unreconciled",
                "confidence": 0.0,
                "reasoning": (reasoning + " [No open invoice balance available to apply.]").strip(),
                "amountApplied": 0.0,
                "discrepancyAmount": None,
                "match_type": match_type,
            })
        else:
            proposals.extend(emitted)

    return proposals


async def _reconcile_single_group(
    customer_name: str,
    group: dict,
    llm: Any,
    tenant_id: Optional[str],
    shared_balances: dict[str, float],
    sem: asyncio.Semaphore,
) -> list[dict]:
    """
    Helper coroutine to reconcile a single customer group.
    """
    payments = group["payments"]
    invoices = group["invoices"]
    group_type = group["group_type"]

    # Zero-Signal Bypass Heuristic or No Matches
    if group_type == "no_match" or not invoices:
        group_proposals = []
        for payment in payments:
            logger.info(f"[AIMatcher] [Group: '{customer_name}'] Bypassing payment {payment['id']} (No candidates).")
            group_proposals.append({
                "paymentId": payment["id"],
                "invoiceId": None,
                "status": "unreconciled",
                "confidence": 0.0,
                "reasoning": f"No candidate invoices found matching sender '{payment.get('senderName') or 'UNKNOWN'}'.",
                "discrepancyAmount": None,
                "amountApplied": 0.0,
                "ai_reasoning": "### Bypassed\nNo candidate invoices matched this payment."
            })
        return group_proposals

    # Check if all payments in group are zero-signal
    all_zero_signal = True
    for payment in payments:
        if not _is_zero_signal_payment(payment):
            all_zero_signal = False
            break

    if all_zero_signal:
        group_proposals = []
        for payment in payments:
            logger.info(f"[AIMatcher] [Group: '{customer_name}'] Bypassing zero-signal payment {payment['id']}.")
            group_proposals.append({
                "paymentId": payment["id"],
                "invoiceId": None,
                "status": "unreconciled",
                "confidence": 0.0,
                "reasoning": f"Bypassed: No matching signals (numeric sender/ref or standard bank noise).",
                "discrepancyAmount": None,
                "amountApplied": 0.0,
                "ai_reasoning": "### Bypassed\nThis transaction contains no letters or actual names to match."
            })
        return group_proposals

    ai_payments = [
        {
            "id": p["id"],
            "amount": float(p["amount"]),
            "currency": p["currency"],
            "date": p["date"],
            "reference": p.get("reference"),
            "senderName": p.get("senderName")
        }
        for p in payments
    ]

    ai_invoices = [
        {
            "id": inv["id"],
            "customerName": inv.get("customerName"),
            "amount": float(inv["amount"]),
            "remaining_balance": float(inv["remaining_balance"]),
            "currency": inv["currency"],
            "dueDate": inv.get("dueDate"),
            "invoiceNumber": inv.get("invoiceNumber"),
            "referenceNumber": inv.get("referenceNumber")
        }
        for inv in invoices
    ]

    logger.info(f"[AIMatcher] [Group: '{customer_name}'] Starting concurrent AI matching with {len(payments)} payments and {len(invoices)} invoices.")

    # ── group_type == "customer": deterministic-allocation path ──────────────
    # The LLM classifies WHICH invoice(s) each payment's evidence points to.
    # It never computes amountApplied/running balances — _allocate_fifo_deterministic
    # does that arithmetically, which is what makes identical data reconcile to
    # identical results on every run, for every tenant.
    if group_type == "customer":
        system_prompt = f"""You are an expert financial reconciliation engine operating under Generally Accepted Accounting Principles (GAAP) and Indian Accounting Standards (Ind AS).
Your task is to classify how each payment should be matched to this customer's open invoices.

You are the MATCHING decision engine, not the accounting engine. You decide WHICH invoice(s)
the evidence points to. A separate deterministic system computes exact amounts and running
balances — you must NOT compute amountApplied, discrepancyAmount, or balances yourself.

Never invent invoices outside the supplied data.
When evidence is insufficient or conflicting, return matchType "manual_review" rather than guessing.

═══════════════════════════════════════════════════
CONTEXT
═══════════════════════════════════════════════════

CUSTOMER ACCOUNT: {customer_name}

PAYMENTS TO CLASSIFY:
{json.dumps(ai_payments, indent=2)}

OPEN INVOICES (remaining_balance reflects prior allocations from payments already applied this run):
{json.dumps(ai_invoices, indent=2)}

═══════════════════════════════════════════════════
MATCH TYPES (choose exactly one per payment)
═══════════════════════════════════════════════════

"exact_reference"
  Payment reference matches an invoice number / reference number / PO number.
  Normalize: strip whitespace, hyphens, slashes, leading zeros. Case-insensitive.
  targetInvoiceIds: the single matched invoice id. confidence ≥ 0.95.

"corroborated_match"
  Exact or near-exact amount match AND payment date on/after invoice date AND
  sender name matches. targetInvoiceIds: the single matched invoice id. confidence ≥ 0.90.

"consolidated_batch"
  A single payment is clearly intended for a SPECIFIC, evidence-backed set of two or
  more invoices (e.g. remittance narration or reference lists multiple invoice numbers).
  targetInvoiceIds: those invoice ids, in the order the customer intends them paid.
  Do NOT use this type just because a payment is large — only with real evidence of
  which specific invoices are meant.

"fifo_apply"
  No reference/name/amount evidence points to one specific invoice — this is a plain
  partial or on-account payment for this customer. targetInvoiceIds: leave empty; the
  deterministic system applies it oldest-due-date-first automatically. This is the
  correct type for the common, ROUTINE case "customer paid X, apply it to whatever
  they owe, oldest first." confidence is ignored for this type (fixed by the system) —
  put any value, it has no effect.

"advance_payment"
  Payment received with no matching invoice at all, or payment date is significantly
  before the earliest invoice date for this customer. reasoning must state "Customer
  advance / on-account receipt — no matching invoice exists". confidence 0.0.

"no_match"
  No logical link to this customer at all. confidence 0.0.

"manual_review"
  Higher-priority and lower-priority evidence conflict (e.g. reference points to one
  invoice while amount strongly indicates another), or currency mismatch. Explain the
  conflict in reasoning.

═══════════════════════════════════════════════════
CONFIDENCE SCORING RUBRIC
═══════════════════════════════════════════════════

  0.95–1.00  Exact reference match
  0.90–0.94  Amount + date + customer name all corroborate
  0.75–0.89  consolidated_batch with solid narration evidence
  0.50–0.74  manual_review: genuine conflicting or contradictory evidence
             (e.g. reference points to one invoice, amount strongly suggests another)
  0.00       no_match / advance_payment / fifo_apply (ignored — see above)

═══════════════════════════════════════════════════
OUTPUT FORMAT
═══════════════════════════════════════════════════

{{
  "allocationSummary": "Step-by-step: for EACH payment, which evidence you evaluated and which matchType you chose and why.",
  "decisions": [
    {{
      "paymentId": "payment_id",
      "matchType": "exact_reference" | "corroborated_match" | "consolidated_batch" | "fifo_apply" | "advance_payment" | "no_match" | "manual_review",
      "targetInvoiceIds": ["invoice_id", "..."],
      "confidence": 0.0_to_1.0,
      "reasoning": "Concise explanation, referencing invoiceNumber/referenceNumber for humans."
    }}
  ]
}}

MANDATORY: Every payment in the input MUST appear in "decisions" exactly once.

CRITICAL — targetInvoiceIds FIELD:
Values MUST be the invoice's "id" field exactly as provided above, not "invoiceNumber" or
"referenceNumber". Leave targetInvoiceIds empty ([]) for fifo_apply, advance_payment,
no_match, and manual_review.
"""

        decision_schema = {
            "type": "object",
            "properties": {
                "allocationSummary": {"type": "string"},
                "decisions": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "paymentId": {"type": "string"},
                            "matchType": {
                                "type": "string",
                                "enum": [
                                    "exact_reference", "corroborated_match", "consolidated_batch",
                                    "fifo_apply", "advance_payment", "no_match", "manual_review"
                                ]
                            },
                            "targetInvoiceIds": {"type": "array", "items": {"type": "string"}},
                            "confidence": {"type": "number"},
                            "reasoning": {"type": "string"}
                        },
                        "required": ["paymentId", "matchType", "targetInvoiceIds", "confidence", "reasoning"],
                        "additionalProperties": False
                    }
                }
            },
            "required": ["allocationSummary", "decisions"],
            "additionalProperties": False
        }

        decisions = None
        thought_trace = ""

        try:
            logger.info(f"[AIMatcher] [Group: '{customer_name}'] Calling LLM generate_json (waiting for semaphore)...")
            async with sem:
                logger.info(f"[AIMatcher] [Group: '{customer_name}'] Acquired semaphore. Calling LLM generate_json...")
                response = await llm.generate_json(
                    system_prompt,
                    decision_schema,
                    purpose="ai_semantic_matching",
                    tenant_id=tenant_id
                )
                if isinstance(response, list):
                    response = response[0] if response else {}

                thought_trace = response.get("allocationSummary") or "No detailed reasoning trace provided."
                decisions = response.get("decisions", [])
                logger.info(f"[AIMatcher] [Group: '{customer_name}'] LLM response received successfully. Decisions count: {len(decisions)}")
        except asyncio.CancelledError:
            logger.warning(f"[AIMatcher] [Group: '{customer_name}'] LLM generation task was CANCELLED by parent.")
            raise
        except Exception as e:
            logger.error(f"[AIMatcher] [Group: '{customer_name}'] Exception during LLM call: {e}", exc_info=True)

        if not decisions:
            logger.warning(f"[AIMatcher] [Group: '{customer_name}'] Fallback to unreconciled proposals.")
            decisions = [
                {"paymentId": p["id"], "matchType": "no_match", "targetInvoiceIds": [], "confidence": 0.0,
                 "reasoning": "LLM failed to generate a classification or error occurred."}
                for p in payments
            ]
            thought_trace = "Error or timeout during processing."

        trace_markdown = f"### AI Decision Engine Analysis\n{thought_trace}\n"
        decisions_by_payment = {d.get("paymentId"): d for d in decisions}

        group_proposals = _allocate_fifo_deterministic(payments, invoices, decisions_by_payment, shared_balances)
        for prop in group_proposals:
            prop["ai_reasoning"] = trace_markdown

        return group_proposals

    # ── other group types (amount_proximity): unchanged legacy path ──────────
    # These groups lack a confirmed single customer identity (matched by amount
    # proximity across possibly-different customers), so the FIFO waterfall above
    # — which assumes one customer's own invoice queue — doesn't apply cleanly here.
    # The LLM still proposes amounts directly, same as before.
    system_prompt = f"""You are an expert financial reconciliation engine operating under Generally Accepted Accounting Principles (GAAP) and Indian Accounting Standards (Ind AS).
Your task is to reconcile payments against open invoices.
You are the decision engine, not the accounting engine.

Your responsibility is to select the most appropriate allocation from the available candidates, explain your decision, and classify the result.

Never invent allocations outside the supplied data.
Never violate the provided accounting constraints.
When evidence is insufficient or conflicting, return "manual_review" rather than guessing.

═══════════════════════════════════════════════════
CONTEXT
═══════════════════════════════════════════════════

CUSTOMER ACCOUNT: {customer_name}

PAYMENTS TO RECONCILE:
{json.dumps(ai_payments, indent=2)}

OPEN INVOICES (with remaining_balance reflecting prior allocations):
{json.dumps(ai_invoices, indent=2)}

═══════════════════════════════════════════════════
MATCHING HIERARCHY (apply in this exact order)
═══════════════════════════════════════════════════

For each payment, evaluate the matching hierarchy in priority order.
Higher-priority evidence outweighs lower-priority evidence, but do not ignore contradictory evidence.

If higher-priority and lower-priority evidence conflict (for example, the payment reference points to one invoice while the amount strongly indicates another), do not force a match. Return status "manual_review" and clearly explain the conflicting evidence.

PRIORITY 1 — REFERENCE / DOCUMENT NUMBER MATCH
  Match payment reference against invoice number, reference number, or PO number.
  Normalize: strip whitespace, hyphens, slashes, leading zeros. Case-insensitive.
  If reference matches exactly → confidence ≥ 0.95.

PRIORITY 2 — AMOUNT + DATE + NAME CORROBORATION
  Exact or near-exact amount match (within tolerance) AND payment date is on or
  after the invoice date AND customer/sender name matches → confidence ≥ 0.90.

PRIORITY 3 — PARTIAL PAYMENT APPLICATION
  Payment is less than invoice balance with no tax/discount explanation.
  Apply the full payment amount to the invoice.
  Status: "partial", discrepancyAmount = remaining unpaid balance (positive).
  When multiple invoices could receive the partial payment, apply FIFO (Rule C).

  WORKED EXAMPLE:
  Payment: 80,000 from "XYZ Trading Co"
  Invoice: id="INV-05CFD4914F", invoiceNumber="INV-007", amount=88,500, remaining_balance=88,500
  → This IS a valid partial payment. The customer paid 80,000 toward an 88,500 invoice.
  → invoiceId: "INV-05CFD4914F" (use the "id" field, NOT "INV-007")
  → amountApplied: 80,000.00
  → discrepancyAmount: 8,500.00 (positive = under-payment)
  → status: "partial"
  → confidence: 0.82

  DO NOT return "unreconciled" simply because the payment amount does not exactly match.
  If the payment is from a known customer and is less than an open invoice balance, it is
  a partial payment unless contradicted by stronger evidence.

PRIORITY 4 — CONSOLIDATED / BATCH PAYMENT (one payment → multiple invoices)
  A single payment amount matches the SUM of two or more invoice balances.
  Split the payment across those invoices. Each sub-allocation is a separate
  proposal entry, all sharing the same paymentId.
  The sum of all amountApplied for that paymentId must equal the payment amount.

PRIORITY 5 — INSTALLMENT RECEIPTS (multiple payments → one invoice)
  Multiple payments that collectively settle an invoice balance.
  Mark intermediate payments as "partial", the final clearing payment as "reconciled".

PRIORITY 6 — ADVANCE PAYMENT / ON-ACCOUNT RECEIPT
  Payment received with no matching invoice at all, or payment date is
  significantly before the earliest invoice date for this customer.
  Status: "unreconciled", reasoning must state "Customer advance / on-account
  receipt — no matching invoice exists", confidence 0.0.

PRIORITY 7 — NO MATCH
  No logical link found. Status: "unreconciled", invoiceId: null,
  confidence: 0.0. Do NOT guess or force-match.

═══════════════════════════════════════════════════
ACCOUNTING RULES (mandatory, never violate)
═══════════════════════════════════════════════════

RULE A — AMOUNT CEILING
  amountApplied must NEVER exceed the invoice's current remaining_balance.
  amountApplied must NEVER exceed the payment amount minus amounts already
  allocated from that payment to other invoices.

RULE B — ROLLING BALANCE TRACKING (CRITICAL)
  Process payments sequentially. After allocating Payment A to Invoice X,
  Invoice X's remaining_balance decreases by the applied amount.
  Subsequent allocations must use the UPDATED balance, not the original.
  State the before/after balance for each allocation in your thought process.

RULE C — DEFAULT ALLOCATION POLICY (FIFO)
  After applying all higher-priority matching rules, if multiple invoices remain equally valid candidates, allocate the payment to the oldest outstanding invoice by Due Date.
  If Due Date is unavailable, use Invoice Date.
  FIFO is a tie-breaking policy and must not override stronger evidence such as invoice references, remittance advice, or explicit customer payment instructions.

RULE D — MATERIALITY THRESHOLD FOR DISCREPANCIES
  Differences ≤ 2% of the invoice amount OR ≤ 500.00 (whichever is smaller)
  may be treated as immaterial (bank charges, rounding, cash discount).
  Status: "reconciled" with discrepancyAmount noted.
  Differences > this threshold: status "partial" or "discrepancy".

RULE E — DISCREPANCY SIGN CONVENTION (mandatory)
  discrepancyAmount = invoice_remaining_balance_BEFORE_allocation − amountApplied
    POSITIVE → under-payment (customer paid less, balance remains open)
    NEGATIVE → over-payment (customer paid more than owed)
    ZERO → exact match

RULE F — NO DOUBLE APPLICATION
  Each payment can only be applied once (or split, but total applied = payment amount).
  Each invoice balance can only be consumed once within this group.
  Flag any suspected duplicate payments (same amount, same date, same reference)
  with a warning in reasoning.

RULE G — CURRENCY CONSISTENCY
  Only match payments and invoices in the same currency.
  If currencies differ, status: "unreconciled" with reasoning noting currency mismatch.

═══════════════════════════════════════════════════
CONFIDENCE SCORING RUBRIC
═══════════════════════════════════════════════════

Apply these calibrated scores (do not invent your own scale):

  0.95–1.00  Exact reference match + amount matches
  0.90–0.94  Amount + date + customer name all corroborate
  0.85–0.89  Amount match with explainable tax/discount adjustment
  0.75–0.84  Strong amount proximity but weak corroboration (name fuzzy, no ref)
  0.50–0.74  Plausible but ambiguous — needs human review
  0.00       No match or forced to "unreconciled"

═══════════════════════════════════════════════════
OUTPUT FORMAT
═══════════════════════════════════════════════════

Return a JSON object with:

{{
  "allocationSummary": "Step-by-step reasoning. For EACH payment: (1) which priority rules you evaluated, (2) which matched and why, (3) the invoice balance before and after allocation. Be explicit about tax/discount calculations if applied. Reference invoices by their actual 'invoiceNumber' or 'referenceNumber' instead of the system-generated 'id'.",
  "proposals": [
    {{
      "paymentId": "payment_id",
      "invoiceId": "invoice_id_or_null",
      "status": "reconciled" | "partial" | "discrepancy" | "unreconciled",
      "confidence": 0.0_to_1.0,
      "reasoning": "Concise explanation: which priority matched, evidence used, any adjustment applied. Refer to invoices by their actual 'invoiceNumber' or 'referenceNumber' instead of the system-generated 'id'.",
      "amountApplied": 0.00,
      "discrepancyAmount": 0.00
    }}
  ]
}}

MANDATORY: Every payment in the input MUST appear in the proposals list exactly once
(unless it is split across multiple invoices via Priority 4, in which case it appears
once per invoice allocation, and all entries share the same paymentId).

CRITICAL — invoiceId FIELD:
The "invoiceId" in each proposal MUST be the value from the invoice's "id" field exactly
as provided in the OPEN INVOICES data above. Do NOT use "invoiceNumber" or "referenceNumber"
in the invoiceId field — those are for human-readable reasoning and allocationSummary only.
The system uses "id" for internal record linkage and will reject proposals with unrecognized IDs.
"""


    decision_schema = {
        "type": "object",
        "properties": {
            "allocationSummary": {"type": "string"},
            "proposals": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "paymentId": {"type": "string"},
                        "invoiceId": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                        "status": {"type": "string", "enum": ["reconciled", "partial", "discrepancy", "unreconciled", "manual_review"]},
                        "confidence": {"type": "number"},
                        "reasoning": {"type": "string"},
                        "amountApplied": {"type": "number"},
                        "discrepancyAmount": {"anyOf": [{"type": "number"}, {"type": "null"}]}
                    },
                    "required": [
                        "paymentId", "invoiceId", "status", "confidence",
                        "reasoning", "amountApplied", "discrepancyAmount"
                    ],
                    "additionalProperties": False
                }
            }
        },
        "required": ["allocationSummary", "proposals"],
        "additionalProperties": False
    }

    results = None
    thought_trace = ""

    try:
        logger.info(f"[AIMatcher] [Group: '{customer_name}'] Calling LLM generate_json (waiting for semaphore)...")
        async with sem:
            logger.info(f"[AIMatcher] [Group: '{customer_name}'] Acquired semaphore. Calling LLM generate_json...")
            response = await llm.generate_json(
                system_prompt,
                decision_schema,
                purpose="ai_semantic_matching",
                tenant_id=tenant_id
            )
            if isinstance(response, list):
                response = response[0] if response else {}

            thought_trace = response.get("allocationSummary") or "No detailed reasoning trace provided."
            results = response.get("proposals", [])
            logger.info(f"[AIMatcher] [Group: '{customer_name}'] LLM response received successfully. Proposals count: {len(results)}")
    except asyncio.CancelledError:
        logger.warning(f"[AIMatcher] [Group: '{customer_name}'] LLM generation task was CANCELLED by parent.")
        raise
    except Exception as e:
        logger.error(f"[AIMatcher] [Group: '{customer_name}'] Exception during LLM call: {e}", exc_info=True)

    if not results:
        logger.warning(f"[AIMatcher] [Group: '{customer_name}'] Fallback to unreconciled proposals.")
        results = [
            {
                "paymentId": p["id"],
                "invoiceId": None,
                "status": "unreconciled",
                "confidence": 0.0,
                "reasoning": "LLM failed to generate proposals or error occurred.",
                "amountApplied": 0.0,
                "discrepancyAmount": None
            }
            for p in payments
        ]
        thought_trace = "Error or timeout during processing."

    trace_markdown = f"### AI Decision Engine Analysis\n{thought_trace}\n"
    valid_inv_ids = {inv["id"] for inv in invoices}

    # shared_balances is a cross-group ledger passed in by node_ai_semantic_matcher.
    # All concurrent groups claim from the same dict, preventing cross-group double-spend.
    # Within a group the loop is synchronous, so no lock is needed.

    group_proposals = []
    for res in results:
        res["ai_reasoning"] = trace_markdown
        p_id = res.get("paymentId")
        pay = next((p for p in payments if p["id"] == p_id), None)
        if not pay:
            continue
        pay_amt = float(pay["amount"])
        inv_id = res.get("invoiceId")
        applied = float(res.get("amountApplied") or 0.0)

        if inv_id and inv_id not in valid_inv_ids:
            # ponytail: LLM may return referenceNumber/invoiceNumber instead of system id.
            # Resolve before blocking — one next() over a small list, only on mismatch.
            resolved = next(
                (inv["id"] for inv in invoices
                 if inv.get("referenceNumber") == inv_id or inv.get("invoiceNumber") == inv_id),
                None
            )
            if resolved:
                logger.warning(
                    f"[AIMatcher] [Group: '{customer_name}'] "
                    f"Resolved LLM reference '{inv_id}' → system ID '{resolved}' for {p_id}"
                )
                inv_id = resolved
                res["invoiceId"] = resolved
            else:
                logger.error(f"[AIMatcher] [Group: '{customer_name}'] BLOCKED out-of-scope invoice: {p_id} → {inv_id}")
                res["invoiceId"] = None
                res["status"] = "unreconciled"
                res["confidence"] = 0.0
                res["reasoning"] = f"BLOCKED: Invoice {inv_id} not in candidate set."
                res["amountApplied"] = 0.0
                res["discrepancyAmount"] = None
        else:
            if inv_id:
                current_bal = shared_balances.get(inv_id, 0.0)
                res["balanceBeforeAllocation"] = current_bal
                if applied > current_bal + 0.01:
                    logger.warning(
                        f"[AIMatcher] [Group: '{customer_name}'] "
                        f"Clamped over-applied {applied:.2f} → {current_bal:.2f} for invoice {inv_id} "
                        f"(prior allocations exhausted the balance)"
                    )
                    applied = max(0.0, current_bal)
                    res["amountApplied"] = applied
                # Decrement shared ledger so subsequent proposals (this group or others) see the live balance
                shared_balances[inv_id] = max(0.0, current_bal - applied)

        group_proposals.append(res)

    # Fix 4 (Rule F, No Double Application): an LLM must not be allowed to split
    # the same payment across two separate proposal rows into the SAME invoice —
    # that posts two journal lines for what is one allocation. Merge them.
    seen: dict[tuple, dict] = {}
    deduped: list[dict] = []
    for prop in group_proposals:
        inv_id = prop.get("invoiceId")
        if inv_id is None:
            deduped.append(prop)
            continue
        key = (prop.get("paymentId"), inv_id)
        if key in seen:
            existing = seen[key]
            existing["amountApplied"] = round(
                float(existing.get("amountApplied") or 0.0) + float(prop.get("amountApplied") or 0.0), 2
            )
            existing["confidence"] = max(float(existing.get("confidence") or 0.0), float(prop.get("confidence") or 0.0))
            existing["reasoning"] = (existing.get("reasoning") or "") + " [Merged duplicate allocation to same invoice — Rule F]"
        else:
            seen[key] = prop
            deduped.append(prop)

    return deduped



async def node_ai_semantic_matcher(state: ReconciliationState) -> ReconciliationState:
    """
    Stage 3 — AI Semantic Matching (Batch Group-Based Decision Engine).
    Groups remaining unmatched payments by customer, gathers candidate invoices,
    and runs a concurrent decision pass across all customer groups.
    """
    matched_pids = {p["paymentId"] for p in state.get("current_match_proposals", []) if p.get("status") not in ("unreconciled", "pending_review")}
    remaining_payments = [p for p in state.get("unreconciled_payments", []) if p["id"] not in matched_pids]

    if not remaining_payments:
        return state

    cfg = state.get("config", {})
    fuzzy_threshold = _get_config_param(cfg, "fuzzy_name_threshold", 0.65)

    # A payment we cannot attribute to a legal entity has no lawful candidate pool.
    # Route it to a human with that stated as the reason, rather than letting it fall
    # through the tiers and surface as an ordinary "no matching invoice found" — the
    # fix for the two cases is completely different.
    unattributed = [p for p in remaining_payments if not has_entity(p)]
    remaining_payments = [p for p in remaining_payments if has_entity(p)]

    proposals = list(state.get("current_match_proposals", []))
    for payment in unattributed:
        logger.warning(
            f"[AIMatcher] Payment {payment['id']} has no legal entity — routed to review, not matched."
        )
        proposals.append({
            "paymentId": payment["id"],
            "invoiceId": None,
            "status": "pending_review",
            "confidence": 0.0,
            "reasoning": (
                "No legal entity could be resolved for this payment. Entity context is "
                "mandatory before an obligation can be settled, so no invoice was "
                "considered. Attribute the payment to a legal entity and re-run."
            ),
            "amountApplied": 0.0,
            "discrepancyAmount": None,
            "match_type": "entity_unresolved",
            "reviewReason": ENTITY_UNRESOLVED,
        })

    if not remaining_payments:
        state["current_match_proposals"] = proposals
        return state

    groups = _group_payments_by_customer(remaining_payments, state["open_invoices"], fuzzy_threshold=fuzzy_threshold)

    tenant_id = state.get("tenant_id")
    llm = get_llm_provider(tenant_id)

    # Build ONE shared balance ledger before launching any groups.
    # All concurrent groups claim from this same dict — cross-group double-spend is structurally impossible.
    # asyncio.gather runs on a single event-loop thread; the balance-claiming code after each
    # LLM await is synchronous, so concurrent groups serialise naturally with no lock needed.
    shared_balances: dict[str, float] = {
        inv_id: float(inv.get("remaining_balance", 0))
        for inv_id, inv in state["open_invoices"].items()
        if inv.get("source_system") == "invoice"
    }

    sem = asyncio.Semaphore(AI_MATCHER_CONCURRENCY_LIMIT)
    tasks = [
        _reconcile_single_group(group.get("customer_name") or group_key, group, llm, tenant_id, shared_balances, sem)
        for group_key, group in groups.items()
    ]

    if tasks:
        logger.info(f"[AIMatcher] Initiating concurrent processing for {len(tasks)} customer groups.")
        results_list = await asyncio.gather(*tasks)
        for group_proposals in results_list:
            proposals.extend(group_proposals)
        logger.info(f"[AIMatcher] Completed concurrent processing for all groups. Total proposals: {len(proposals)}")

    # Sync shared_balances back to state so the verification node sees live remaining balances.
    for inv_id, bal in shared_balances.items():
        if inv_id in state["open_invoices"]:
            state["open_invoices"][inv_id]["remaining_balance"] = bal

    # Update in_run_allocated from committed proposals.
    in_run_allocated: dict[str, float] = state.get("in_run_allocated") or {}
    for prop in proposals:
        inv_id = prop.get("invoiceId")
        applied = float(prop.get("amountApplied") or 0.0)
        if inv_id and applied > 0:
            in_run_allocated[inv_id] = in_run_allocated.get(inv_id, 0.0) + applied
    state["in_run_allocated"] = in_run_allocated

    state["current_match_proposals"] = proposals
    return state


async def node_verification_and_hitl(state: ReconciliationState) -> ReconciliationState:
    """
    Stage 4 — Verification & HITL Routing (Batch Mode).
    Validates every proposal in current_match_proposals.
    Auto-approves matches with confidence >= auto_approve_confidence.
    Otherwise, marks them as pending_review.
    """
    proposals = state.get("current_match_proposals", [])
    if not proposals:
        return state

    cfg = state.get("config", {})
    auto_approve_confidence = _get_config_param(cfg, "auto_approve_confidence", 0.90)

    # Map of payments by ID for amount checking
    payments_map = {p["id"]: p for p in state["unreconciled_payments"]}

    # Group proposals by payment ID to perform cross-customer and amount allocation checks per payment
    props_by_payment = {}
    for prop in proposals:
        pid = prop["paymentId"]
        if pid not in props_by_payment:
            props_by_payment[pid] = []
        props_by_payment[pid].append(prop)

    # Initialize results lists
    state["reconciliation_results"] = []
    state["pending_reviews"] = []

    for pid, payment_props in props_by_payment.items():
        payment = payments_map.get(pid)
        if not payment:
            logger.warning(f"[Verify] Payment {pid} not found in state.")
            continue
        pay_amount = float(payment["amount"])

        # 1. Cross-Customer Guard for this payment's proposals
        proposed_customers = set()
        for prop in payment_props:
            inv_id = prop.get("invoiceId")
            if inv_id and inv_id in state["open_invoices"]:
                c = state["open_invoices"][inv_id].get("customerName", "").strip().lower()
                if c:
                    proposed_customers.add(c)

        if len(proposed_customers) > 1:
            logger.error(f"[Verify] CROSS-CUSTOMER BLOCK: payment={pid} → customers={proposed_customers}")
            for prop in payment_props:
                prop["status"] = "pending_review"
                prop["confidence"] = 0.0
                prop["reasoning"] = (
                    f"BLOCKED: Proposals span multiple customers {proposed_customers}. "
                    "A single payment cannot be applied across different customers."
                )
                state["pending_reviews"].append(prop)
            continue

        # 2. Verify each proposal for this payment
        total_applied = 0.0
        all_approved = True
        processed_props = []

        for prop in payment_props:
            inv_id = prop.get("invoiceId")
            applied = float(prop.get("amountApplied") or 0.0)
            confidence = float(prop.get("confidence") or 0.0)
            total_applied += applied

            if inv_id is None:
                prop["status"] = "unreconciled"
                all_approved = False
            elif inv_id not in state["open_invoices"]:
                prop["status"] = "unreconciled"
                prop["invoiceId"] = None
                prop["confidence"] = 0.0
                prop["reasoning"] = (prop.get("reasoning") or "") + f" [FLAG: Invoice {inv_id} not found in open invoices]"
                prop["reviewReason"] = "invoice_not_found"
                all_approved = False
            else:
                orig_bal = state.get("original_balances", {}).get(inv_id)
                if orig_bal is None:
                    orig_bal = float(state["open_invoices"][inv_id].get("remaining_balance", 0))

                if confidence < 0.50:
                    # Keep an audit trail of what was computed and discarded — a payment
                    # this system actually allocated (real amountApplied, real invoice)
                    # must never vanish into "unreconciled" with no explanation of why.
                    prop["reasoning"] = (prop.get("reasoning") or "") + (
                        f" [FLAG: Confidence {confidence:.0%} below minimum match threshold "
                        f"(50%) — proposed allocation of ₹{applied:,.2f} to invoice {inv_id} discarded]"
                    )
                    prop["status"] = "unreconciled"
                    prop["invoiceId"] = None
                    prop["confidence"] = 0.0
                    prop["reviewReason"] = "below_minimum_confidence"
                    all_approved = False
                elif confidence < auto_approve_confidence:
                    prop["status"] = "pending_review"
                    prop["reasoning"] = (prop.get("reasoning") or "") + (
                        f" [FLAG: Confidence {confidence:.0%} below "
                        f"auto-approve threshold ({auto_approve_confidence:.0%})]"
                    )
                    prop["reviewReason"] = "low_confidence"
                    prop["autoApproveThreshold"] = auto_approve_confidence
                    all_approved = False
                elif applied > orig_bal + 0.01:
                    # Two-tier check:
                    # Tier 1 — check live balance (post all prior node deductions).
                    #   If applied <= live balance, the FLAG was from a stale orig_bal snapshot.
                    #   Strip it and allow auto-approval.
                    # Tier 2 — if still over-applying the live balance, it's a genuine issue → HITL.
                    live_bal = float(state["open_invoices"][inv_id].get("remaining_balance", 0))
                    if applied <= live_bal + 0.01:
                        # Stale snapshot artifact — proposal is actually valid against live balance
                        logger.info(
                            f"[Verify] Stale orig_bal FLAG cleared for {inv_id}: "
                            f"applied={applied:.2f} orig_bal={orig_bal:.2f} live_bal={live_bal:.2f} — auto-approving"
                        )
                        # No change to all_approved — let it proceed
                    else:
                        prop["status"] = "pending_review"
                        prop["reasoning"] = (prop.get("reasoning") or "") + (
                            f" [FLAG: Applied {applied:.2f} exceeds live balance {live_bal:.2f} "
                            f"(original: {orig_bal:.2f})]"
                        )
                        prop["reviewReason"] = "over_allocation"
                        all_approved = False

                # 3-Way PO validation: Invoice vs PO Outstanding Balance check
                parent_id = state["open_invoices"][inv_id].get("parent_id")
                if parent_id and parent_id in state["open_invoices"]:
                    parent_po = state["open_invoices"][parent_id]
                    po_outstanding = float(parent_po.get("remaining_balance", 0.0))
                    inv_total = float(state["open_invoices"][inv_id].get("amount", 0.0))
                    if inv_total > po_outstanding + 0.01:
                        prop["reasoning"] = (prop.get("reasoning") or "") + (
                            f" [FLAG: Invoice amount {inv_total:.2f} exceeds "
                            f"parent PO {parent_id} outstanding balance {po_outstanding:.2f}]"
                        )
                        prop["status"] = "pending_review"
                        prop["reviewReason"] = "po_balance_exceeded"
                        all_approved = False

            processed_props.append(prop)

        # 3. Total amount guard
        if total_applied > pay_amount + 0.01:
            all_approved = False
            logger.warning(f"[Verify] Amount guard: total_applied={total_applied:.2f} > pay_amount={pay_amount:.2f}")
            for prop in processed_props:
                prop["status"] = "pending_review"
                prop["reasoning"] = (prop.get("reasoning") or "") + f" [FLAG: Total allocated ({total_applied:.2f}) exceeds payment ({pay_amount:.2f})]"
                prop["reviewReason"] = "payment_over_allocated"

        # 4. Commit results
        # Stamp accounting_treatment deterministically on every proposal before commit.
        # This runs after all guards — status is final at this point.
        #
        # invoice_amount for the shortfall math MUST be the balance actually owed
        # immediately before THIS proposal's allocation, not the invoice's face value.
        # When one invoice receives multiple proposals in a run (FIFO split across
        # payments, installments, etc.), face value only reflects the truth for the
        # FIRST one — every proposal after that would have its shortfall computed
        # against an amount that was already partly paid down by an earlier proposal
        # in this same run, misclassifying a closing payment as a big shortfall.
        # Each matcher (rule/memory/AI FIFO allocator) already knows its own correct
        # "balance before" and stamps it as balanceBeforeAllocation; original_balances
        # (the run-start snapshot) is the correct fallback for anything that didn't.
        short_pay_tol = _get_config_param(cfg, "short_payment_tolerance", 500.0)
        short_pay_pct = _get_config_param(cfg, "short_payment_pct_cap", 2.0)
        run_start_balances = state.get("original_balances", {})
        for prop in processed_props:
            inv_id_for_treat = prop.get("invoiceId")
            balance_owed = prop.get("balanceBeforeAllocation")
            if balance_owed is None:
                balance_owed = run_start_balances.get(inv_id_for_treat)
            if balance_owed is None:
                balance_owed = state["open_invoices"].get(inv_id_for_treat, {}).get("amount", 0) if inv_id_for_treat else 0
            balance_owed = float(balance_owed)
            prop["accounting_treatment"] = _compute_accounting_treatment(
                status=prop.get("status", "unreconciled"),
                amount_applied=float(prop.get("amountApplied") or 0.0),
                invoice_amount=balance_owed,
                payment_amount=pay_amount,
                short_payment_tolerance=short_pay_tol,
                short_payment_pct_cap=short_pay_pct,
            )
            prop["_balanceOwedAtAllocation"] = balance_owed

        if all_approved:
            for prop in processed_props:
                # Add audit trail for auto-approved matches too
                inv_id_for_trail = prop.get("invoiceId")
                if inv_id_for_trail:
                    inv_data = state["open_invoices"].get(inv_id_for_trail, {})
                    inv_ref_trail = inv_data.get("referenceNumber", inv_id_for_trail)
                    inv_face_amount = float(inv_data.get("amount", 0))
                    balance_owed = prop.pop("_balanceOwedAtAllocation", inv_face_amount)
                    applied_trail = float(prop.get("amountApplied") or 0.0)
                    prop["reasoning"] = (prop.get("reasoning") or "") + build_audit_trail(
                        payment_amount=pay_amount,
                        invoice_ref=inv_ref_trail,
                        invoice_face_amount=inv_face_amount,
                        balance_owed=balance_owed,
                        amount_applied=applied_trail,
                        treatment=prop.get("accounting_treatment"),
                    )
                state["reconciliation_results"].append(prop)
                logger.info(
                    f"[Verify] ✅ AUTO-APPROVED: {pid} → {prop.get('invoiceId')} "
                    f"amount={prop.get('amountApplied'):.2f} conf={prop.get('confidence'):.2f} "
                    f"treatment={prop.get('accounting_treatment')}"
                )
        else:
            for prop in processed_props:
                # If there's no invoice ID, the payment is completely unreconciled rather than pending review.
                if prop.get("invoiceId") is None:
                    prop["status"] = "unreconciled"
                    prop.pop("_balanceOwedAtAllocation", None)
                    state["reconciliation_results"].append(prop)
                    logger.info(
                        f"[Verify] ❌ UNRECONCILED: {pid} (No invoice candidate)"
                    )
                else:
                    prop["status"] = "pending_review"
                    # Finding D1: compute REAL treatment (partial_receipt etc.),
                    # don't overwrite with 'pending_review'. Workflow state is
                    # already in prop["status"]; treatment reflects accounting nature.
                    # accounting_treatment was already stamped above using the correct
                    # balance_owed (not face value) — just reuse it for the audit text.
                    inv_id_for_treat = prop.get("invoiceId")
                    inv_data = state["open_invoices"].get(inv_id_for_treat, {})
                    inv_face_amount = float(inv_data.get("amount", 0))
                    balance_owed = prop.pop("_balanceOwedAtAllocation", inv_face_amount)
                    inv_ref = inv_data.get("referenceNumber", inv_id_for_treat)
                    # Enhance reasoning with auditable allocation details
                    applied = float(prop.get('amountApplied') or 0.0)
                    prop["reasoning"] = (prop.get("reasoning") or "") + build_audit_trail(
                        payment_amount=pay_amount,
                        invoice_ref=inv_ref,
                        invoice_face_amount=inv_face_amount,
                        balance_owed=balance_owed,
                        amount_applied=applied,
                        treatment=prop["accounting_treatment"],
                    )
                    state["pending_reviews"].append(prop)
                    logger.info(
                        f"[Verify] ⏳ PENDING REVIEW: {pid} → {inv_id_for_treat} "
                        f"conf={prop.get('confidence'):.2f} treatment={prop.get('accounting_treatment')}"
                    )

    # ── Layer 2: Aggregate Invoice Balance Guard (GAAP Rule A) ───────────────
    # Defense-in-depth: even if Layer 1 (shared_balances) stops cross-group
    # double-spend at source, this guard catches any future allocation path
    # that bypasses the shared ledger. Clamps the weakest (lowest-confidence)
    # proposal for an over-allocated invoice, preserving the highest-confidence
    # matches (strict > memory > AI).
    original_balances = state.get("original_balances", {})
    if original_balances:
        inv_applied_total: dict[str, float] = {}
        inv_weakest: dict[str, dict] = {}  # lowest-confidence proposal per invoice
        for prop in state["reconciliation_results"] + state["pending_reviews"]:
            inv_id = prop.get("invoiceId")
            if not inv_id:
                continue
            applied = float(prop.get("amountApplied") or 0.0)
            inv_applied_total[inv_id] = inv_applied_total.get(inv_id, 0.0) + applied
            conf = float(prop.get("confidence") or 0.0)
            if inv_id not in inv_weakest or conf < float(inv_weakest[inv_id].get("confidence") or 0.0):
                inv_weakest[inv_id] = prop

        for inv_id, total_applied in inv_applied_total.items():
            ceiling = original_balances.get(inv_id)
            if ceiling is None or total_applied <= ceiling + 0.01:
                continue
            excess = round(total_applied - ceiling, 2)
            target = inv_weakest[inv_id]
            target_applied = float(target.get("amountApplied") or 0.0)
            clamped = round(max(0.0, target_applied - excess), 2)
            logger.warning(
                f"[Verify] INVOICE OVER-ALLOCATION (Rule A): {inv_id} "
                f"total={total_applied:.2f} ceiling={ceiling:.2f} excess={excess:.2f}. "
                f"Clamping weakest proposal (payment={target.get('paymentId')}, "
                f"conf={target.get('confidence'):.2f}) from {target_applied:.2f} → {clamped:.2f}"
            )
            target["amountApplied"] = clamped
            target["reasoning"] = (target.get("reasoning") or "") + (
                f" [FLAG: Invoice over-allocated by {excess:.2f}; clamped to {clamped:.2f} (Rule A)]"
            )
            if clamped <= 0.01:
                target["status"] = "unreconciled"
                target["invoiceId"] = None
                target["confidence"] = 0.0
            elif target.get("status") not in ("pending_review",):
                target["status"] = "pending_review"
            # Move from reconciliation_results → pending_reviews if status changed
            if target in state["reconciliation_results"] and target["status"] in ("pending_review", "unreconciled"):
                state["reconciliation_results"].remove(target)
                if target["status"] == "pending_review":
                    state["pending_reviews"].append(target)
                else:
                    state["reconciliation_results"].append(target)

    return state


async def node_overpayment_classifier(state: ReconciliationState) -> ReconciliationState:
    """
    Stage 3.5 — LLM Overpayment & Remainder Classifier Agent.
    For every proposal with a remainder (discrepancyAmount > 0.01), calls LLM
    to classify the remainder as: advance_payment, installment_candidate,
    duplicate_payment, or unidentified_receipt.
    """
    proposals = state.get("current_match_proposals", []) or []
    if not proposals:
        return state

    tenant_id = state.get("tenant_id")
    llm = get_llm_provider(tenant_id)

    # Map of payments by ID
    payments_map = {p["id"]: p for p in state.get("unreconciled_payments", [])}

    # Group proposals by payment ID to calculate total applied
    applied_by_payment = {}
    for prop in proposals:
        pid = prop["paymentId"]
        applied_by_payment[pid] = applied_by_payment.get(pid, 0.0) + float(prop.get("amountApplied") or 0.0)

    # Let's check which payments have remainders
    for prop in proposals:
        pid = prop["paymentId"]
        payment = payments_map.get(pid)
        if not payment:
            continue

        pay_amount = float(payment["amount"])
        total_applied = applied_by_payment.get(pid, 0.0)
        remainder = round(pay_amount - total_applied, 2)

        # If there is a meaningful remainder and it hasn't been classified yet
        if remainder > 0.01 and not prop.get("remainder_classification"):
            sender = payment.get("senderName") or "Unknown"
            ref = payment.get("reference") or ""
            desc = payment.get("description") or ""

            # Find candidate open invoices for this customer
            cust_invoices = []
            if sender and sender != "Unknown":
                # Entity-scoped: never offer another entity's invoice as somewhere to
                # put this payment's remainder.
                in_entity = {
                    inv_id: inv for inv_id, inv in state.get("open_invoices", {}).items()
                    if same_entity(payment, inv)
                }
                cust_invoices = _get_customer_invoices(sender, in_entity)

            cust_invoices_serialized = [
                {
                    "id": inv["id"],
                    "amount": float(inv["amount"]),
                    "remaining_balance": float(inv["remaining_balance"]),
                    "dueDate": inv.get("dueDate"),
                    "invoiceNumber": inv.get("invoiceNumber"),
                }
                for inv in cust_invoices
            ]

            system_prompt = f"""You are a senior accounts receivable specialist reviewing an overpayment remainder.
Your job is to classify the unapplied remainder of a payment after allocating the matched portion.

PAYMENT DETAILS:
- Payment ID: {pid}
- Total Received: {pay_amount:.2f}
- Sender: {sender}
- Reference: {ref}
- Narration/Description: {desc}

ALLOCATION SUMMARY:
- Matched Invoice ID: {prop.get("invoiceId")}
- Amount Applied to Invoice: {prop.get("amountApplied")}
- Unapplied Remainder: {remainder:.2f}

OTHER OPEN INVOICES FOR THIS CUSTOMER:
{json.dumps(cust_invoices_serialized, indent=2)}

CLASSIFICATION RULES:
- advance_payment: Customer intentionally paid extra (e.g. rounded up, or explicitly stated in narration) to prepay future invoices.
- installment_candidate: The remainder fits or is intended as a partial payment toward another open invoice (e.g. matching an upcoming invoice balance).
- duplicate_payment: The sender has paid the same amount/invoice previously, indicating a double payment error.
- unidentified_receipt: No clear link or customer match, or the source/intent is totally ambiguous.

JSON RESPONSE FORMAT:
Provide a JSON object containing your thought and classification details:
{{
  "thought": "Your step-by-step audit analysis of the remainder.",
  "classification": "advance_payment" | "installment_candidate" | "duplicate_payment" | "unidentified_receipt",
  "confidence": 0.0 to 1.0,
  "reasoning": "A concise explanation of why you chose this classification for the audit notes."
}}
"""
            decision_schema = {
                "type": "object",
                "properties": {
                    "thought": {"type": "string"},
                    "classification": {"type": "string", "enum": ["advance_payment", "installment_candidate", "duplicate_payment", "unidentified_receipt"]},
                    "confidence": {"type": "number"},
                    "reasoning": {"type": "string"}
                },
                "required": ["thought", "classification", "confidence", "reasoning"],
                "additionalProperties": False
            }

            try:
                logger.info(f"[OverpaymentClassifier] Classifying remainder of {remainder:.2f} for payment {pid}...")
                response = await llm.generate_json(
                    system_prompt,
                    decision_schema,
                    purpose="overpayment_classification",
                    tenant_id=tenant_id
                )
                if isinstance(response, list):
                    response = response[0] if response else {}

                classification = response.get("classification", "unidentified_receipt")
                reasoning = response.get("reasoning", "Classified by AI.")

                # Update the proposal with classification metadata
                prop["remainder_classification"] = classification
                prop["discrepancyAmount"] = remainder
                prop["reasoning"] = (prop.get("reasoning") or "") + f" [Overpayment Remainder: {remainder:.2f} classified as '{classification}' - {reasoning}]"
                logger.info(f"[OverpaymentClassifier] Classified {pid} remainder as {classification}")
            except Exception as e:
                logger.error(f"[OverpaymentClassifier] Error during LLM call: {e}")
                prop["remainder_classification"] = "unidentified_receipt"
                prop["discrepancyAmount"] = remainder

    return state
