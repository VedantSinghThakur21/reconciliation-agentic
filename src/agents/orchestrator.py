from __future__ import annotations

"""Agentic orchestration for the AI Finance Controller prototype."""

import asyncio
import logging
import time
from copy import deepcopy
from typing import Any, Callable

from src.agents.ingestion import validate_invoices, validate_payments
from src.agents.investigation import investigate_pending
from src.agents.normalization import normalize_batch
from src.audit.decision_log import stamp_proposal, stamp_strict_match
from src.connectors.demo_sources import DEMO_DIR, load_all_demo_sources
from src.db.store import Database
from src.eval.metrics import evaluate_against_ground_truth, load_ground_truth
from src.pipeline import DEFAULT_CONFIG, _run_graph
from src.rule_engine.strict_matcher import StrictMatcher
from src.verification.review_gate import ReviewQueue

logger = logging.getLogger(__name__)

StageCallback = Callable[[str, str, dict[str, Any] | None], None]


def _cash_position(
    invoices: dict[str, dict],
    payments: list[dict[str, Any]],
    decisions: list[dict[str, Any]],
) -> dict[str, Any]:
    reconciled = [d for d in decisions if d.get("status") == "reconciled"]
    cash_in = sum(float(d.get("amountApplied") or 0) for d in reconciled)
    open_ar = sum(float(inv.get("remaining_balance") or 0) for inv in invoices.values())
    pending_cash = sum(
        float(p.get("amount") or 0)
        for p in payments
        if p["id"] not in {d.get("paymentId") for d in reconciled}
    )
    return {
        "cash_applied": round(cash_in, 2),
        "open_ar": round(open_ar, 2),
        "unapplied_cash": round(pending_cash, 2),
        "net_exposure": round(open_ar - pending_cash, 2),
        "invoice_count": len(invoices),
        "payment_count": len(payments),
    }


