"""Compose WorkspaceUI from an LLM plan + FinanceDB (real data only)."""

from __future__ import annotations

import json
from typing import Any

from src.agui.planner import WorkspacePlan
from src.agui.ui_schema import IntentObject, UIComponent, WorkspaceUI
from src.finance_controller.db import FinanceDB
from src.finance_controller.review_state import (
    exception_rows as _exception_rows_from_db,
    pending_review_items as _pending_from_db,
    reconciliation_summary as _recon_summary,
)


def _reviews_by_txn(db: FinanceDB, run_id: str) -> dict[str, dict[str, Any]]:
    return {r["transaction_id"]: r for r in db.fetch_reviews(run_id)}


def _sources_by_txn(db: FinanceDB, run_id: str) -> dict[str, dict[str, Any]]:
    return {r["transaction_id"]: r for r in db.fetch_sources(run_id)}


def _parse_json(raw: Any) -> Any:
    if raw is None:
        return {}
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(raw)
    except Exception:
        return {}


def _exception_rows(db: FinanceDB, run_id: str, *, pending_only: bool = False) -> list[dict[str, Any]]:
    if pending_only:
        return _pending_from_db(db, run_id)
    return _exception_rows_from_db(db, run_id, pending_only=False)


def _mismatch_rows(db: FinanceDB, run_id: str) -> list[dict[str, Any]]:
    sources = _sources_by_txn(db, run_id)
    reviews = _reviews_by_txn(db, run_id)
    mismatched: list[dict[str, Any]] = []
    for c in db.fetch_candidates(run_id):
        amt_score = c.get("amount_score")
        if amt_score is None or float(amt_score) >= 0.98:
            continue
        erp = sources.get(c["erp_txn_id"]) or {}
        counter = sources.get(c["counter_txn_id"]) or {}
        erp_amt = float(erp.get("amount_norm") or erp.get("amount") or 0)
        ctr_amt = float(counter.get("amount_norm") or counter.get("amount") or 0)
        mismatched.append(
            {
                "id": c["id"],
                "transaction_id": c["counter_txn_id"],
                "matched_txn_id": c["erp_txn_id"],
                "amount_score": float(amt_score),
                "erp_amount": erp_amt,
                "counter_amount": ctr_amt,
                "mismatch_amount": round(abs(erp_amt - ctr_amt), 2),
                "confidence": c.get("confidence"),
                "strategy": c.get("strategy"),
                "human_decision": (reviews.get(c["counter_txn_id"]) or {}).get("decision"),
            }
        )
    for e in db.fetch_exceptions(run_id):
        if str(e.get("exception_type") or "").upper() != "MISMATCH":
            continue
        tid = e["transaction_id"]
        if any(m["transaction_id"] == tid for m in mismatched):
            continue
        src = sources.get(tid) or {}
        mismatched.append(
            {
                "id": e["id"],
                "transaction_id": tid,
                "matched_txn_id": (_parse_json(e.get("details_json")) or {}).get("erp"),
                "amount_score": None,
                "erp_amount": None,
                "counter_amount": float(src.get("amount_norm") or src.get("amount") or 0),
                "mismatch_amount": float(src.get("amount_norm") or src.get("amount") or 0),
                "confidence": e.get("confidence"),
                "strategy": e.get("exception_type"),
                "human_decision": (reviews.get(tid) or {}).get("decision"),
                "ai_recommendation": e.get("recommended_action"),
            }
        )
    return mismatched


def clarification(message: str, *, txn_ref: str | None = None, reasoning: str | None = None) -> WorkspaceUI:
    return WorkspaceUI(
        intent=IntentObject(primary="clarification", secondary=[]),
        title="Need a bit more detail",
        status="clarification",
        message=message,
        reasoning=reasoning,
        txn_ref=txn_ref,
        components=[UIComponent(type="clarification", data={"message": message})],
    )


