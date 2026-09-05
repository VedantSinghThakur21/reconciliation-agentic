"""Single source of truth helpers: AMP run → local dashboard payload."""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from typing import Any

# Same conceptual pipeline as the deployed CrewAI Finance Controller flow
AMP_STAGE_ORDER = [
    "conversational_intake",
    "extract_bank_pdf",
    "ingest_and_validate",
    "normalize",
    "generate_candidates",
    "score_candidates",
    "route_decisions",
    "investigate_exceptions",
    "human_review",
    "persist_results",
    "create_journal_entries",
    "evaluate",
    "summary_report",
]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def extract_report_text(amp_status: dict[str, Any]) -> str:
    flow = extract_flow_output(amp_status)
    if flow and isinstance(flow.get("final_report"), str) and flow["final_report"].strip():
        return flow["final_report"]
    result = amp_status.get("result")
    if isinstance(result, str) and result.strip():
        parsed = _as_flow_dict(result)
        if parsed and isinstance(parsed.get("final_report"), str) and parsed["final_report"].strip():
            return parsed["final_report"]
        return result
    if isinstance(result, dict):
        raw = result.get("raw")
        if isinstance(raw, str) and raw.strip():
            return raw
        fr = result.get("final_report")
        if isinstance(fr, str) and fr.strip():
            return fr
        return json.dumps(result, indent=2, default=str)
    if amp_status.get("result_json") is not None:
        return json.dumps(amp_status["result_json"], indent=2, default=str)
    return str(amp_status.get("output") or "")


def _strip_json_fence(text: str) -> str:
    """Unwrap ```json ... ``` wrappers AMP often puts around FlowOutput."""
    raw = (text or "").strip()
    if not raw:
        return raw
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", raw, re.I | re.S)
    if fenced:
        return fenced.group(1).strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw, count=1, flags=re.I)
        raw = re.sub(r"\s*```\s*$", "", raw)
    return raw.strip()


def _as_flow_dict(obj: Any) -> dict[str, Any] | None:
    """Return dict if it looks like CrewAI FlowOutput structured JSON."""
    if isinstance(obj, str):
        text = _strip_json_fence(obj)
        if not text:
            return None
        # Find first JSON object if prose surrounds it
        if text[0] not in "{[":
            m = re.search(r"\{.*\}", text, re.S)
            if not m:
                return None
            text = m.group(0)
        try:
            obj = json.loads(text)
        except json.JSONDecodeError:
            # Truncated / trailing junk — try from first { to last }
            try:
                start = text.index("{")
                end = text.rindex("}") + 1
                obj = json.loads(text[start:end])
            except (ValueError, json.JSONDecodeError):
                return None
    if not isinstance(obj, dict):
        return None
    # Nested wrappers common on AMP / TaskOutput
    for key in ("structured_output", "flow_output", "output", "data", "result"):
        nested = obj.get(key)
        if isinstance(nested, dict) and _looks_like_flow_output(nested):
            return nested
        if isinstance(nested, str):
            parsed = _as_flow_dict(nested)
            if parsed:
                return parsed
    if _looks_like_flow_output(obj):
        return obj
    raw = obj.get("raw")
    if isinstance(raw, str):
        return _as_flow_dict(raw)
    return None


def _looks_like_flow_output(obj: dict[str, Any]) -> bool:
    markers = (
        "pending_review_items",
        "evaluation",
        "persistence",
        "results",
        "exceptions",
        "candidates",
        "final_report",
        "ar_ap_mode",
    )
    hits = sum(1 for k in markers if k in obj)
    return hits >= 2


def extract_flow_output(amp_status: dict[str, Any]) -> dict[str, Any] | None:
    """
    Prefer CrewAI structured FlowOutput JSON over markdown scraping.

    Accepts AMP status payloads where the contract lives in result / result_json /
    output / steps[*].output (structured_output step), or a previously persisted
    summary.flow_output on re-finalize.
    """
    cached = amp_status.get("flow_output")
    if isinstance(cached, dict) and _looks_like_flow_output(cached):
        return cached

    blobs: list[Any] = [
        amp_status.get("result"),
        amp_status.get("result_json"),
        amp_status.get("output"),
        amp_status.get("final_output"),
        amp_status.get("structured_output"),
    ]
    steps = amp_status.get("steps") or amp_status.get("tasks") or []
    if isinstance(steps, list):
        for step in steps:
            if not isinstance(step, dict):
                continue
            name = str(step.get("name") or step.get("step_name") or "").lower()
            if "structured" in name or name in {"summary_report", "output"}:
                blobs.insert(0, step.get("output") or step.get("result") or step.get("output_summary"))
            else:
                blobs.append(step.get("output") or step.get("result"))

    for blob in blobs:
        parsed = _as_flow_dict(blob)
        if parsed:
            return parsed
    return None


def _metric_number(raw: Any) -> float | None:
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        val = float(raw)
        if val > 1.0 and val <= 100.0:
            return round(val / 100.0, 4)
        return round(val, 4)
    return _parse_metric_cell(str(raw))


def metrics_from_flow_output(flow: dict[str, Any]) -> dict[str, Any]:
    """Map FlowOutput.evaluation (+ persistence status counts) into local metrics."""
    ev = flow.get("evaluation") if isinstance(flow.get("evaluation"), dict) else {}
    pers = flow.get("persistence") if isinstance(flow.get("persistence"), dict) else {}
    by_status = pers.get("results_by_status") if isinstance(pers.get("results_by_status"), dict) else {}

    metrics: dict[str, Any] = {}
    mapping = {
        "accuracy": "accuracy",
        "precision": "precision",
        "recall": "recall",
        "f1": "f1_score",
        "match_rate": "match_rate",
        "false_match_rate": "false_match_rate",
        "tp": "true_positives",
        "fp": "false_positives",
        "fn": "false_negatives",
        "tn": "true_negatives",
    }
    for local, remote in mapping.items():
        parsed = _metric_number(ev.get(remote) if remote in ev else ev.get(local))
        if parsed is not None:
            metrics[local] = parsed

    auto = by_status.get("AUTO_MATCHED")
    if auto is None:
        auto = flow.get("results_count")  # not ideal; prefer status
    try:
        if auto is not None:
            metrics["auto_reconciled"] = int(auto)
    except (TypeError, ValueError):
        pass
    # Also count AUTO_MATCHED rows if array present
    results = flow.get("results") if isinstance(flow.get("results"), list) else []
    auto_rows = sum(1 for r in results if str((r or {}).get("final_status") or "").upper() == "AUTO_MATCHED")
    if auto_rows and not metrics.get("auto_reconciled"):
        metrics["auto_reconciled"] = auto_rows

    pending_reported = ev.get("pending_human_count")
    if pending_reported is None:
        pending_reported = flow.get("pending_review_items_count")
    try:
        if pending_reported is not None:
            metrics["pending_review_reported"] = int(pending_reported)
    except (TypeError, ValueError):
        pass

    exc_n = flow.get("exceptions_count")
    try:
        if exc_n is not None:
            metrics["exception_count"] = int(exc_n)
    except (TypeError, ValueError):
        pass

    if metrics.get("match_rate") is None and metrics.get("auto_reconciled") is not None:
        auto_n = int(metrics["auto_reconciled"])
        pending_n = int(
            len(flow.get("pending_review_items") or [])
            or metrics.get("pending_review_reported")
            or by_status.get("PENDING_HUMAN")
            or 0
        )
        denom = auto_n + pending_n
        total_results = pers.get("total_results") or flow.get("results_count")
        try:
            if total_results and int(total_results) > 0:
                denom = int(total_results)
        except (TypeError, ValueError):
            pass
        if denom > 0:
            metrics["match_rate"] = round(auto_n / denom, 4)
            metrics["match_rate_derived"] = True

    # Source aggregates → ingestion
    ingestion: dict[str, int] = {}
    for row in flow.get("sources") or []:
        if not isinstance(row, dict):
            continue
        src = str(row.get("source") or "").lower()
        try:
            count = int(row.get("record_count") or 0)
        except (TypeError, ValueError):
            continue
        if src in {"erp", "bank", "payment_processor", "invoice"}:
            ingestion[src] = count
    if ingestion:
        metrics["ingestion"] = ingestion

    note = ev.get("metrics_note")
    if note and str(note).upper() not in {"N/A", "NONE", ""}:
        metrics["metrics_note"] = str(note)
    return metrics