class FinanceControllerOrchestrator:
    """
    Agentic finance-ops loop:

      Ingest → Validate → Normalize → Strict reconcile → AI leftovers →
      Investigate → Evaluate → Persist
    """

    def __init__(self, db: Database | None = None) -> None:
        self.db = db or Database()

    def run(
        self,
        *,
        mode: str = "demo",
        use_ai: bool = True,
        config: dict[str, Any] | None = None,
        on_stage: StageCallback | None = None,
    ) -> dict[str, Any]:
        cfg = {**DEFAULT_CONFIG, **(config or {})}
        run_id = self.db.create_run(mode=mode, use_ai=use_ai)
        stages: list[dict[str, Any]] = []
        t0 = time.perf_counter()

        def stage(name: str, status: str, detail: dict[str, Any] | None = None) -> None:
            entry = {"name": name, "status": status, "at": time.time(), "detail": detail or {}}
            for s in stages:
                if s["name"] == name:
                    s.update(entry)
                    break
            else:
                stages.append(entry)
            self.db.update_stages(run_id, stages)
            self.db.log_audit(
                run_id,
                f"stage_{status}",
                entity_type="stage",
                entity_id=name,
                message=f"Stage {name}: {status}",
                payload=detail,
            )
            if on_stage:
                on_stage(name, status, detail)

        try:
            stage("loading", "running")
            bundle = load_all_demo_sources()
            invoices_raw = bundle["invoices"]
            payments_raw = bundle["payments"]
            stage("loading", "completed", {
                "invoices": len(invoices_raw),
                "payments": len(payments_raw),
                "sources": bundle["sources"],
            })

            tx_rows: list[dict[str, Any]] = []
            for inv in invoices_raw:
                tx_rows.append({
                    "id": f"{run_id}-INV-{inv['id']}",
                    "source": inv.get("source") or "quickbooks",
                    "kind": "invoice",
                    "external_id": inv["id"],
                    "party_name": inv.get("customerName"),
                    "amount": inv["amount"],
                    "currency": inv.get("currency") or "INR",
                    "txn_date": inv.get("invoiceDate"),
                    "reference": inv.get("referenceNumber"),
                    "raw": inv,
                })
            for pay in payments_raw:
                tx_rows.append({
                    "id": f"{run_id}-PAY-{pay['id']}",
                    "source": pay.get("source") or "bank",
                    "kind": "payment",
                    "external_id": pay["id"],
                    "party_name": pay.get("senderName"),
                    "amount": pay["amount"],
                    "currency": pay.get("currency") or "INR",
                    "txn_date": pay.get("date"),
                    "reference": pay.get("reference"),
                    "raw": pay,
                })
            self.db.save_transactions(run_id, tx_rows)

            stage("validating", "running")
            invoices_ok, inv_issues = validate_invoices(invoices_raw)
            payments_ok, pay_issues = validate_payments(payments_raw)
            stage("validating", "completed", {
                "invoice_issues": inv_issues,
                "payment_issues": pay_issues,
                "invoices": len(invoices_ok),
                "payments": len(payments_ok),
            })

            stage("normalizing", "running")
            invoices_norm, payments_norm = normalize_batch(invoices_ok, payments_ok)
            invoices = {inv["id"]: deepcopy(inv) for inv in invoices_norm}
            stage("normalizing", "completed", {
                "normalized_invoices": len(invoices_norm),
                "normalized_payments": len(payments_norm),
            })

            stage("reconciling", "running")
            matcher = StrictMatcher(cfg, [])
            strict_matches, leftover = matcher.run(payments_norm, invoices)
            decisions: list[dict[str, Any]] = [stamp_strict_match(m) for m in strict_matches]
            pending: list[dict[str, Any]] = [d for d in decisions if d.get("status") == "pending_review"]
            rules_auto = sum(1 for d in decisions if d.get("status") == "reconciled")
            stage("reconciling", "completed", {
                "engine": "StrictMatcher",
                "matched": len(strict_matches),
                "auto_reconciled": rules_auto,
                "leftover_to_ai": len(leftover),
            })

            ai_suggestions = 0
            if use_ai and leftover:
                stage("ai_matching", "running")
                graph_out = asyncio.run(_run_graph(leftover, invoices, cfg, []))
                for prop in graph_out.get("reconciliation_results", []):
                    stamped = stamp_proposal(prop, invoices)
                    if stamped.get("status") == "reconciled":
                        decisions.append(stamped)
                    else:
                        pending.append(stamped)
                        decisions.append(stamped)
                    ai_suggestions += 1
                for prop in graph_out.get("pending_reviews", []):
                    stamped = stamp_proposal(prop, invoices)
                    pending.append(stamped)
                    decisions.append(stamped)
                    ai_suggestions += 1
                matched_ids = {d.get("paymentId") for d in decisions}
                leftover = [p for p in leftover if p["id"] not in matched_ids]
                stage("ai_matching", "completed", {
                    "provider": "mock" if use_ai else "off",
                    "suggestions": ai_suggestions,
                    "still_unmatched": len(leftover),
                })
            elif use_ai:
                stage("ai_matching", "completed", {"provider": "mock", "suggestions": 0, "still_unmatched": 0})
            else:
                stage("ai_matching", "skipped", {"provider": "off"})

            if leftover:
                for p in leftover:
                    unmatched = {
                        "paymentId": p["id"],
                        "invoiceId": None,
                        "status": "pending_review",
                        "confidence": 0.0,
                        "reasoning": "No deterministic or AI match — honest exception.",
                        "amountApplied": 0.0,
                        "paymentAmount": float(p.get("amount") or 0),
                        "reviewReason": "unmatched",
                        "source": "orchestrator",
                    }
                    pending.append(unmatched)
                    decisions.append(unmatched)

            # Deduplicate pending by paymentId (keep highest confidence)
            pending_by_pay: dict[str, dict[str, Any]] = {}
            for item in pending:
                pid = str(item.get("paymentId"))
                prev = pending_by_pay.get(pid)
                if not prev or float(item.get("confidence") or 0) >= float(prev.get("confidence") or 0):
                    pending_by_pay[pid] = item
            pending = list(pending_by_pay.values())

            auto = [d for d in decisions if d.get("status") == "reconciled"]

            stage("investigating", "running")
            investigated = investigate_pending(pending, invoices)
            queue = ReviewQueue()
            exception_rows = []
            for item in investigated:
                rev = queue.enqueue_from_match(
                    payment_id=str(item.get("paymentId")),
                    invoice_id=item.get("invoiceId"),
                    confidence=float(item.get("confidence") or 0),
                    reason=str(item.get("category") or item.get("reviewReason") or "pending_review"),
                    reasoning=str(item.get("reasoning") or ""),
                    amount_applied=float(item.get("amountApplied") or 0),
                )
                exception_rows.append({
                    **rev.__dict__,
                    "candidates": item.get("candidates"),
                    "confidenceBreakdown": item.get("confidenceBreakdown"),
                    "category": item.get("category"),
                })
            store_items = []
            for er in exception_rows:
                reasoning = er.get("reasoning") or ""
                extra = ""
                if er.get("category"):
                    extra += f" | category={er['category']}"
                if er.get("confidenceBreakdown"):
                    extra += f" | confidence={er['confidenceBreakdown']}"
                if er.get("candidates"):
                    extra += f" | candidates={er['candidates']}"
                store_items.append({
                    "id": er["id"],
                    "payment_id": er["payment_id"],
                    "invoice_id": er.get("invoice_id"),
                    "confidence": er["confidence"],
                    "reason": er.get("reason"),
                    "reasoning": reasoning + extra,
                    "amount_applied": er.get("amount_applied") or 0,
                    "status": er.get("status") or "pending",
                    "created_at": er.get("created_at"),
                })
            self.db.save_exceptions(run_id, store_items)
            stage("investigating", "completed", {"exceptions": len(store_items)})

            self.db.save_results(run_id, decisions)

            stage("evaluating", "running")
            gt_path = DEMO_DIR / "ground_truth.csv"
            gt = load_ground_truth(gt_path) if gt_path.exists() else []
            eval_metrics = evaluate_against_ground_truth(decisions, gt) if gt else {
                "accuracy": None, "precision": None, "recall": None, "f1": None, "details": [],
            }

            pay_by_id = {p["id"]: p for p in payments_norm}
            unreconciled_amount = 0.0
            for d in pending:
                pid = str(d.get("paymentId"))
                unreconciled_amount += float(
                    d.get("paymentAmount")
                    or pay_by_id.get(pid, {}).get("amount")
                    or 0
                )

            elapsed_s = max(time.perf_counter() - t0, 0.001)
            match_rate = (len(auto) / len(payments_norm)) if payments_norm else 0.0
            throughput = round(len(payments_norm) / elapsed_s, 2)
            cash = _cash_position(invoices, payments_norm, decisions)

            metrics = {
                "accuracy": eval_metrics.get("accuracy"),
                "precision": eval_metrics.get("precision"),
                "recall": eval_metrics.get("recall"),
                "f1": eval_metrics.get("f1"),
                "match_rate": round(match_rate, 4),
                "auto_reconciled": len(auto),
                "pending_review": len(pending),
                "total_payments": len(payments_norm),
                "unreconciled_amount": round(unreconciled_amount, 2),
                "details": {
                    "confusion": {
                        "tp": eval_metrics.get("tp"),
                        "fp": eval_metrics.get("fp"),
                        "fn": eval_metrics.get("fn"),
                        "tn": eval_metrics.get("tn"),
                    },
                    "per_payment": eval_metrics.get("details") or [],
                    "throughput_per_sec": throughput,
                    "duration_sec": round(elapsed_s, 3),
                    "ai_enabled": use_ai,
                    "ai_suggestions": ai_suggestions,
                    "cash_position": cash,
                },
            }
            self.db.save_metrics(run_id, metrics)
            stage("evaluating", "completed", {
                "accuracy": metrics["accuracy"],
                "f1": metrics["f1"],
                "match_rate": metrics["match_rate"],
                "throughput_per_sec": throughput,
            })

            summary = {
                "auto_reconciled": len(auto),
                "pending_review": len(pending),
                "total_decisions": len(decisions),
                "total_invoices": len(invoices_norm),
                "total_payments": len(payments_norm),
                "match_rate": metrics["match_rate"],
                "unreconciled_amount": metrics["unreconciled_amount"],
                "accuracy": metrics["accuracy"],
                "f1": metrics["f1"],
                "throughput_per_sec": throughput,
                "duration_sec": round(elapsed_s, 3),
                "cash_position": cash,
                "ai_enabled": use_ai,
            }
            self.db.finish_run(run_id, summary, status="completed")

            return {
                "run_id": run_id,
                "summary": summary,
                "stages": stages,
                "metrics": metrics,
                "decisions": decisions,
                "exceptions": store_items,
                "sources": bundle["sources"],
                "cash_position": cash,
            }
        except Exception as exc:
            logger.exception("Orchestrator failed")
            stage("error", "failed", {"error": str(exc)})
            self.db.finish_run(run_id, {"error": str(exc)}, status="failed")
            raise