def unavailable(
    primary: str,
    title: str,
    message: str,
    *,
    reasoning: str | None = None,
    secondary: list[str] | None = None,
) -> WorkspaceUI:
    return WorkspaceUI(
        intent=IntentObject(primary=primary, secondary=secondary or []),
        title=title,
        status="unavailable",
        message=message,
        reasoning=reasoning,
        components=[UIComponent(type="unavailable", data={"message": message})],
    )


def _metrics_for_run(db: FinanceDB, run: dict[str, Any] | None) -> dict[str, Any]:
    """Authoritative metrics for a run: summary.evaluation from CrewAI, else latest DB row."""
    summary = (run or {}).get("summary") or {}
    for key in ("evaluation", "metrics"):
        block = summary.get(key)
        if isinstance(block, dict) and any(
            block.get(k) is not None for k in ("match_rate", "accuracy", "f1", "precision", "recall")
        ):
            return dict(block)
    rid = (run or {}).get("id")
    if rid:
        return dict(db.get_metrics(rid) or {})
    return {}


def _primary_wants(ctype: str, primary: str) -> bool:
    """Whether an empty component should still render an empty-state for this primary intent."""
    wants = {
        "exception_review": {"exception_table", "recommendation", "kpi"},
        "amount_mismatch": {"transaction_table", "comparison", "kpi"},
        "transaction_investigation": {"transaction_detail", "comparison", "recommendation", "metric_grid"},
        "finance_summary": {"metric_grid", "chart"},
        "cash_position": {"kpi"},
        "audit_review": {"audit_timeline", "kpi"},
        "reconciliation": {"metric_grid", "chart", "kpi", "exception_table"},
    }
    return ctype in wants.get(primary, set())


def _layout_for_primary(primary: str, requested: str | None) -> str:
    allowed = {"dashboard", "focused", "detail", "table"}
    raw = (requested or "").strip().lower()
    if raw in allowed:
        return raw
    return {
        "reconciliation": "dashboard",
        "finance_summary": "dashboard",
        "transaction_investigation": "detail",
        "amount_mismatch": "table",
        "exception_review": "focused",
        "cash_position": "focused",
        "audit_review": "focused",
    }.get(primary, "focused")