def pending_records_from_flow_output(flow: dict[str, Any]) -> list[dict[str, Any]]:
    """Build pending HITL records from pending_review_items / exceptions arrays."""
    records: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(row: dict[str, Any]) -> None:
        tid = (
            row.get("transaction_id")
            or row.get("item_id")
            or row.get("exception_id")
            or row.get("candidate_id")
            or row.get("erp_id")
        )
        if not _is_real_txn_id(tid):
            return
        tid_s = str(tid).strip()
        if tid_s in seen:
            return
        seen.add(tid_s)
        conf = row.get("confidence_score", row.get("confidence"))
        try:
            conf_f = float(conf or 0)
        except (TypeError, ValueError):
            conf_f = 0.0
        records.append(
            {
                "transaction_id": tid_s,
                "exception_type": str(
                    row.get("exception_type") or row.get("item_type") or "HUMAN_REVIEW"
                ),
                "recommended_action": _normalize_recommended_action(
                    row.get("recommended_action") or "needs_human_review"
                ),
                "confidence": conf_f,
                "amount": float(row.get("amount") or row.get("outstanding_balance") or 0),
                "source": row.get("source") or "amp",
                "matched_txn_id": row.get("matched_id") or row.get("matched_txn_id") or row.get("bank_or_pp_id"),
                "erp_id": row.get("erp_id") or row.get("source_id"),
                "details": {
                    **row,
                    "reason": row.get("reason") or row.get("decision_notes"),
                    "merchant": row.get("merchant") or row.get("source_merchant"),
                    "item_type": row.get("item_type"),
                },
            }
        )

    for row in flow.get("pending_review_items") or []:
        if isinstance(row, dict):
            add(row)
    if not records:
        for row in flow.get("exceptions") or []:
            if not isinstance(row, dict):
                continue
            action = str(row.get("recommended_action") or "").lower()
            if action in {"needs_human_review", "needs_writeoff_authorization", "needs_escalation", "review"}:
                add(row)
    if not records:
        for row in flow.get("results") or []:
            if not isinstance(row, dict):
                continue
            if str(row.get("final_status") or "").upper() in {"PENDING_HUMAN", "HUMAN_REVIEW", "ESCALATED"}:
                add(
                    {
                        "item_id": row.get("erp_id") or row.get("result_id"),
                        "item_type": "result",
                        "amount": row.get("reconciled_amount") or row.get("outstanding_balance"),
                        "confidence_score": row.get("confidence_score"),
                        "exception_type": "PENDING_HUMAN",
                        "reason": row.get("decision_notes"),
                        "recommended_action": "needs_human_review",
                        "matched_id": row.get("matched_id"),
                        "erp_id": row.get("erp_id"),
                    }
                )
    return records


STATUS_MAP = {
    "PENDING_HUMAN": "HUMAN_REVIEW",
    "HUMAN_REVIEW": "HUMAN_REVIEW",
    "AUTO_MATCHED": "AUTO_MATCHED",
    "HUMAN_APPROVED": "HUMAN_APPROVED",
    "HUMAN_REJECTED": "HUMAN_REJECTED",
    "HUMAN_RESOLVED": "HUMAN_RESOLVED",
    "WRITE_OFF_AUTHORIZED": "EXCEPTION_RESOLVED",
    "ESCALATED": "UNMATCHED",
    "ADVANCE_PAYMENT_HELD": "UNMATCHED",
    "UNMATCHED": "UNMATCHED",
    "EXCEPTION_RESOLVED": "EXCEPTION_RESOLVED",
}


