from __future__ import annotations
"""
Standalone AR cash-application pipeline.

Flow:
  CSV ingest → StrictMatcher (rule engine)
    ├─ matched → verify + audit
    └─ leftovers → optional CrewAI AMP enrichment (flag + env gated)
                 → else optional LangGraph --ai
                 → else plain unmatched → HITL ReviewQueue
"""

import asyncio
import logging
from copy import deepcopy
from pathlib import Path
from typing import Any

from src.audit.decision_log import stamp_proposal, stamp_strict_match
from src.ingestion.csv_loader import parse_invoice_csv, parse_payment_csv
from src.rule_engine.strict_matcher import StrictMatcher
from src.verification.review_gate import ReviewQueue

logger = logging.getLogger(__name__)

DEFAULT_CONFIG: dict[str, Any] = {
    "fuzzy_name_threshold": 0.85,
    "auto_approve_threshold": 0.93,
    "amount_tolerance_pct": 0.05,
    "auto_approve_confidence": 0.90,
    "no_sender_confidence_cap": 0.75,
    "short_payment_tolerance": 500.0,
    "short_payment_pct_cap": 2.0,
}


class PipelineResult:
    def __init__(
        self,
        decisions: list[dict[str, Any]],
        pending_reviews: list[dict[str, Any]],
        leftover_payments: list[dict[str, Any]],
        review_queue: ReviewQueue,
        invoices: dict[str, dict],
    ):
        self.decisions = decisions
        self.pending_reviews = pending_reviews
        self.leftover_payments = leftover_payments
        self.review_queue = review_queue
        self.invoices = invoices

    def summary(self) -> dict[str, int]:
        auto = sum(1 for d in self.decisions if d.get("status") == "reconciled")
        return {
            "auto_reconciled": auto,
            "pending_review": len(self.pending_reviews),
            "leftover": len(self.leftover_payments),
            "total_decisions": len(self.decisions),
        }


def run_pipeline(
    invoice_csv: str | Path,
    payment_csv: str | Path | None = None,
    *,
    payment_pdf: str | Path | None = None,
    config: dict[str, Any] | None = None,
    memory_rules: list[dict] | None = None,
    use_ai_graph: bool = False,
    use_crewai: bool = False,
    crewai_bank_csv: str | Path | None = None,
    crewai_processor_csv: str | Path | None = None,
    crewai_ground_truth_csv: str | Path | None = None,
    crewai_max_wait_sec: float | None = None,
) -> PipelineResult:
    """
    Run the core reconciliation engine.

    Payment source: classic CSV (`payment_csv`) OR text-layer bank PDF (`payment_pdf`).
    CSV parsing is unchanged; PDF is an additional ingestion path into the same matcher.
    """
    cfg = {**DEFAULT_CONFIG, **(config or {})}
    invoices_list = parse_invoice_csv(invoice_csv)
    if payment_pdf and payment_csv:
        raise ValueError("Pass either payment_csv or payment_pdf, not both")
    if payment_pdf:
        from src.ingestion.pdf_loader import parse_payment_pdf

        payments = parse_payment_pdf(payment_pdf)
        payment_source = str(payment_pdf)
    elif payment_csv:
        payments = parse_payment_csv(payment_csv)
        payment_source = str(payment_csv)
    else:
        raise ValueError("Provide payment_csv or payment_pdf")
    invoices = {inv["id"]: deepcopy(inv) for inv in invoices_list}

    matcher = StrictMatcher(cfg, memory_rules or [])
    strict_matches, leftover = matcher.run(payments, invoices)

    decisions: list[dict[str, Any]] = [stamp_strict_match(m) for m in strict_matches]
    pending: list[dict[str, Any]] = [d for d in decisions if d.get("status") == "pending_review"]
    auto = [d for d in decisions if d.get("status") == "reconciled"]

    crewai_handled_ids: set[str] = set()
    if use_crewai and leftover:
        from src.agents.crewai_client import crewai_configured, enrich_leftovers_with_crewai

        if not crewai_configured():
            logger.info(
                "use_crewai requested but CREWAI_API_URL/CREWAI_BEARER_TOKEN unset — "
                "skipping enrichment (fail-open)"
            )
        else:
            from src.agents.crewai_client import DEFAULT_MAX_WAIT_SEC

            wait_sec = (
                float(crewai_max_wait_sec)
                if crewai_max_wait_sec is not None
                else DEFAULT_MAX_WAIT_SEC
            )
            suggestions = enrich_leftovers_with_crewai(
                leftover,
                bank_csv_path=str(crewai_bank_csv or payment_source),
                payment_processor_csv_path=str(crewai_processor_csv or ""),
                ground_truth_csv_path=str(crewai_ground_truth_csv or ""),
                max_wait_sec=wait_sec,
            )
            for sug in suggestions:
                stamped = stamp_proposal(sug, invoices) if sug.get("invoiceId") else sug
                pending.append(stamped)
                decisions.append(stamped)
                if stamped.get("paymentId"):
                    crewai_handled_ids.add(str(stamped["paymentId"]))
            leftover = [p for p in leftover if p["id"] not in crewai_handled_ids]

    if use_ai_graph and leftover:
        graph_out = asyncio.run(_run_graph(leftover, invoices, cfg, memory_rules or []))
        for prop in graph_out.get("reconciliation_results", []):
            stamped = stamp_proposal(prop, invoices)
            auto.append(stamped)
            decisions.append(stamped)
        for prop in graph_out.get("pending_reviews", []):
            stamped = stamp_proposal(prop, invoices)
            pending.append(stamped)
            decisions.append(stamped)
        matched_ids = {d.get("paymentId") for d in decisions}
        leftover = [p for p in leftover if p["id"] not in matched_ids]
    elif leftover:
        for p in leftover:
            unmatched = {
                "paymentId": p["id"],
                "invoiceId": None,
                "status": "pending_review",
                "confidence": 0.0,
                "reasoning": "No deterministic match; AI graph disabled for this run.",
                "amountApplied": 0.0,
                "reviewReason": "unmatched",
                "source": "pipeline",
            }
            pending.append(unmatched)
            decisions.append(unmatched)

    queue = ReviewQueue()
    for item in pending:
        queue.enqueue_from_match(
            payment_id=str(item.get("paymentId")),
            invoice_id=item.get("invoiceId"),
            confidence=float(item.get("confidence") or 0),
            reason=str(item.get("reviewReason") or item.get("status") or "pending_review"),
            reasoning=str(item.get("reasoning") or ""),
            amount_applied=float(item.get("amountApplied") or 0),
        )

    return PipelineResult(
        decisions=decisions,
        pending_reviews=pending,
        leftover_payments=leftover if use_ai_graph else [],
        review_queue=queue,
        invoices=invoices,
    )


async def _run_graph(
    payments: list[dict],
    invoices: dict[str, dict],
    config: dict,
    memory_rules: list[dict],
) -> dict:
    from src.ai_matcher.graph import build_graph

    graph = build_graph()
    state = {
        "config": config,
        "open_invoices": invoices,
        "unreconciled_payments": payments,
        "current_payment_index": 0,
        "current_match_proposals": [],
        "reconciliation_results": [],
        "pending_reviews": [],
        "match_memory_rules": memory_rules,
        "purchase_orders_context": [],
        "tenant_id": None,
        "original_balances": {},
        "in_run_allocated": {},
    }
    return await graph.ainvoke(state)