def _fill_component(
    ctype: str,
    *,
    db: FinanceDB,
    run_id: str | None,
    plan: WorkspacePlan,
    topics: list[str],
) -> UIComponent | None:
    """Materialize one whitelisted component from real DB data; None if irrelevant/unavailable."""
    primary = plan.intent.primary
    if ctype == "clarification":
        return UIComponent(
            type="clarification",
            data={"message": plan.actions.clarification_question or plan.reasoning},
        )

    if not run_id and ctype not in {"clarification", "unavailable"}:
        return None

    if not run_id:
        return None
    pending = _exception_rows(db, run_id, pending_only=True)
    all_exc = _exception_rows(db, run_id)
    need_mismatch = "amount_mismatch" in topics or ctype in {"transaction_table", "comparison"}
    mismatches = _mismatch_rows(db, run_id) if need_mismatch else []
    run = db.get_run(run_id)
    summary = (run or {}).get("summary") or {}
    persistence = summary.get("persistence") or {}
    # Same evaluation block the CrewAI summary_report embeds into final_report
    metrics = _metrics_for_run(db, run)

    if ctype == "kpi":
        # Pick KPIs relevant to topics from real counts/amounts only
        items: list[UIComponent] = []
        if "exception_review" in topics or "reconciliation" in topics:
            items.append(
                UIComponent(
                    type="kpi",
                    data={
                        "label": "Needs attention",
                        "value": len(pending),
                        "meta": "Pending human review",
                        "tone": "warn" if pending else "ok",
                    },
                )
            )
            items.append(
                UIComponent(
                    type="kpi",
                    data={
                        "label": "Total exceptions",
                        "value": len(all_exc),
                        "tone": "neutral",
                    },
                )
            )
        if "amount_mismatch" in topics:
            total = round(sum(float(m.get("mismatch_amount") or 0) for m in mismatches), 2)
            items.append(
                UIComponent(
                    type="kpi",
                    data={
                        "label": "Mismatch count",
                        "value": len(mismatches),
                        "tone": "warn" if mismatches else "ok",
                    },
                )
            )
            items.append(
                UIComponent(
                    type="kpi",
                    data={
                        "label": "Total mismatch amount",
                        "value": total,
                        "meta": "INR abs delta",
                        "tone": "warn" if total else "ok",
                    },
                )
            )
        if "cash_position" in topics:
            sources = db.fetch_sources(run_id)
            invoices = [s for s in sources if s["source"] == "erp"]
            payments = [s for s in sources if s["source"] in ("bank", "payment_processor")]
            cash_applied = persistence.get("total_reconciled_amount")
            unapplied = persistence.get("total_unmatched_amount")
            erp_total = sum(float(i.get("amount_norm") or i.get("amount") or 0) for i in invoices)
            open_ar = round(erp_total - float(cash_applied), 2) if cash_applied is not None else None
            items.extend(
                [
                    UIComponent(
                        type="kpi",
                        data={"label": "Cash applied", "value": cash_applied, "meta": "INR", "tone": "ok"},
                    ),
                    UIComponent(
                        type="kpi",
                        data={
                            "label": "Unapplied cash",
                            "value": unapplied,
                            "meta": "INR",
                            "tone": "warn" if unapplied else "ok",
                        },
                    ),
                    UIComponent(type="kpi", data={"label": "Open AR", "value": open_ar, "tone": "neutral"}),
                    UIComponent(type="kpi", data={"label": "Invoices", "value": len(invoices), "tone": "neutral"}),
                    UIComponent(type="kpi", data={"label": "Payments", "value": len(payments), "tone": "neutral"}),
                ]
            )
        if "audit_review" in topics:
            events = db.fetch_audit(run_id, 80)
            humanish = [
                e
                for e in events
                if str(e.get("event_type") or "")
                in {"exception_resolved", "human_review", "human_review_pending"}
            ]
            items.append(
                UIComponent(type="kpi", data={"label": "Audit events", "value": len(events), "tone": "neutral"})
            )
            items.append(
                UIComponent(
                    type="kpi",
                    data={"label": "Human-related events", "value": len(humanish), "tone": "neutral"},
                )
            )
        if "finance_summary" in topics or "reconciliation" in topics:
            if metrics.get("match_rate") is not None:
                mr = float(metrics.get("match_rate") or 0)
                items.append(
                    UIComponent(
                        type="kpi",
                        data={
                            "label": "Match rate",
                            "value": metrics.get("match_rate"),
                            "tone": "ok" if mr >= 0.85 else "warn",
                        },
                    )
                )
        if not items:
            return None
        return UIComponent(type="kpi", data={"_bundle": [c.data for c in items]})

    if ctype == "metric_grid":
        if "transaction_investigation" in topics and plan.txn_ref:
            ref = plan.txn_ref
            candidates = [
                c
                for c in db.fetch_candidates(run_id)
                if c["counter_txn_id"] == ref or c["erp_txn_id"] == ref
            ]
            best = max(candidates, key=lambda c: float(c.get("confidence") or 0), default=None)
            if not best:
                if _primary_wants(ctype, primary):
                    return UIComponent(
                        type="metric_grid",
                        data={
                            "title": "Confidence breakdown",
                            "empty": True,
                            "message": f"No scored candidates for {ref}.",
                            "metrics": [],
                        },
                    )
                return None
            return UIComponent(
                type="metric_grid",
                data={
                    "title": "Confidence breakdown",
                    "metrics": [
                        {"label": "Overall", "value": best.get("confidence")},
                        {"label": "Amount", "value": best.get("amount_score")},
                        {"label": "Merchant", "value": best.get("merchant_score")},
                        {"label": "Date", "value": best.get("date_score")},
                        {"label": "Reference", "value": best.get("reference_score")},
                    ],
                },
            )
        if not metrics and not persistence:
            return None
        results = db.fetch_results(run_id)
        summary = _recon_summary(db, run_id)
        return UIComponent(
            type="metric_grid",
            data={
                "title": "Run metrics",
                "metrics": [
                    {"label": "Match rate", "value": metrics.get("match_rate")},
                    {"label": "Accuracy", "value": metrics.get("accuracy")},
                    {"label": "Results", "value": summary.get("total_results", len(results))},
                    {"label": "Auto-matched", "value": summary.get("auto_matched")},
                    {"label": "Pending human review", "value": summary.get("pending_human_count", len(pending))},
                    {"label": "Approved", "value": summary.get("approved")},
                    {"label": "Exceptions", "value": summary.get("exceptions", len(all_exc))},
                    {
                        "label": "Reconciled amount",
                        "value": persistence.get("total_reconciled_amount"),
                    },
                ],
            },
        )

    if ctype == "chart":
        if not metrics:
            return None
        return UIComponent(
            type="chart",
            data={
                "kind": "quality",
                "title": "Quality scorecard",
                "rows": [
                    {"name": "Accuracy", "value": (metrics.get("accuracy") or 0) * 100},
                    {"name": "Precision", "value": (metrics.get("precision") or 0) * 100},
                    {"name": "Recall", "value": (metrics.get("recall") or 0) * 100},
                    {"name": "F1", "value": (metrics.get("f1") or 0) * 100},
                    {"name": "Match", "value": (metrics.get("match_rate") or 0) * 100},
                ],
            },
        )

    if ctype == "exception_table":
        rows = pending if pending else (all_exc if primary == "exception_review" else pending)
        columns = [
            "transaction_id",
            "amount",
            "source",
            "reason",
            "confidence",
            "exception_type",
            "ai_recommendation",
            "status",
        ]
        if not rows:
            if _primary_wants(ctype, primary):
                return UIComponent(
                    type="exception_table",
                    data={
                        "title": "Needs Your Attention",
                        "rows": [],
                        "empty": True,
                        "message": "No exceptions need attention right now.",
                        "columns": columns,
                    },
                )
            return None
        return UIComponent(
            type="exception_table",
            data={
                "title": "Needs Your Attention" if pending else "Exception history",
                "rows": rows,
                "columns": columns,
            },
        )

    if ctype == "recommendation":
        if "transaction_investigation" in topics and plan.txn_ref:
            ref = plan.txn_ref
            exc = next((e for e in db.fetch_exceptions(run_id) if e["transaction_id"] == ref), None)
            human = (_reviews_by_txn(db, run_id).get(ref) or {}).get("decision")
            return UIComponent(
                type="recommendation",
                data={
                    "title": "Recommended action",
                    "reason": (
                        f"{exc.get('exception_type')}: {exc.get('recommended_action')}"
                        if exc
                        else "No open exception for this transaction."
                    ),
                    "actions": [str((exc or {}).get("recommended_action") or "REVIEW").upper()],
                    "human_decision": human,
                    "exception_id": (exc or {}).get("id"),
                    "note": "Record a decision only when you are ready — nothing is written until you act.",
                },
            )
        if not pending:
            if _primary_wants(ctype, primary):
                return UIComponent(
                    type="recommendation",
                    data={
                        "title": "Recommendations",
                        "empty": True,
                        "message": "No pending recommendations.",
                        "actions": [],
                    },
                )
            return None
        return UIComponent(
            type="recommendation",
            data={
                "title": "AI suggestions",
                "actions": sorted(
                    {str(r.get("ai_recommendation") or "REVIEW").upper() for r in pending}
                ),
                "note": "Suggestions only — human decisions stay unset until you choose an action.",
                "pending_count": len(pending),
            },
        )

    if ctype == "transaction_table":
        if not mismatches:
            if _primary_wants(ctype, primary):
                return UIComponent(
                    type="transaction_table",
                    data={
                        "rows": [],
                        "empty": True,
                        "message": "No amount mismatches in this run.",
                        "filter": "amount_mismatch",
                    },
                )
            return None
        return UIComponent(
            type="transaction_table",
            data={
                "title": "Amount mismatches",
                "rows": mismatches,
                "filter": "amount_mismatch",
                "columns": [
                    "transaction_id",
                    "matched_txn_id",
                    "counter_amount",
                    "erp_amount",
                    "mismatch_amount",
                    "amount_score",
                    "confidence",
                ],
            },
        )

    if ctype == "comparison":
        if "transaction_investigation" in topics and plan.txn_ref:
            ref = plan.txn_ref
            candidates = [
                c
                for c in db.fetch_candidates(run_id)
                if c["counter_txn_id"] == ref or c["erp_txn_id"] == ref
            ]
            if not candidates:
                if _primary_wants(ctype, primary):
                    return UIComponent(
                        type="comparison",
                        data={
                            "title": "Candidate matches",
                            "empty": True,
                            "message": f"No candidate matches found for {ref}.",
                            "rows": [],
                        },
                    )
                return None
            return UIComponent(
                type="comparison",
                data={
                    "title": "Candidate matches",
                    "rows": [
                        {
                            "left": c["counter_txn_id"],
                            "right": c["erp_txn_id"],
                            "fields": {
                                "strategy": c.get("strategy"),
                                "confidence": c.get("confidence"),
                                "amount_score": c.get("amount_score"),
                                "merchant_score": c.get("merchant_score"),
                                "date_score": c.get("date_score"),
                                "reference_score": c.get("reference_score"),
                            },
                        }
                        for c in sorted(
                            candidates,
                            key=lambda x: float(x.get("confidence") or 0),
                            reverse=True,
                        )[:10]
                    ],
                },
            )
        if not mismatches:
            return None
        return UIComponent(
            type="comparison",
            data={
                "title": "Mismatch details",
                "rows": [
                    {
                        "left": m.get("transaction_id"),
                        "right": m.get("matched_txn_id"),
                        "fields": {
                            "counter_amount": m.get("counter_amount"),
                            "erp_amount": m.get("erp_amount"),
                            "amount_score": m.get("amount_score"),
                        },
                    }
                    for m in mismatches[:20]
                ],
            },
        )

    if ctype == "transaction_detail":
        if not plan.txn_ref:
            return None
        ref = plan.txn_ref
        sources = _sources_by_txn(db, run_id)
        src = sources.get(ref)
        exceptions = [e for e in db.fetch_exceptions(run_id) if e["transaction_id"] == ref]
        candidates = [
            c
            for c in db.fetch_candidates(run_id)
            if c["counter_txn_id"] == ref or c["erp_txn_id"] == ref
        ]
        if not src and not exceptions and not candidates:
            return None
        exc = exceptions[0] if exceptions else None
        human = (_reviews_by_txn(db, run_id).get(ref) or {}).get("decision")
        return UIComponent(
            type="transaction_detail",
            data={
                "title": f"Transaction {ref}",
                "transaction_id": ref,
                "found": src is not None,
                "source": (src or {}).get("source"),
                "amount": (src or {}).get("amount_norm") or (src or {}).get("amount"),
                "currency": (src or {}).get("currency_norm") or (src or {}).get("currency"),
                "merchant": (src or {}).get("merchant_norm") or (src or {}).get("merchant"),
                "date": (src or {}).get("date_norm") or (src or {}).get("txn_date"),
                "reference": (src or {}).get("reference_norm") or (src or {}).get("reference"),
                "human_decision": human,
                "ai_recommendation": (exc or {}).get("recommended_action"),
                "exception_type": (exc or {}).get("exception_type"),
                "exception_id": (exc or {}).get("id"),
            },
        )

    if ctype == "audit_timeline":
        events = db.fetch_audit(run_id, 80)
        if not events:
            if _primary_wants(ctype, primary):
                return UIComponent(
                    type="audit_timeline",
                    data={"events": [], "empty": True, "message": "No audit events recorded yet.", "title": "Audit trail"},
                )
            return None
        return UIComponent(
            type="audit_timeline",
            data={
                "title": "Audit trail",
                "events": [
                    {
                        "event_type": e.get("event_type"),
                        "message": e.get("message"),
                        "entity_id": e.get("entity_id"),
                        "created_at": e.get("created_at"),
                    }
                    for e in events[:40]
                ],
            },
        )

    if ctype == "agent_progress":
        return None

    return None