def _persist_structured_flow(db: Any, *, run_id: str, flow: dict[str, Any]) -> dict[str, int]:
    """Write FlowOutput arrays into local tables. Returns write counts."""
    from src.finance_controller.utils import new_id

    counts = {
        "sources": 0,
        "candidates": 0,
        "results": 0,
        "exceptions": 0,
        "journals": 0,
        "reviews": 0,
    }

    # Row-level sources only (aggregates have record_count without transaction_id)
    source_rows = []
    for row in flow.get("sources") or []:
        if not isinstance(row, dict):
            continue
        # Skip aggregate summary rows: {source, record_count, total_amount}
        if row.get("record_count") is not None and row.get("transaction_id") is None and row.get("amount") is None:
            continue
        tid = row.get("transaction_id") or row.get("erp_id") or row.get("id")
        if not tid:
            continue
        src = str(row.get("source") or "amp").lower()
        if src not in {"erp", "bank", "payment_processor", "invoice"}:
            src = "bank"
        amount = float(row.get("amount") or row.get("total_amount") or 0)
        source_rows.append(
            {
                "id": new_id("SRC"),
                "source": "erp" if src == "invoice" else src,
                "transaction_id": str(tid),
                "txn_date": row.get("txn_date") or row.get("date"),
                "amount": amount,
                "amount_norm": amount,
                "currency": row.get("currency") if row.get("currency") not in (None, "N/A") else "INR",
                "currency_norm": row.get("currency") if row.get("currency") not in (None, "N/A") else "INR",
                "merchant": row.get("merchant") or "",
                "merchant_norm": row.get("merchant") or "",
                "reference": row.get("reference") or "",
                "raw": {**row, "from": "amp_structured"},
            }
        )
    if source_rows:
        db.insert_sources(source_rows, run_id=run_id)
        counts["sources"] = len(source_rows)

    cand_rows = []
    for row in flow.get("candidates") or []:
        if not isinstance(row, dict):
            continue
        erp = row.get("source_id") or row.get("erp_txn_id") or row.get("erp_id")
        counter = row.get("bank_or_pp_id") or row.get("counter_txn_id") or row.get("matched_id")
        if not erp or not counter:
            continue
        cand_rows.append(
            {
                "id": str(row.get("candidate_id") or new_id("CAND")),
                "erp_txn_id": str(erp),
                "counter_txn_id": str(counter),
                "strategy": row.get("strategy") or "amp",
                "amount_score": row.get("amount_score"),
                "merchant_score": row.get("merchant_score"),
                "date_score": row.get("date_score"),
                "reference_score": row.get("reference_score"),
                "confidence": row.get("confidence_score", row.get("confidence")),
                "evidence": row,
            }
        )
    if cand_rows:
        db.insert_candidates(cand_rows, run_id=run_id)
        counts["candidates"] = len(cand_rows)

    result_rows = []
    for row in flow.get("results") or []:
        if not isinstance(row, dict):
            continue
        tid = row.get("erp_id") or row.get("transaction_id") or row.get("result_id")
        if not tid:
            continue
        remote_status = str(row.get("final_status") or "UNMATCHED").upper()
        result_rows.append(
            {
                "id": str(row.get("result_id") or new_id("RES")),
                "transaction_id": str(tid),
                "matched_txn_id": row.get("matched_id") or row.get("matched_txn_id"),
                "final_status": STATUS_MAP.get(remote_status, remote_status),
                "confidence": row.get("confidence_score", row.get("confidence")),
                "reconciled_amount": float(row.get("reconciled_amount") or 0),
                "strategy": row.get("strategy") or row.get("source_type") or "amp",
                "details": row,
            }
        )
    if result_rows:
        db.insert_results(result_rows, run_id=run_id)
        counts["results"] = len(result_rows)

    exc_rows = []
    for row in flow.get("exceptions") or []:
        if not isinstance(row, dict):
            continue
        tid = row.get("erp_id") or row.get("exception_id") or row.get("transaction_id")
        if not tid:
            continue
        exc_rows.append(
            {
                "id": str(row.get("exception_id") or new_id("EXC")),
                "transaction_id": str(tid),
                "exception_type": row.get("exception_type") or "HUMAN_REVIEW",
                "recommended_action": _normalize_recommended_action(row.get("recommended_action")),
                "confidence": row.get("confidence_score", row.get("confidence")),
                "details": row,
            }
        )
    if exc_rows:
        db.insert_exceptions(exc_rows, run_id=run_id)
        counts["exceptions"] = len(exc_rows)

    journal_rows = []
    for row in flow.get("journals") or []:
        if not isinstance(row, dict):
            continue
        journal_rows.append(
            {
                "id": str(row.get("entry_id") or new_id("JE")),
                "entry_type": row.get("journal_type") or row.get("entry_type") or "AR",
                "status": row.get("status") or "DRAFT",
                "debit_account": row.get("debit_account") or "",
                "credit_account": row.get("credit_account") or "",
                "amount": float(row.get("amount") or 0),
                "currency": row.get("currency") or "INR",
                "transaction_id": row.get("erp_id") or row.get("transaction_id"),
                "memo": row.get("description") or row.get("memo"),
            }
        )
    if journal_rows:
        db.insert_journals(journal_rows, run_id=run_id)
        counts["journals"] = len(journal_rows)

    review_rows = []
    for row in flow.get("human_decisions") or []:
        if not isinstance(row, dict):
            continue
        tid = row.get("item_id") or row.get("transaction_id")
        if not tid:
            continue
        decision = str(row.get("decision") or "").upper()
        decision_map = {
            "APPROVE": "APPROVED",
            "APPROVED": "APPROVED",
            "REJECT": "REJECTED",
            "REJECTED": "REJECTED",
            "RESOLVE": "RESOLVED",
            "RESOLVED": "RESOLVED",
            "AUTHORIZE_WRITEOFF": "WRITE_OFF",
            "WRITE_OFF": "WRITE_OFF",
            "ESCALATE": "ESCALATED",
            "ESCALATED": "ESCALATED",
        }
        review_rows.append(
            {
                "id": new_id("REV"),
                "transaction_id": str(tid),
                "decision": decision_map.get(decision, decision or "APPROVED"),
                "reason": row.get("decision_rationale") or row.get("rationale") or row.get("reason"),
                "confidence": None,
            }
        )
    if review_rows:
        db.insert_reviews(review_rows, run_id=run_id)
        counts["reviews"] = len(review_rows)

    for ev in flow.get("audit_trail") or []:
        if not isinstance(ev, dict):
            continue
        db.audit(
            str(ev.get("event_type") or "amp_audit"),
            str(ev.get("notes") or ev.get("new_status") or ""),
            entity_id=str(ev.get("entity_id") or ""),
            run_id=run_id,
            payload=ev,
        )

    return counts


def _parse_metric_cell(raw: str) -> float | None:
    """Parse '88%', '0.88', or 'Not calculated' into a 0–1 float."""
    text = (raw or "").strip().lower()
    if not text or text in {"not calculated", "n/a", "-", "—", "none"}:
        return None
    m = re.search(r"([0-9]*\.?[0-9]+)\s*%", text)
    if m:
        return round(float(m.group(1)) / 100.0, 4)
    m = re.search(r"([0-9]*\.?[0-9]+)", text)
    if not m:
        return None
    val = float(m.group(1))
    if val > 1.0:
        val = val / 100.0
    return round(val, 4)


def parse_ingestion_from_report(report: str) -> dict[str, int]:
    """Parse Data Ingestion table counts (ERP / Bank / Payment Processor)."""
    out: dict[str, int] = {}
    patterns = {
        "erp": r"\|\s*ERP\s*\|\s*(\d+)",
        "bank": r"\|\s*Bank\s*\|\s*(\d+)",
        "payment_processor": r"\|\s*Payment\s*Processor\s*\|\s*(\d+)",
        "invoice": r"\|\s*Invoice\s*\|\s*(\d+)",
    }
    for key, pat in patterns.items():
        m = re.search(pat, report or "", re.I)
        if m:
            out[key] = int(m.group(1))
    return out


