"""Pending human-review state — one query shape for report, AG-UI, and dashboard."""

from __future__ import annotations

import json
from typing import Any

HITL_ACTIONS = frozenset({"REVIEW", "ESCALATE", "INVESTIGATE"})
DECISION_TO_RESULT_STATUS = {
    "APPROVED": "HUMAN_APPROVED",
    "RESOLVED": "HUMAN_RESOLVED",
    "REJECTED": "HUMAN_REJECTED",
    "ESCALATED": "UNMATCHED",
    "WRITE_OFF": "EXCEPTION_RESOLVED",
}


def _parse_json(raw: Any) -> Any:
    if raw is None:
        return {}
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(raw)
    except Exception:
        return {}


def _reason_for(exception: dict[str, Any]) -> str:
    etype = str(exception.get("exception_type") or "").strip()
    action = str(exception.get("recommended_action") or "").strip()
    details = _parse_json(exception.get("details_json") or exception.get("details"))
    bits = [p for p in (etype, action) if p]
    erp = details.get("erp") if isinstance(details, dict) else None
    if erp:
        bits.append(f"vs {erp}")
    strategy = details.get("strategy") if isinstance(details, dict) else None
    if strategy:
        bits.append(str(strategy))
    return " · ".join(bits) if bits else "Needs human review"


def pending_review_items(db: Any, run_id: str) -> list[dict[str, Any]]:
    """Actual pending HITL records. Count must equal len(this list)."""
    reviews = {r["transaction_id"] for r in db.fetch_reviews(run_id)}
    sources = {r["transaction_id"]: r for r in db.fetch_sources(run_id)}
    results_by_txn = {r["transaction_id"]: r for r in db.fetch_results(run_id)}
    items: list[dict[str, Any]] = []
    seen: set[str] = set()

    for exc in db.fetch_exceptions(run_id):
        tid = exc["transaction_id"]
        if tid in reviews or tid in seen:
            continue
        seen.add(tid)
        src = sources.get(tid) or {}
        result = results_by_txn.get(tid) or {}
        details = _parse_json(exc.get("details_json"))
        amount = float(
            src.get("amount_norm")
            or src.get("amount")
            or result.get("reconciled_amount")
            or 0
        )
        items.append(
            {
                "id": exc["id"],
                "transaction_id": tid,
                "matched_txn_id": result.get("matched_txn_id")
                or (details.get("erp") if isinstance(details, dict) else None),
                "amount": amount,
                "source": src.get("source") or result.get("strategy") or "unknown",
                "reason": _reason_for(exc),
                "confidence": exc.get("confidence"),
                "exception_type": exc.get("exception_type"),
                "ai_recommendation": exc.get("recommended_action"),
                "human_decision": None,
                "status": "HUMAN_REVIEW",
                "result_status": result.get("final_status") or "HUMAN_REVIEW",
            }
        )

    return items


def exception_rows(db: Any, run_id: str, *, pending_only: bool = False) -> list[dict[str, Any]]:
    reviews = {r["transaction_id"]: r for r in db.fetch_reviews(run_id)}
    sources = {r["transaction_id"]: r for r in db.fetch_sources(run_id)}
    results_by_txn = {r["transaction_id"]: r for r in db.fetch_results(run_id)}
    rows: list[dict[str, Any]] = []
    for exc in db.fetch_exceptions(run_id):
        tid = exc["transaction_id"]
        rev = reviews.get(tid)
        if pending_only and rev is not None:
            continue
        src = sources.get(tid) or {}
        result = results_by_txn.get(tid) or {}
        details = _parse_json(exc.get("details_json"))
        human = rev["decision"] if rev else None
        rows.append(
            {
                "id": exc["id"],
                "transaction_id": tid,
                "matched_txn_id": result.get("matched_txn_id")
                or (details.get("erp") if isinstance(details, dict) else None),
                "exception_type": exc.get("exception_type"),
                "confidence": exc.get("confidence"),
                "ai_recommendation": exc.get("recommended_action"),
                "human_decision": human,
                "amount": float(src.get("amount_norm") or src.get("amount") or 0),
                "source": src.get("source") or "unknown",
                "reason": _reason_for(exc),
                "details": details,
                "status": "HUMAN_REVIEW" if human is None else str(human),
            }
        )
    return rows


def reconciliation_summary(db: Any, run_id: str) -> dict[str, Any]:
    results = db.fetch_results(run_id)
    reviews = db.fetch_reviews(run_id)
    pending = pending_review_items(db, run_id)
    exceptions = db.fetch_exceptions(run_id)
    by_status: dict[str, int] = {}
    for r in results:
        st = str(r.get("final_status") or "UNKNOWN")
        by_status[st] = by_status.get(st, 0) + 1
    approved = sum(1 for r in reviews if str(r.get("decision") or "").upper() == "APPROVED")
    auto_matched = by_status.get("AUTO_MATCHED", 0)
    return {
        "total_results": len(results),
        "auto_matched": auto_matched,
        "pending_human": len(pending),
        "pending_human_count": len(pending),
        "exceptions": len(exceptions),
        "approved": approved,
        "results_by_status": by_status,
        "item_ids": [p["transaction_id"] for p in pending],
    }