def compose_workspace(db: FinanceDB, plan: WorkspacePlan, run_id: str | None) -> WorkspaceUI:
    """Build structured AG-UI workspace state from LLM plan + real DB rows."""
    if plan.actions.needs_clarification or plan.intent.primary == "clarification":
        return clarification(
            plan.actions.clarification_question
            or "Could you clarify what you need in the finance workspace?",
            txn_ref=plan.txn_ref,
            reasoning=plan.reasoning,
        )

    topics = plan.topics()
    if plan.actions.run_reconciliation and "reconciliation" not in topics:
        topics = ["reconciliation", *topics]

    if not run_id:
        return unavailable(
            plan.intent.primary,
            plan.ui.title or "Workspace",
            "No reconciliation run available. Ask to run reconciliation first, or start one.",
            reasoning=plan.reasoning,
            secondary=plan.intent.secondary,
        )

    # Investigation with missing txn already handled in planner; still guard data miss
    if plan.intent.primary == "transaction_investigation" and plan.txn_ref:
        ref = plan.txn_ref
        sources = _sources_by_txn(db, run_id)
        has = (
            ref in sources
            or any(e["transaction_id"] == ref for e in db.fetch_exceptions(run_id))
            or any(
                c["counter_txn_id"] == ref or c["erp_txn_id"] == ref for c in db.fetch_candidates(run_id)
            )
        )
        if not has:
            return unavailable(
                "transaction_investigation",
                plan.ui.title or f"Investigation · {ref}",
                f"Transaction {ref} was not found in the database for this run.",
                reasoning=plan.reasoning,
            )

    components: list[UIComponent] = []
    seen_types: set[str] = set()
    requested = list(plan.ui.components)
    # Reconciliation / HITL views must always carry the pending queue, even if the
    # planner omitted exception_table — otherwise counts exist with no item list.
    if plan.intent.primary in {"reconciliation", "exception_review"} or "exception_review" in topics:
        if "exception_table" not in requested:
            requested.append("exception_table")
        if "kpi" not in requested:
            requested.insert(0, "kpi")
    for ctype in requested:
        if ctype in seen_types and ctype != "kpi":
            continue
        filled = _fill_component(ctype, db=db, run_id=run_id, plan=plan, topics=topics)
        if filled is None:
            continue
        if ctype == "kpi" and isinstance(filled.data.get("_bundle"), list):
            for item in filled.data["_bundle"]:
                components.append(UIComponent(type="kpi", data=item))
            seen_types.add("kpi")
        else:
            components.append(filled)
            seen_types.add(ctype)

    if not components:
        return unavailable(
            plan.intent.primary,
            plan.ui.title or "Workspace",
            "Required data is unavailable for the selected view.",
            reasoning=plan.reasoning,
            secondary=plan.intent.secondary,
        )

    layout = _layout_for_primary(plan.intent.primary, plan.ui.layout)

    run = db.get_run(run_id) if run_id else None
    evaluation = _metrics_for_run(db, run)
    pending_items = _pending_from_db(db, run_id)
    all_exc = _exception_rows_from_db(db, run_id)
    recon = _recon_summary(db, run_id)
    data_summary = {
        "topics": topics,
        "run_id": run_id,
        "component_types": [c.type for c in components],
        "layout": layout,
        "source_of_truth": ((run or {}).get("summary") or {}).get("source_of_truth")
        or ((run or {}).get("summary") or {}).get("engine")
        or "crewai-local",
        "evaluation": {
            k: evaluation.get(k)
            for k in ("accuracy", "precision", "recall", "f1", "match_rate", "false_match_rate")
            if evaluation.get(k) is not None
        },
        "pending_human_count": len(pending_items),
        "pending_review_items": pending_items,
        "exceptions": all_exc,
        "reconciliation_summary": recon,
    }

    return WorkspaceUI(
        intent=IntentObject(primary=plan.intent.primary, secondary=list(plan.intent.secondary)),
        title=plan.ui.title or "Finance workspace",
        status="ok",
        message=None,
        reasoning=plan.reasoning,
        components=components,
        run_id=run_id,
        txn_ref=plan.txn_ref,
        data=data_summary,
        layout=layout,
    )