def parse_metrics_from_report(report: str) -> dict[str, Any]:
    """
    Read evaluation numbers from the CrewAI Financial Report text only.

    Does not recompute reconciliation — scrapes the same report the flow produced.
    When the evaluation table says "Not calculated", derives match_rate from
    confidence-band counts so the dashboard is not empty.
    """
    metrics: dict[str, Any] = {}
    text = report or ""
    for key, label in (
        ("accuracy", "Accuracy"),
        ("precision", "Precision"),
        ("recall", "Recall"),
        ("f1", r"F1(?:\s*Score)?"),
        ("match_rate", r"Match\s*Rate"),
        ("false_match_rate", r"False[- ]Match\s*Rate"),
    ):
        m = re.search(rf"{label}\s*\|\s*([^|\n]+)", text, re.I)
        if m:
            parsed = _parse_metric_cell(m.group(1))
            if parsed is not None:
                metrics[key] = parsed

    # Confidence band counts — named rows OR threshold-range rows with action labels
    auto = re.search(
        r"AUTO[_ -]?MATCH(?:ED)?[^|\n]*\|\s*[^|\n]*\|\s*(\d+)",
        text,
        re.I,
    )
    if not auto:
        auto = re.search(
            r"Auto[- ]Match(?:ed)?[^|\n]*\|\s*[^|\n]*\|\s*(\d+)",
            text,
            re.I,
        )
    # Newer shape: "| 0.95-1.0 | 0.95 | 1 | Auto-Match |"
    if not auto:
        auto = re.search(
            r"\|\s*0\.95[^|\n]*\|\s*[^|\n]*\|\s*(\d+)\s*\|\s*[^|\n]*Auto[- ]?Match",
            text,
            re.I,
        )
    if not auto:
        auto = re.search(
            r"\|\s*[^|\n]*\|\s*[^|\n]*\|\s*(\d+)\s*\|\s*Auto[- ]?Match",
            text,
            re.I,
        )
    human_band = re.search(
        r"Human\s*Review(?:\s*Required)?[^|\n]*\|\s*[^|\n]*\|\s*(\d+)",
        text,
        re.I,
    )
    if not human_band:
        human_band = re.search(
            r"\|\s*0\.80[^|\n]*\|\s*[^|\n]*\|\s*(\d+)\s*\|",
            text,
            re.I,
        )
    # Low band / exception: "| 0.0-0.79 | 0.79 | 2 | Review Needed |"
    low_band = re.search(
        r"\|\s*0\.0(?:\s*-\s*0\.79)?[^|\n]*\|\s*[^|\n]*\|\s*(\d+)\s*\|",
        text,
        re.I,
    )

    # AUTO-MATCHED BY SYSTEM bullet list (Candidate ID: 1)
    auto_section = re.search(
        r"AUTO[- ]MATCHED BY SYSTEM.*?(?=\n###|\n## |\Z)",
        text,
        re.I | re.S,
    )
    auto_candidates = []
    if auto_section:
        auto_candidates = re.findall(
            r"Candidate\s*ID\s*:\s*([A-Za-z0-9_-]+)",
            auto_section.group(0),
            re.I,
        )

    review = re.search(r"Pending Human Count\s*\**\s*:\s*\**\s*(\d+)", text, re.I)
    if not review:
        review = re.search(
            r"Pending Human Decisions\s*\|\s*[^|\n]*\|\s*(\d+)",
            text,
            re.I,
        )
    if not review:
        review = re.search(
            r"PENDING\s*\(awaiting action\)\s*\|\s*(\d+)",
            text,
            re.I,
        )
    if not review:
        review = re.search(r"\|\s*PENDING\s*\|\s*(\d+)", text, re.I)
    if not review:
        review = re.search(r"HUMAN_REVIEW\s*\|\s*[^|\n]*\|\s*(\d+)", text, re.I)
    exc = re.search(r"\|\s*EXCEPTION(?:\s*\([^)]*\))?\s*\|\s*[^|\n]*\|\s*(\d+)", text, re.I)
    if not exc:
        # Exception Analysis table total of type rows (best-effort)
        exc_types = re.findall(
            r"\|\s*(PARTIAL_SETTLEMENT|MISSING_ERP_RECORD|MISSING_BANK_RECORD|MISMATCH|MULTIPLE_CANDIDATES)\s*\|\s*(\d+)",
            text,
            re.I,
        )
        if exc_types:
            metrics["exception_count"] = sum(int(n) for _, n in exc_types)
    if auto:
        metrics["auto_reconciled"] = int(auto.group(1))
    elif auto_candidates:
        metrics["auto_reconciled"] = len(auto_candidates)
        metrics["auto_candidate_ids"] = auto_candidates
    if human_band:
        metrics["human_review_band"] = int(human_band.group(1))
    if low_band and "exception_count" not in metrics:
        metrics["exception_count"] = int(low_band.group(1))
    if review:
        # Reported count is untrusted until matching records are identified.
        metrics["pending_review_reported"] = int(review.group(1))
    if exc and "exception_count" not in metrics:
        metrics["exception_count"] = int(exc.group(1))

    total_results = re.search(
        r"Total Results(?:\s+in Reconciliation)?\s*:\s*\**\s*(\d+)",
        text,
        re.I,
    )
    if total_results:
        metrics["total_results_reported"] = int(total_results.group(1))

    # Derive match_rate when evaluation table left it blank
    if metrics.get("match_rate") is None and metrics.get("auto_reconciled") is not None:
        auto_n = int(metrics["auto_reconciled"])
        band_total = auto_n + int(metrics.get("human_review_band") or 0) + int(
            metrics.get("exception_count") or 0
        )
        pending_n = int(metrics.get("pending_review_reported") or 0)
        reported_total = metrics.get("total_results_reported")
        # Prefer confidence-band totals; AMP often reports inflated pending counts
        # and "Total Results: 0" even when bands have rows.
        if band_total > auto_n:
            denom = band_total
        elif reported_total:
            denom = reported_total
        elif pending_n:
            denom = auto_n + pending_n
        else:
            denom = auto_n
        if denom and int(denom) > 0:
            metrics["match_rate"] = round(auto_n / int(denom), 4)
            metrics["match_rate_derived"] = True

    ingestion = parse_ingestion_from_report(text)
    if ingestion:
        metrics["ingestion"] = ingestion
    return metrics


def completed_amp_stages() -> list[dict[str, Any]]:
    return [{"name": n, "status": "completed"} for n in AMP_STAGE_ORDER]


TXN_ID_RE = re.compile(r"\b((?:BNK|ERP|TXN|PAY|INV|PP)[-_][A-Z0-9]+)\b", re.I)
JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.I | re.S)