def answer_finance_question(db: Database, question: str, run_id: str | None = None) -> dict[str, Any]:
    """Settlement / cash-position Q&A over the latest (or given) run."""
    run = db.get_run(run_id) if run_id else db.latest_run()
    if not run:
        return {"answer": "No reconciliation runs found yet. Start a run first.", "run_id": None}

    rid = run["id"]
    metrics = db.get_metrics(rid) or {}
    details = metrics.get("details") or {}
    cash = details.get("cash_position") or (run.get("summary") or {}).get("cash_position") or {}
    exceptions = db.list_exceptions(rid)
    pending = [e for e in exceptions if e["status"] == "pending"]
    q = question.lower().strip()

    if any(w in q for w in ("cash", "position", "exposure", "ar", "receivable")):
        answer = (
            f"Cash position for {rid}: applied INR {cash.get('cash_applied', 0):,.2f}, "
            f"open AR INR {cash.get('open_ar', 0):,.2f}, "
            f"unapplied cash INR {cash.get('unapplied_cash', 0):,.2f}, "
            f"net exposure INR {cash.get('net_exposure', 0):,.2f}."
        )
    elif any(w in q for w in ("unresolved", "pending", "exception")):
        top = ", ".join(e["payment_id"] for e in pending[:5]) or "none"
        answer = (
            f"{len(pending)} unresolved exception(s). "
            f"Unreconciled amount INR {metrics.get('unreconciled_amount', 0):,.2f}. "
            f"Top payments: {top}."
        )
    elif "throughput" in q or "how fast" in q or "speed" in q:
        answer = (
            f"Throughput {details.get('throughput_per_sec') or (run.get('summary') or {}).get('throughput_per_sec')} "
            f"payments/sec over {(run.get('summary') or {}).get('duration_sec')}s "
            f"for {metrics.get('total_payments')} payments."
        )
    elif "accuracy" in q or "f1" in q or "precision" in q or "recall" in q:
        answer = (
            f"Measured accuracy={metrics.get('accuracy')}, precision={metrics.get('precision')}, "
            f"recall={metrics.get('recall')}, F1={metrics.get('f1')} against labeled ground truth."
        )
    elif "match rate" in q or "matched" in q or "reconciled" in q:
        answer = (
            f"Auto-reconciled {metrics.get('auto_reconciled', 0)} of "
            f"{metrics.get('total_payments', 0)} payments "
            f"(match rate {float(metrics.get('match_rate') or 0):.1%}). "
            f"Honest exceptions: {metrics.get('pending_review', 0)}."
        )
    elif "ai" in q:
        answer = (
            f"AI matching was {'ON' if details.get('ai_enabled', True) else 'OFF'} "
            f"with {details.get('ai_suggestions', 0)} leftover suggestions (review-gated)."
        )
    elif "audit" in q:
        audit = db.list_audit(rid, limit=5)
        lines = [a["message"] for a in audit[:5]]
        answer = "Recent audit events:\n" + ("\n".join(f"- {l}" for l in lines) if lines else "None")
    else:
        summary = run.get("summary") or {}
        answer = (
            f"Run {rid}: match rate {float(summary.get('match_rate') or 0):.1%}, "
            f"accuracy {summary.get('accuracy')}, "
            f"{summary.get('pending_review')} exceptions, "
            f"throughput {summary.get('throughput_per_sec')}/s. "
            f"Ask about cash position, exceptions, match rate, accuracy, or throughput."
        )

    db.log_audit(rid, "qa", message=f"Q: {question}", payload={"answer": answer})
    return {"answer": answer, "run_id": rid, "question": question}