# Backward-compatible helpers used by tests / older call sites
def build_exception_review(db: FinanceDB, run_id: str | None) -> WorkspaceUI:
    plan = WorkspacePlan.model_validate(
        {
            "intent": {"primary": "exception_review", "secondary": []},
            "reasoning": "exception_review",
            "confidence": 1,
            "actions": {},
            "ui": {
                "title": "Transactions Requiring Attention",
                "components": ["kpi", "exception_table", "recommendation"],
            },
        }
    )
    return compose_workspace(db, plan, run_id)


def build_amount_mismatch(db: FinanceDB, run_id: str | None) -> WorkspaceUI:
    plan = WorkspacePlan.model_validate(
        {
            "intent": {"primary": "amount_mismatch", "secondary": []},
            "reasoning": "amount_mismatch",
            "confidence": 1,
            "actions": {},
            "ui": {
                "title": "Amount mismatches",
                "components": ["kpi", "transaction_table", "comparison"],
            },
        }
    )
    return compose_workspace(db, plan, run_id)


def build_transaction_investigation(
    db: FinanceDB, run_id: str | None, txn_ref: str | None
) -> WorkspaceUI:
    plan = WorkspacePlan.model_validate(
        {
            "intent": {"primary": "transaction_investigation", "secondary": []},
            "reasoning": "transaction_investigation",
            "confidence": 1,
            "actions": {},
            "txn_ref": txn_ref,
            "ui": {
                "title": f"Investigation · {txn_ref or ''}",
                "components": [
                    "transaction_detail",
                    "comparison",
                    "metric_grid",
                    "recommendation",
                ],
            },
        }
    )
    return compose_workspace(db, plan, run_id)