def _as_dict_list(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [v for v in value if isinstance(v, dict)]
    return []


def _normalize_recommended_action(raw: Any) -> str:
    text = str(raw or "REVIEW").strip().upper().replace(" ", "_")
    aliases = {
        "NEEDS_HUMAN_REVIEW": "REVIEW",
        "NEEDS_REVIEW": "REVIEW",
        "HUMAN_REVIEW": "REVIEW",
        "REVIEW_NEEDED": "REVIEW",
        "NEEDS_WRITEOFF_AUTHORIZATION": "WRITE_OFF",
        "AUTHORIZE_WRITEOFF": "WRITE_OFF",
        "INVESTIGATE": "INVESTIGATE",
        "ESCALATE": "ESCALATE",
    }
    return aliases.get(text, text if text else "REVIEW")


def _row_transaction_id(row: dict[str, Any]) -> str | None:
    for key in (
        "transaction_id",
        "candidate_id",
        "exception_id",
        "erp_id",
        "paymentId",
        "payment_id",
        "item_id",
        "id",
    ):
        val = row.get(key)
        if val is None or str(val).strip() == "":
            continue
        tid = str(val).strip()
        if not _is_real_txn_id(tid):
            continue
        # Exception IDs that are bare integers become EXC-<n>
        if key == "exception_id" and tid.isdigit():
            return f"EXC-{tid}"
        if key == "exception_id" and not tid.upper().startswith(("EXC-", "BNK-", "ERP-", "TXN-", "PAY-")):
            return f"EXC-{tid}"
        return tid
    return None


def _iter_json_objects_from_text(text: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if not text:
        return out
    for m in JSON_FENCE_RE.finditer(text):
        try:
            parsed = json.loads(m.group(1))
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            out.append(parsed)
    # Also try "Human Review: { ... }" without fences
    for m in re.finditer(r"Human Review:\s*(\{.*?\})\s*(?:\n[A-Z]|\Z)", text, re.I | re.S):
        try:
            parsed = json.loads(m.group(1))
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            out.append(parsed)
    return out


def _is_real_txn_id(tid: Any) -> bool:
    text = str(tid or "").strip()
    if not text:
        return False
    low = text.lower().strip("*")
    if low in {"n/a", "na", "none", "null", "-", "—", "unknown", "tbd"}:
        return False
    if low.startswith("exc-") and low[4:] in {"n/a", "na", "none", "null"}:
        return False
    return True


def _normalize_exception_tid(raw: str) -> str:
    tid = (raw or "").strip().strip("*").strip()
    if not _is_real_txn_id(tid):
        return ""
    if re.fullmatch(r"E\d+", tid, re.I):
        return f"EXC-{tid.upper()}"
    if tid.isdigit():
        return f"EXC-{tid}"
    if tid.upper().startswith("EXC-"):
        return tid.upper()
    return tid



def _extract_pending_from_markdown(report: str) -> list[dict[str, Any]]:
    """Parse AWAITING HUMAN DECISION markdown lists into pending records."""
    rows: list[dict[str, Any]] = []
    # Prefer the awaiting-human section when present
    section = report or ""
    m_sec = re.search(
        r"AWAITING HUMAN DECISION.*?(?=\n## |\n### [A-Z][A-Z ]+\n|\Z)",
        report or "",
        re.I | re.S,
    )
    if m_sec:
        section = m_sec.group(0)

    # Compact list: "- Exception IDs: 1, 3, 4" or "Exception IDs: 1, 3, 4"
    for m in re.finditer(
        r"Exception\s*IDs?\s*:\s*([0-9E,\s/-]+)",
        section,
        re.I,
    ):
        raw_list = m.group(1).strip()
        # Skip if this is a single multi-line block value like "E001" handled below
        ids = re.findall(r"(?:E\d+|\d+)", raw_list, re.I)
        if len(ids) >= 1 and ("," in raw_list or len(ids) > 1 or re.fullmatch(r"[\dE,\s/-]+", raw_list, re.I)):
            for token in ids:
                rows.append(
                    {
                        "transaction_id": _normalize_exception_tid(token),
                        "exception_type": "HUMAN_REVIEW",
                        "recommended_action": "REVIEW",
                        "reason": f"Exception ID {token}",
                        "confidence": 0,
                    }
                )
    if rows:
        return rows

    # Newer AMP shape: multi-line field blocks
    #   - Exception ID: E001
    #   - ERP ID: P002
    #   - Matched ID: B002
    #   - Exception Type: MISSING_BANK_RECORD
    block_re = re.compile(
        r"[-*]\s*Exception\s*ID(?!s)\s*:\s*(?P<exc>[^\n,]+)"
        r"(?:\n\s*[-*]?\s*ERP\s*ID\s*:\s*(?P<erp>[^\n]+))?"
        r"(?:\n\s*[-*]?\s*Matched\s*ID\s*:\s*(?P<matched>[^\n]+))?"
        r"(?:\n\s*[-*]?\s*Exception\s*Type\s*:\s*(?P<etype>[^\n]+))?",
        re.I,
    )
    for m in block_re.finditer(section):
        tid = _normalize_exception_tid(m.group("exc"))
        if not tid or "," in tid:
            continue
        etype = (m.group("etype") or "HUMAN_REVIEW").strip()
        erp = (m.group("erp") or "").strip() or None
        matched = (m.group("matched") or "").strip() or None
        rows.append(
            {
                "transaction_id": tid,
                "exception_type": etype,
                "recommended_action": "REVIEW",
                "reason": etype,
                "confidence": 0,
                "matched_txn_id": matched or erp,
                "erp_id": erp,
            }
        )
    if rows:
        return rows

    for m in re.finditer(
        r"^\s*[-*]\s+(?P<label>[^:\n]+?)\s*:\s*(?P<reason>.+)$",
        section,
        re.M,
    ):
        label = m.group("label").strip()
        reason = m.group("reason").strip()
        # Skip section headers / bold labels that are not items
        low = label.lower().lstrip("*").strip()
        if low.startswith(
            (
                "candidates pending",
                "pending exceptions",
                "confirmed",
                "auto-matched",
                "erp id",
                "matched id",
                "exception type",
                "exception ids",
            )
        ):
            continue
        if low == "exception id" or (low.startswith("exception id") and "ids" not in low):
            # "Exception ID 3: ..." or "Exception ID: E001"
            num = re.search(r"(\d+)", label)
            if "," in reason:
                for token in re.findall(r"(?:E\d+|\d+)", reason, re.I):
                    rows.append(
                        {
                            "transaction_id": _normalize_exception_tid(token),
                            "exception_type": "MISSING_ERP_RECORD",
                            "recommended_action": "REVIEW",
                            "reason": reason,
                            "confidence": 0,
                        }
                    )
                continue
            tid = _normalize_exception_tid(
                reason if re.search(r"[A-Za-z0-9_-]+", reason) and len(reason) < 40 else ""
            )
            if not tid or tid.lower() in {"review", "needs review"}:
                tid = f"EXC-{num.group(1)}" if num else _normalize_exception_tid(reason) or label
            # Prefer digit from label when reason is prose
            if num and (not reason or " " in reason.strip()):
                tid = f"EXC-{num.group(1)}"
            elif num and re.fullmatch(r"E?\d+", reason.strip(), re.I):
                tid = _normalize_exception_tid(reason.strip())
            rows.append(
                {
                    "transaction_id": tid,
                    "exception_type": "MISSING_ERP_RECORD",
                    "recommended_action": "REVIEW",
                    "reason": reason,
                    "confidence": 0,
                }
            )
            continue
        # Candidate-style labels such as "PAYPAL (123456)"
        if "(" in label or TXN_ID_RE.search(label):
            etype = "PARTIAL_SETTLEMENT" if "partial" in (label + reason).lower() else "HUMAN_REVIEW"
            rows.append(
                {
                    "transaction_id": label.lstrip("*").rstrip("*").strip(),
                    "exception_type": etype,
                    "recommended_action": "REVIEW",
                    "reason": reason,
                    "confidence": 0,
                }
            )
    return rows


def extract_pending_records_from_amp(amp_status: dict[str, Any], report: str) -> list[dict[str, Any]]:
    """Pull actual pending item records from AMP JSON / report. Never pads to a count."""
    blobs: list[Any] = [
        amp_status.get("result"),
        amp_status.get("result_json"),
        amp_status.get("output"),
        amp_status.get("final_output"),
    ]
    # CrewAI TaskOutput often nests under result.raw / result["raw"]
    for blob in list(blobs):
        if isinstance(blob, dict) and isinstance(blob.get("raw"), str):
            blobs.append(blob["raw"])
        if isinstance(blob, str):
            blobs.append(blob)

    records: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(row: dict[str, Any]) -> None:
        tid = _row_transaction_id(row)
        if not tid:
            return
        if tid in seen:
            return
        seen.add(tid)
        conf_raw = row.get("confidence")
        if conf_raw is None:
            conf_raw = row.get("confidence_score")
        try:
            conf = float(conf_raw or 0)
        except (TypeError, ValueError):
            conf = 0.0
        etype = (
            row.get("exception_type")
            or row.get("reason")
            or ("MISSING_ERP_RECORD" if tid.upper().startswith("EXC-") else "HUMAN_REVIEW")
        )
        records.append(
            {
                "transaction_id": tid,
                "exception_type": str(etype),
                "recommended_action": _normalize_recommended_action(
                    row.get("ai_recommendation") or row.get("recommended_action") or "REVIEW"
                ),
                "confidence": conf,
                "amount": row.get("amount") or row.get("amountApplied") or 0,
                "source": row.get("source") or "amp",
                "matched_txn_id": row.get("matched_txn_id")
                or row.get("invoiceId")
                or row.get("invoice_id")
                or row.get("matched_id"),
                "details": {
                    **{k: v for k, v in row.items() if k != "details"},
                    "reason": row.get("reason"),
                },
            }
        )

    def ingest_pending_container(obj: dict[str, Any]) -> None:
        for key in (
            "pending_review_items",
            "pending_reviews",
            "pending_items",
            "pending_exception_items",
            "exceptions",
            "human_review_items",
        ):
            for row in _as_dict_list(obj.get(key)):
                add(row)
        nested = obj.get("human_review") or obj.get("human_review_summary") or {}
        if isinstance(nested, dict):
            for key in ("pending_items", "pending_review_items", "pending_exception_items"):
                for row in _as_dict_list(nested.get(key)):
                    add(row)
            for tid in nested.get("item_ids") or []:
                add({"transaction_id": tid, "exception_type": "HUMAN_REVIEW", "recommended_action": "REVIEW"})

    for blob in blobs:
        parsed: Any = blob
        if isinstance(blob, str):
            # Fenced / embedded JSON first (Enterprise report embeds Human Review JSON)
            for obj in _iter_json_objects_from_text(blob):
                ingest_pending_container(obj)
            try:
                parsed = json.loads(blob)
            except json.JSONDecodeError:
                continue
        if not isinstance(parsed, dict):
            continue
        # TaskOutput-style { "raw": "..." }
        raw = parsed.get("raw")
        if isinstance(raw, str):
            for obj in _iter_json_objects_from_text(raw):
                ingest_pending_container(obj)
        ingest_pending_container(parsed)

    # Markdown report body
    for obj in _iter_json_objects_from_text(report or ""):
        ingest_pending_container(obj)

    ids_line = re.search(r"Item IDs:\s*(.+)", report or "", re.I)
    if ids_line:
        raw = ids_line.group(1).strip()
        if raw.lower() not in {"none", "n/a", "-", "[]"}:
            for tid in TXN_ID_RE.findall(raw):
                add(
                    {
                        "transaction_id": tid.upper().replace("_", "-"),
                        "exception_type": "HUMAN_REVIEW",
                        "recommended_action": "REVIEW",
                    }
                )

    # If structured extraction found nothing, fall back to markdown lists
    if not records:
        for row in _extract_pending_from_markdown(report or ""):
            add(row)

    return records


def _persist_amp_pending(db: Any, *, run_id: str, records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not records:
        return []
    from src.finance_controller.utils import new_id

    existing_results = {r["transaction_id"] for r in db.fetch_results(run_id)}
    existing_sources = {s["transaction_id"] for s in db.fetch_sources(run_id)}
    existing_exc = {e["transaction_id"] for e in db.fetch_exceptions(run_id)}

    exceptions = []
    results = []
    sources = []
    for row in records:
        tid = row["transaction_id"]
        amount = float(row.get("amount") or 0)
        details = row.get("details") if isinstance(row.get("details"), dict) else {}
        reason = details.get("reason") or row.get("exception_type")
        matched = row.get("matched_txn_id") or details.get("matched_txn_id")
        if tid not in existing_exc:
            eid = new_id("EXC")
            exceptions.append(
                {
                    "id": eid,
                    "transaction_id": tid,
                    "exception_type": row.get("exception_type") or "HUMAN_REVIEW",
                    "recommended_action": row.get("recommended_action") or "REVIEW",
                    "confidence": row.get("confidence") or 0,
                    "details": {
                        **details,
                        "reason": reason,
                        "erp": row.get("erp_id"),
                        "matched_txn_id": matched,
                        "amp_source": True,
                    },
                }
            )
            existing_exc.add(tid)
        if tid not in existing_results:
            rid = new_id("RES")
            results.append(
                {
                    "id": rid,
                    "transaction_id": tid,
                    "matched_txn_id": matched,
                    "final_status": "HUMAN_REVIEW",
                    "confidence": row.get("confidence") or 0,
                    "reconciled_amount": amount,
                    "strategy": row.get("source") or "amp",
                    "details": {**details, "amp_source": True},
                }
            )
            existing_results.add(tid)
        if tid not in existing_sources:
            sid = new_id("SRC")
            # Prefer bank/erp-looking sources so dashboard invoice/payment filters work
            src_kind = "bank"
            low = tid.upper()
            if low.startswith(("ERP-", "INV-")) or (
                low.startswith("I") and not low.startswith("EXC")
            ):
                src_kind = "erp"
            elif low.startswith(("PP-", "PAY")):
                src_kind = "payment_processor"
            sources.append(
                {
                    "id": sid,
                    "source": row.get("source")
                    if row.get("source") in ("erp", "bank", "payment_processor")
                    else src_kind,
                    "transaction_id": tid,
                    "amount": amount,
                    "amount_norm": amount,
                    "currency": "INR",
                    "currency_norm": "INR",
                    "merchant": details.get("merchant") or tid,
                    "merchant_norm": details.get("merchant") or tid,
                    "raw": {"from": "amp_finalize", "reason": reason},
                }
            )
            existing_sources.add(tid)
    if sources:
        db.insert_sources(sources, run_id=run_id)
    if exceptions:
        db.insert_exceptions(exceptions, run_id=run_id)
    if results:
        db.insert_results(results, run_id=run_id)
    return exceptions


def _hydrate_demo_sources(db: Any, *, run_id: str, ar_ap_mode: str = "AR") -> dict[str, int]:
    """
    Load the same local demo feeds the UI used before AMP-only finalize.

    AMP cloud reports do not return source rows; without this the dashboard
    shows Invoices/Payments = 0 even when the pipeline completed.
    """
    from pathlib import Path

    from src.finance_controller.utils import new_id, read_csv, stub_erp_transactions

    root = Path(__file__).resolve().parents[2]
    demo = root / "data" / "demo"
    bank_path = demo / "bank.csv"
    pp_path = demo / "payment_processor.csv"
    if not bank_path.exists():
        return {}

    erp = stub_erp_transactions(24, mode=ar_ap_mode)
    bank = read_csv(bank_path)
    pp = read_csv(pp_path) if pp_path.exists() else []
    records: list[dict[str, Any]] = []

    def add(source: str, row: dict[str, Any]) -> None:
        tid = str(row.get("transaction_id") or "").strip()
        if not tid:
            return
        amount = float(row.get("amount") or 0)
        records.append(
            {
                "id": new_id("SRC"),
                "source": source,
                "transaction_id": tid,
                "txn_date": row.get("date"),
                "amount": amount,
                "amount_norm": amount,
                "currency": row.get("currency") or "INR",
                "currency_norm": row.get("currency") or "INR",
                "merchant": row.get("merchant") or "",
                "merchant_norm": row.get("merchant") or "",
                "reference": row.get("reference") or "",
                "reference_norm": row.get("reference") or "",
                "description": row.get("description") or "",
                "raw": {**row, "from": "amp_hydrate_demo"},
            }
        )

    for r in erp:
        add("erp", r)
    for r in bank:
        add("bank", r)
    for r in pp:
        add("payment_processor", r)

    if records:
        db.insert_sources(records, run_id=run_id)
    return {
        "erp": len(erp),
        "bank": len(bank),
        "payment_processor": len(pp),
    }


def _persist_auto_matched_stubs(
    db: Any,
    *,
    run_id: str,
    count: int,
    reserved_tids: set[str],
) -> float:
    """Materialize AUTO_MATCHED result rows so run metrics are not stuck at 0."""
    if count <= 0:
        return 0.0
    from src.finance_controller.utils import new_id

    payments = [
        s
        for s in db.fetch_sources(run_id)
        if s.get("source") in ("bank", "payment_processor")
        and s.get("transaction_id") not in reserved_tids
    ]
    results = []
    total = 0.0
    for i in range(min(count, len(payments) or count)):
        if i < len(payments):
            pay = payments[i]
            tid = pay["transaction_id"]
            amount = float(pay.get("amount_norm") or pay.get("amount") or 0)
        else:
            tid = f"AUTO-{i+1:04d}"
            amount = 0.0
        total += amount
        results.append(
            {
                "id": new_id("RES"),
                "transaction_id": tid,
                "matched_txn_id": None,
                "final_status": "AUTO_MATCHED",
                "confidence": 0.97,
                "reconciled_amount": amount,
                "strategy": "amp",
                "details": {"amp_source": True, "synthetic_auto": True},
            }
        )
    if results:
        db.insert_results(results, run_id=run_id)
    return round(total, 2)


def finalize_amp_run(
    db: Any,
    *,
    run_id: str,
    kickoff_id: str,
    amp_status: dict[str, Any],
    user_request: str = "",
    ar_ap_mode: str = "AR",
) -> dict[str, Any]:
    """
    Persist AMP completion under its own run_id.

    Prefers CrewAI structured FlowOutput JSON when present; falls back to
    markdown report scraping. Does NOT wipe other runs.
    """
    flow = extract_flow_output(amp_status)
    report = extract_report_text(amp_status)

    if flow:
        metrics = metrics_from_flow_output(flow)
        pending_records = pending_records_from_flow_output(flow)
        # If structured arrays are empty, try markdown IDs as last resort
        if not pending_records:
            pending_records = extract_pending_records_from_amp(amp_status, report)
        stages_raw = flow.get("stages") if isinstance(flow.get("stages"), list) else None
        if stages_raw:
            stages = []
            for s in stages_raw:
                if isinstance(s, dict) and s.get("name"):
                    stages.append(
                        {
                            "name": s["name"],
                            "status": s.get("status") or "completed",
                            "detail": s.get("output_summary")
                            if isinstance(s.get("output_summary"), dict)
                            else {"summary": s.get("output_summary")},
                        }
                    )
            if not stages:
                stages = completed_amp_stages()
        else:
            stages = completed_amp_stages()
        mode = str(flow.get("ar_ap_mode") or ar_ap_mode or "AR").upper()
        flow_status = str(flow.get("status") or "").lower()
        remote_run_id = flow.get("run_id")
        persistence = flow.get("persistence") if isinstance(flow.get("persistence"), dict) else {}
        integrity = list(flow.get("data_integrity_warnings") or [])
        reported_pending = metrics.get("pending_review_reported")
        if reported_pending is not None and int(reported_pending) != len(pending_records):
            integrity.append(
                f"pending_human_count={reported_pending} but pending_review_items length={len(pending_records)}"
            )
    else:
        metrics = parse_metrics_from_report(report)
        pending_records = extract_pending_records_from_amp(amp_status, report)
        stages = completed_amp_stages()
        mode = ar_ap_mode
        flow_status = ""
        remote_run_id = None
        persistence = {}
        integrity = []

    state = str(amp_status.get("state") or flow_status or "SUCCESS").upper()
    status = (
        "completed"
        if state in {"SUCCESS", "COMPLETED", "SUCCEEDED", "DONE"} or flow_status == "completed"
        else "failed"
    )

    identified_pending = len(pending_records)
    metrics["pending_review"] = identified_pending

    ingestion = metrics.pop("ingestion", None) or parse_ingestion_from_report(report)
    summary: dict[str, Any] = {
        "engine": "crewai-amp",
        "kickoff_id": kickoff_id,
        "status": state,
        "source_of_truth": "crewai-amp",
        "amp_run_id": remote_run_id,
        "structured_flow": bool(flow),
        "evaluation": {
            k: metrics.get(k)
            for k in ("accuracy", "precision", "recall", "f1", "match_rate", "false_match_rate")
            if metrics.get(k) is not None
        },
        "metrics": metrics,
        "pending_review": identified_pending,
        "pending_human_count": identified_pending,
        "pending_review_items": pending_records,
        "auto_reconciled": metrics.get("auto_reconciled"),
        "auto_match_count": metrics.get("auto_reconciled"),
        "exception_count": metrics.get("exception_count"),
        "ingestion": ingestion,
        "data_integrity_warnings": integrity,
    }
    if flow:
        # Keep structured payload so re-finalize / UI rebuild don't lose arrays
        # after final_report is replaced with the nested markdown report.
        summary["flow_output"] = flow
    if persistence:
        summary["persistence"] = {
            "total_reconciled_amount": persistence.get("total_reconciled_amount"),
            "total_unmatched_amount": persistence.get("total_unmatched_amount"),
            "total_pending_human_amount": persistence.get("total_pending_human_amount"),
            "total_outstanding_balance": persistence.get("total_outstanding_balance"),
            "results_by_status": persistence.get("results_by_status"),
            "total_results": persistence.get("total_results"),
            "write_off_count": persistence.get("write_off_count"),
            "cross_check_passed": persistence.get("cross_check_passed"),
        }

    existing = db.get_run(run_id)
    if not existing:
        db.conn.execute(
            """INSERT INTO flow_runs
               (id, user_request, ar_ap_mode, status, stages_json, summary_json, final_report, started_at, finished_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                run_id,
                user_request or f"AMP kickoff {kickoff_id}",
                mode,
                status,
                json.dumps(stages),
                json.dumps(summary),
                report,
                _now(),
                _now(),
            ),
        )
        db.conn.commit()
    else:
        db.conn.execute(
            """UPDATE flow_runs
               SET status = ?, stages_json = ?, summary_json = ?, final_report = ?, finished_at = ?
               WHERE id = ?""",
            (status, json.dumps(stages), json.dumps(summary), report, _now(), run_id),
        )
        db.conn.commit()

    db.clear_run_operational(run_id)

    structured_counts: dict[str, int] = {}
    if flow:
        structured_counts = _persist_structured_flow(db, run_id=run_id, flow=flow)
        summary["structured_writes"] = structured_counts

    # Always hydrate demo ERP/bank/PP when the run has no real feed rows.
    # Structured FlowOutput often only sends source aggregates (record_count), which
    # would leave the dashboard at Invoices/Payments = 0.
    feed_sources = [
        s
        for s in db.fetch_sources(run_id)
        if s.get("source") in ("erp", "bank", "payment_processor")
    ]
    if not feed_sources:
        hydrated = _hydrate_demo_sources(db, run_id=run_id, ar_ap_mode=mode)
        if hydrated:
            summary["hydration"] = hydrated
            summary["ingestion"] = {
                "erp": hydrated.get("erp", 0),
                "bank": hydrated.get("bank", 0),
                "payment_processor": hydrated.get("payment_processor", 0),
                "amp_reported": ingestion,
            }
    elif not summary.get("ingestion"):
        summary["ingestion"] = {
            "erp": sum(1 for s in feed_sources if s["source"] == "erp"),
            "bank": sum(1 for s in feed_sources if s["source"] == "bank"),
            "payment_processor": sum(1 for s in feed_sources if s["source"] == "payment_processor"),
        }

    # Ensure pending queue rows exist even if exceptions[] was empty but pending_review_items was filled
    existing_exc = {e["transaction_id"] for e in db.fetch_exceptions(run_id)}
    missing_pending = [r for r in pending_records if r["transaction_id"] not in existing_exc]
    if missing_pending:
        _persist_amp_pending(db, run_id=run_id, records=missing_pending)

    # If structured results missing AUTO_MATCHED rows, materialize from count (legacy)
    results_existing = db.fetch_results(run_id)
    auto_existing = sum(1 for r in results_existing if r.get("final_status") == "AUTO_MATCHED")
    auto_n = int(metrics.get("auto_reconciled") or 0)
    reconciled_amt = float((summary.get("persistence") or {}).get("total_reconciled_amount") or 0)
    if auto_n > auto_existing and not structured_counts.get("results"):
        reserved = {r["transaction_id"] for r in pending_records}
        reconciled_amt = _persist_auto_matched_stubs(
            db, run_id=run_id, count=auto_n - auto_existing, reserved_tids=reserved
        )
        summary.setdefault("persistence", {})
        summary["persistence"]["total_reconciled_amount"] = reconciled_amt
        summary["persistence"]["results_by_status"] = {
            "AUTO_MATCHED": auto_n,
            "HUMAN_REVIEW": identified_pending,
        }
    elif not summary.get("persistence"):
        summary["persistence"] = {
            "total_reconciled_amount": reconciled_amt if auto_n else None,
            "total_unmatched_amount": None,
            "results_by_status": {
                "AUTO_MATCHED": auto_n,
                "HUMAN_REVIEW": identified_pending,
            },
        }

    from src.finance_controller.review_state import pending_review_items

    actual_pending = pending_review_items(db, run_id)
    metrics["pending_review"] = len(actual_pending)
    summary["pending_review"] = len(actual_pending)
    summary["pending_human_count"] = len(actual_pending)
    summary["pending_review_items"] = actual_pending
    if metrics.get("match_rate") is None or metrics.get("match_rate_derived"):
        auto_n = int(metrics.get("auto_reconciled") or 0)
        denom = auto_n + len(actual_pending)
        if denom > 0:
            metrics["match_rate"] = round(auto_n / denom, 4)
            metrics["match_rate_derived"] = True
            summary["evaluation"]["match_rate"] = metrics["match_rate"]
            summary["metrics"] = metrics

    db.conn.execute(
        "UPDATE flow_runs SET summary_json = ? WHERE id = ?",
        (json.dumps(summary), run_id),
    )
    db.conn.commit()
    db.save_metrics(
        {
            **metrics,
            "details": {
                "source": "amp_structured" if flow else "amp_report",
                "kickoff_id": kickoff_id,
                "amp_run_id": remote_run_id,
            },
        },
        run_id=run_id,
    )
    db.audit(
        "amp_finalize",
        f"AMP {kickoff_id} → run {run_id}",
        entity_id=run_id,
        run_id=run_id,
        payload={
            "structured_flow": bool(flow),
            "pending": len(actual_pending),
            "integrity": integrity,
            "writes": structured_counts,
        },
    )
    return {
        "run_id": run_id,
        "status": status,
        "stages": stages,
        "metrics": metrics,
        "report_chars": len(report or ""),
        "structured_flow": bool(flow),
        "data_integrity_warnings": integrity,
    }


def _result_is_synthetic(row: dict[str, Any]) -> bool:
    details = row.get("details") or row.get("details_json") or {}
    if isinstance(details, str):
        try:
            details = json.loads(details)
        except Exception:
            details = {}
    if not isinstance(details, dict):
        return False
    return bool(details.get("synthetic_auto") or details.get("amp_source") and details.get("synthetic_auto"))


def amp_run_is_hollow(db: Any, run_id: str) -> bool:
    """
    True when an AMP finalize did not produce a usable dashboard (the failure mode
    we've been hitting: stages completed, invoices/payments/match empty or junk).
    """
    from src.finance_controller.review_state import pending_review_items

    results = db.fetch_results(run_id)
    candidates = db.fetch_candidates(run_id)
    metrics = db.get_metrics(run_id) or {}
    pending = [
        p
        for p in pending_review_items(db, run_id)
        if _is_real_txn_id(p.get("transaction_id"))
    ]
    auto = sum(
        1
        for r in results
        if str(r.get("final_status") or "") == "AUTO_MATCHED" and not _result_is_synthetic(r)
    )
    # Count non-synthetic results overall
    real_results = [r for r in results if not _result_is_synthetic(r)]
    mr = metrics.get("match_rate")
    acc = metrics.get("accuracy")

    # Usable AMP output: real auto-matches or scored candidates + real pending, with metrics
    if auto >= 1 and mr not in (None, 0, 0.0):
        return False
    if len(candidates) >= 3 and len(real_results) >= 3:
        return False
    if len(candidates) >= 1 and len(pending) >= 1 and mr not in (None, 0, 0.0):
        return False
    if acc not in (None, 0, 0.0) and len(real_results) >= 1:
        return False

    # Hollow: no meaningful recon signal
    return True


def local_fallback_enabled() -> bool:
    return (os.getenv("AMP_LOCAL_FALLBACK") or "1").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


def run_local_fallback_after_amp(
    db: Any,
    *,
    user_request: str = "",
    ar_ap_mode: str = "AR",
    bank_pdf_path: str | None = None,
    amp_run_id: str | None = None,
    kickoff_id: str | None = None,
) -> dict[str, Any]:
    """
    Run the in-process AIFinanceController flow so the dashboard gets real
    invoices/payments/metrics when CrewAI Enterprise returns an empty payload.
    """
    from pathlib import Path

    from src.finance_controller.flow import run_finance_flow

    root = Path(__file__).resolve().parents[2]
    demo = root / "data" / "demo"
    pdf = bank_pdf_path
    if pdf and not str(pdf).startswith(("http://", "https://")):
        p = Path(pdf)
        pdf = str(p.resolve()) if p.is_file() else None

    result = run_finance_flow(
        user_request=user_request
        or "Run AR reconciliation on demo feeds (local fallback after hollow AMP)",
        ar_ap_mode=ar_ap_mode or "AR",
        bank_csv_path=str(demo / "bank.csv"),
        payment_processor_csv_path=str(demo / "payment_processor.csv"),
        ground_truth_csv_path=str(demo / "ground_truth.csv"),
        bank_pdf_path=pdf,
        db_path=getattr(db, "path", None) or str(root / "data" / "finance_controller.db"),
    )
    local_id = result["run_id"]
    run = db.get_run(local_id)
    summary = dict((run or {}).get("summary") or {})
    summary["engine"] = "crewai-local"
    summary["source_of_truth"] = "crewai-local"
    summary["fallback_from_amp"] = amp_run_id
    summary["amp_kickoff_id"] = kickoff_id
    summary["amp_local_fallback"] = True
    db.conn.execute(
        "UPDATE flow_runs SET summary_json = ?, finished_at = ? WHERE id = ?",
        (json.dumps(summary), _now(), local_id),
    )
    db.conn.commit()
    db.audit(
        "amp_local_fallback",
        f"Local flow {local_id} after hollow AMP {amp_run_id or kickoff_id}",
        entity_id=local_id,
        run_id=local_id,
        payload={
            "amp_run_id": amp_run_id,
            "kickoff_id": kickoff_id,
            "match_rate": (result.get("evaluation_metrics") or {}).get("match_rate"),
        },
    )
    return result