def build_finance_summary(db: FinanceDB, run_id: str | None) -> WorkspaceUI:
    plan = WorkspacePlan.model_validate(
        {
            "intent": {"primary": "finance_summary", "secondary": []},
            "reasoning": "finance_summary",
            "confidence": 1,
            "actions": {},
            "ui": {"title": "Finance summary", "components": ["metric_grid", "chart"]},
        }
    )
    return compose_workspace(db, plan, run_id)


def build_cash_position(db: FinanceDB, run_id: str | None) -> WorkspaceUI:
    plan = WorkspacePlan.model_validate(
        {
            "intent": {"primary": "cash_position", "secondary": []},
            "reasoning": "cash_position",
            "confidence": 1,
            "actions": {},
            "ui": {"title": "Cash position", "components": ["kpi"]},
        }
    )
    return compose_workspace(db, plan, run_id)


def build_audit_review(db: FinanceDB, run_id: str | None) -> WorkspaceUI:
    plan = WorkspacePlan.model_validate(
        {
            "intent": {"primary": "audit_review", "secondary": []},
            "reasoning": "audit_review",
            "confidence": 1,
            "actions": {},
            "ui": {"title": "Audit review", "components": ["kpi", "audit_timeline"]},
        }
    )
    return compose_workspace(db, plan, run_id)


def build_reconciliation_result(db: FinanceDB, run_id: str | None) -> WorkspaceUI:
    plan = WorkspacePlan.model_validate(
        {
            "intent": {
                "primary": "reconciliation",
                "secondary": ["exception_review"],
            },
            "reasoning": "reconciliation complete",
            "confidence": 1,
            "actions": {},
            "ui": {
                "title": "Reconciliation complete — what needs attention",
                "layout": "dashboard",
                "components": [
                    "metric_grid",
                    "chart",
                    "kpi",
                    "exception_table",
                    "recommendation",
                ],
            },
        }
    )
    return compose_workspace(db, plan, run_id)
