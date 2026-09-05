from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

os.environ.setdefault("CREWAI_TRACING_ENABLED", "false")

ROOT = Path(__file__).resolve().parents[2]
DEMO = ROOT / "data" / "demo"


def _load_dotenv() -> None:
    env_path = ROOT / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


_load_dotenv()

from src.agents.crewai_client import (  # noqa: E402
    CrewAIClient,
    CrewAIClientError,
    crewai_configured,
)
from src.finance_controller.crewai_amp import run_amp_flow  # noqa: E402

# Compat aliases used below
CrewAIAmpClient = CrewAIClient
CrewAIAmpError = CrewAIClientError
amp_enabled = crewai_configured


def _crewai_url() -> str:
    return os.getenv("CREWAI_API_URL") or os.getenv("CREWAI_AMP_URL") or ""
from src.finance_controller.db import FinanceDB  # noqa: E402
from src.finance_controller.flow import run_finance_flow  # noqa: E402

app = FastAPI(
    title="AI Finance Controller",
    description="CrewAI Flow — local + AMP deployed reconciliation",
    version="0.3.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

_db = FinanceDB()


def _resolve_engine(requested: str | None = None) -> str:
    mode = (requested or os.getenv("RECON_ENGINE") or "auto").lower()
    if mode == "auto":
        return "amp" if amp_enabled() else "local"
    if mode in ("amp", "local"):
        return mode
    return "local"


class RunRequest(BaseModel):
    user_request: str = "Run AR reconciliation on demo feeds"
    ar_ap_mode: Literal["AP", "AR", "BOTH"] = "AR"
    bank_csv_path: str | None = None
    payment_processor_csv_path: str | None = None
    ground_truth_csv_path: str | None = None
    bank_pdf_path: str | None = None
    engine: Literal["auto", "local", "amp"] | None = None
    wait: bool = False  # AMP runs are long — kickoff returns immediately; poll /api/amp/status/{id}


class ResolveRequest(BaseModel):
    decision: str = Field(..., description="APPROVED | REJECTED | RESOLVED | ESCALATED")
    note: str | None = None


class QARequest(BaseModel):
    question: str
    run_id: str | None = None


@app.get("/api/health")
def health() -> dict[str, Any]:
    engine = _resolve_engine()
    return {
        "status": "ok",
        "service": "ai-finance-controller",
        "engine": f"crewai-{engine}",
        "amp_configured": amp_enabled(),
        "amp_configured_url": bool(_crewai_url()),
    }


@app.get("/api/amp/inputs")
def amp_inputs() -> dict[str, Any]:
    if not amp_enabled():
        raise HTTPException(400, "AMP not configured")
    try:
        return {"inputs": CrewAIAmpClient().get_inputs()}
    except CrewAIAmpError as e:
        raise HTTPException(502, str(e)) from e


@app.get("/api/amp/status/{kickoff_id}")
def amp_status(kickoff_id: str) -> dict[str, Any]:
    if not amp_enabled():
        raise HTTPException(400, "AMP not configured")
    try:
        return CrewAIAmpClient().status(kickoff_id)[2]
    except CrewAIAmpError as e:
        raise HTTPException(502, str(e)) from e


@app.get("/api/sources")
def get_sources() -> dict[str, Any]:
    bank = DEMO / "bank.csv"
    pp = DEMO / "payment_processor.csv"
    erp_count = 24
    bank_count = sum(1 for _ in bank.open(encoding="utf-8")) - 1 if bank.exists() else 0
    pp_count = sum(1 for _ in pp.open(encoding="utf-8")) - 1 if pp.exists() else 0
    amp = amp_enabled()
    return {
        "sources": [
            {
                "id": "erp",
                "label": "ERP (stub)",
                "mode": "amp" if amp else "demo",
                "count": erp_count,
                "kind": "invoices",
                "status": "connected",
                "live": amp,
            },
            {
                "id": "bank",
                "label": "Bank",
                "mode": "amp" if amp else "demo",
                "count": max(bank_count, 0),
                "kind": "payments",
                "status": "connected",
                "live": amp,
            },
            {
                "id": "payment_processor",
                "label": "Payment Processor",
                "mode": "amp" if amp else "demo",
                "count": max(pp_count, 0),
                "kind": "payments",
                "status": "connected",
                "live": amp,
            },
        ],
        "amp": {
            "configured": amp,
            "engine": _resolve_engine(),
        },
    }


@app.post("/api/reconcile")
def start_reconcile(body: RunRequest) -> dict[str, Any]:
    engine = _resolve_engine(body.engine)
    bank = body.bank_csv_path or str(DEMO / "bank.csv")
    processor = body.payment_processor_csv_path or str(DEMO / "payment_processor.csv")
    gt = body.ground_truth_csv_path or str(DEMO / "ground_truth.csv")

    if engine == "amp":
        # Deployed crew expects path-like inputs; send demo filenames the AMP workspace can resolve.
        bank_in = Path(bank).name
        processor_in = Path(processor).name
        gt_in = Path(gt).name
        try:
            amp = run_amp_flow(
                user_request=body.user_request,
                ar_ap_mode=body.ar_ap_mode,
                bank_csv_path=bank_in,
                payment_processor_csv_path=processor_in,
                ground_truth_csv_path=gt_in,
                bank_pdf_path=body.bank_pdf_path,
                poll=body.wait,
                max_wait_sec=float(
                    os.getenv("CREWAI_MAX_WAIT_SEC")
                    or os.getenv("CREWAI_AMP_MAX_WAIT_SEC")
                    or "600"
                ),
            )
        except CrewAIAmpError as e:
            raise HTTPException(502, str(e)) from e

        run_id = f"AMP-{amp['kickoff_id'][:8].upper()}"
        # Persist a lightweight AMP run record for the UI
        existing = _db.get_run(run_id)
        if not existing:
            # create_run generates its own id — insert manually
            from datetime import datetime, timezone
            import json as _json

            _db.conn.execute(
                """INSERT OR REPLACE INTO flow_runs
                   (id, user_request, ar_ap_mode, status, stages_json, summary_json, final_report, started_at, finished_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    run_id,
                    body.user_request,
                    body.ar_ap_mode,
                    amp.get("status") or "running",
                    _json.dumps([
                        {"name": "crewai_amp_kickoff", "status": "completed"},
                        {"name": "crewai_amp_status", "status": "completed" if body.wait else "running"},
                    ]),
                    _json.dumps({"kickoff_id": amp["kickoff_id"], "engine": "crewai-amp", "inputs": amp.get("inputs_sent")}),
                    str(amp.get("final_output") or ""),
                    datetime.now(timezone.utc).isoformat(),
                    datetime.now(timezone.utc).isoformat() if body.wait else None,
                ),
            )
            _db.conn.commit()
            _db.audit("amp_kickoff", f"AMP kickoff {amp['kickoff_id']}", entity_id=run_id, payload=amp.get("inputs_sent"))

        final_output = amp.get("final_output")
        return {
            "run_id": run_id,
            "kickoff_id": amp["kickoff_id"],
            "engine": "crewai-amp",
            "confirmation_message": f"Kicked off deployed CrewAI AMP flow ({amp['kickoff_id']}).",
            "stages": [
                {"name": "crewai_amp_kickoff", "status": "completed"},
                {"name": "crewai_amp_wait", "status": "completed" if body.wait else "running"},
            ],
            "summary": {
                "engine": "crewai-amp",
                "kickoff_id": amp["kickoff_id"],
                "status": amp.get("status"),
            },
            "metrics": {},
            "amp_result": amp.get("amp_result"),
            "final_report": final_output if isinstance(final_output, str) else _safe_json(final_output),
            "inputs_sent": amp.get("inputs_sent"),
        }

    # Local deterministic CrewAI Flow
    result = run_finance_flow(
        user_request=body.user_request,
        ar_ap_mode=body.ar_ap_mode,
        bank_csv_path=bank,
        payment_processor_csv_path=processor,
        ground_truth_csv_path=gt,
        bank_pdf_path=body.bank_pdf_path,
    )
    m = result["evaluation_metrics"]
    return {
        "run_id": result["run_id"],
        "engine": "crewai-local",
        "confirmation_message": result["confirmation_message"],
        "stages": result["stages"],
        "summary": {
            "auto_reconciled": result.get("persistence_summary", {}).get("results_by_status", {}).get("AUTO_MATCHED", 0),
            "pending_review": result.get("human_review_summary", {}).get("total_reviewed", 0),
            "match_rate": m.get("match_rate"),
            "accuracy": m.get("accuracy"),
            "f1": m.get("f1"),
            "precision": m.get("precision"),
            "recall": m.get("recall"),
            **(result.get("persistence_summary") or {}),
        },
        "metrics": m,
        "journals": result.get("journal_entries_summary"),
        "investigation": result.get("investigation_summary"),
        "final_report": result.get("final_report"),
    }


def _safe_json(obj: Any) -> str:
    import json

    try:
        return json.dumps(obj, indent=2, default=str)
    except Exception:
        return str(obj)


@app.get("/api/runs")
def list_runs(limit: int = 20) -> dict[str, Any]:
    return {"runs": _db.list_runs(limit)}


@app.get("/api/runs/latest")
def latest_run() -> dict[str, Any]:
    run = _db.latest_run()
    if not run:
        raise HTTPException(404, "No runs yet")
    return _payload(run["id"])


@app.get("/api/runs/{run_id}")
def get_run(run_id: str) -> dict[str, Any]:
    run = _db.get_run(run_id)
    if not run:
        raise HTTPException(404, f"Run not found: {run_id}")
    return _payload(run_id)


@app.get("/api/transactions")
def transactions(source: str | None = None) -> dict[str, Any]:
    return {"transactions": _db.fetch_sources(source)}


@app.get("/api/candidates")
def candidates() -> dict[str, Any]:
    return {"candidates": _db.fetch_candidates()}


@app.get("/api/exceptions")
def exceptions() -> dict[str, Any]:
    return {"exceptions": _db.fetch_exceptions()}


@app.get("/api/reviews")
def reviews() -> dict[str, Any]:
    return {"reviews": _db.fetch_reviews()}


@app.get("/api/results")
def results() -> dict[str, Any]:
    return {"results": _db.fetch_results()}


@app.get("/api/journals")
def journals() -> dict[str, Any]:
    return {"journals": _db.fetch_journals(), "writebacks": _db.fetch_writebacks()}


@app.get("/api/metrics")
def metrics() -> dict[str, Any]:
    run = _db.latest_run()
    return {"run_id": run["id"] if run else None, "metrics": _db.latest_metrics(), "summary": (run or {}).get("summary")}


@app.get("/api/audit")
def audit(limit: int = 100) -> dict[str, Any]:
    return {"events": _db.fetch_audit(limit)}


@app.get("/api/report")
def report() -> dict[str, Any]:
    run = _db.latest_run()
    if not run:
        raise HTTPException(404, "No runs yet")
    return {"run_id": run["id"], "report": run.get("final_report") or ""}


@app.post("/api/exceptions/{exception_id}/resolve")
def resolve_exception(exception_id: str, body: ResolveRequest) -> dict[str, Any]:
    decision = body.decision.upper()
    mapping = {
        "CONFIRMED": "APPROVED",
        "APPROVED": "APPROVED",
        "REJECTED": "REJECTED",
        "RESOLVED": "RESOLVED",
        "ESCALATED": "ESCALATED",
    }
    mapped = mapping.get(decision)
    if not mapped:
        raise HTTPException(400, f"Invalid decision: {body.decision}")

    exc = next((e for e in _db.fetch_exceptions() if e["id"] == exception_id), None)
    if not exc:
        raise HTTPException(404, f"Exception not found: {exception_id}")

    from src.finance_controller.utils import new_id

    _db.insert_reviews([{
        "id": new_id("REV"),
        "transaction_id": exc["transaction_id"],
        "decision": mapped,
        "reason": body.note or f"Manual UI decision: {mapped}",
        "confidence": exc.get("confidence") or 0,
    }])
    _db.audit("exception_resolved", f"{exception_id} → {mapped}", entity_id=exception_id, payload=body.model_dump())
    return {
        "id": exception_id,
        "payment_id": exc["transaction_id"],
        "status": {"APPROVED": "confirmed", "REJECTED": "rejected", "RESOLVED": "resolved", "ESCALATED": "rejected"}[mapped],
        "reason": exc.get("exception_type"),
        "confidence": exc.get("confidence") or 0,
        "amount_applied": 0,
        "run_id": (_db.latest_run() or {}).get("id"),
        "created_at": exc.get("created_at"),
    }


@app.post("/api/qa")
def qa(body: QARequest) -> dict[str, Any]:
    run = _db.get_run(body.run_id) if body.run_id else _db.latest_run()
    if not run:
        return {"answer": "No flow runs yet. Start reconciliation first.", "run_id": None, "question": body.question}

    rid = run["id"]
    m = _db.latest_metrics() or {}
    exc = _db.fetch_exceptions()
    results = _db.fetch_results()
    journals = _db.fetch_journals()
    q = body.question.lower()

    if "amp" in q or "deploy" in q:
        answer = (
            f"AMP configured={amp_enabled()} (credentials from env only). "
            f"Active engine={_resolve_engine()}."
        )
    elif "journal" in q:
        posted = sum(1 for j in journals if j["status"] == "POSTED")
        answer = f"{len(journals)} journal entries ({posted} POSTED, {len(journals) - posted} DRAFT)."
    elif "exception" in q or "unresolved" in q or "pending" in q:
        answer = f"{len(exc)} exceptions classified. Review decisions are in human_reviews for run {rid}."
    elif "accuracy" in q or "f1" in q or "precision" in q or "recall" in q:
        answer = (
            f"Accuracy={m.get('accuracy')}, precision={m.get('precision')}, "
            f"recall={m.get('recall')}, F1={m.get('f1')}, false-match rate={m.get('false_match_rate')}."
        )
    elif "match" in q:
        answer = f"Match rate {float(m.get('match_rate') or 0):.1%} across {len(results)} reconciliation results."
    elif "cash" in q or "amount" in q:
        summary = run.get("summary") or {}
        pers = summary.get("persistence") or {}
        answer = (
            f"Reconciled amount INR {pers.get('total_reconciled_amount', '—')}, "
            f"unmatched INR {pers.get('total_unmatched_amount', '—')}."
        )
    elif run.get("final_report") and ("report" in q or "summary" in q):
        answer = str(run.get("final_report"))[:1200]
    else:
        answer = (
            f"Run {rid} ({run.get('ar_ap_mode')}): accuracy={m.get('accuracy')}, "
            f"match rate={m.get('match_rate')}, exceptions={len(exc)}, journals={len(journals)}. "
            f"Ask about match rate, accuracy, exceptions, journals, AMP, or cash."
        )

    _db.audit("qa", f"Q: {body.question}", entity_id=rid, payload={"answer": answer})
    return {"answer": answer, "run_id": rid, "question": body.question}


def _payload(run_id: str) -> dict[str, Any]:
    run = _db.get_run(run_id)
    assert run is not None
    metrics = _db.latest_metrics()
    sources = _db.fetch_sources()
    results = _db.fetch_results()
    invoices = [s for s in sources if s["source"] == "erp"]
    payments = [s for s in sources if s["source"] in ("bank", "payment_processor")]
    ui_results = [
        {
            "id": r["id"],
            "payment_id": r["transaction_id"],
            "invoice_id": r.get("matched_txn_id"),
            "status": "reconciled" if r["final_status"] in (
                "AUTO_MATCHED", "HUMAN_APPROVED", "HUMAN_RESOLVED", "EXCEPTION_RESOLVED"
            ) else "pending_review",
            "confidence": r.get("confidence") or 0,
            "tier": r.get("strategy"),
            "amount_applied": r.get("reconciled_amount") or 0,
            "source": r.get("final_status"),
            "final_status": r.get("final_status"),
        }
        for r in results
    ]
    ui_tx = []
    for s in sources:
        ui_tx.append({
            "id": s["id"],
            "run_id": run_id,
            "source": s["source"],
            "kind": "invoice" if s["source"] == "erp" else "payment",
            "external_id": s["transaction_id"],
            "party_name": s.get("merchant_norm") or s.get("merchant"),
            "amount": s.get("amount_norm") or s.get("amount") or 0,
            "currency": s.get("currency_norm") or s.get("currency") or "INR",
            "txn_date": s.get("date_norm") or s.get("txn_date"),
            "reference": s.get("reference_norm") or s.get("reference"),
            "raw": s,
        })
    exceptions = _db.fetch_exceptions()
    ui_exceptions = [
        {
            "id": e["id"],
            "run_id": run_id,
            "payment_id": e["transaction_id"],
            "invoice_id": None,
            "confidence": e.get("confidence") or 0,
            "reason": e.get("exception_type"),
            "reasoning": f"{e.get('exception_type')}: {e.get('recommended_action')} | {e.get('details_json')}",
            "amount_applied": 0,
            "status": "pending",
            "created_at": e.get("created_at"),
        }
        for e in exceptions
    ]
    reviews = {r["transaction_id"]: r for r in _db.fetch_reviews()}
    for ex in ui_exceptions:
        rev = reviews.get(ex["payment_id"])
        if rev:
            ex["status"] = {
                "APPROVED": "confirmed",
                "REJECTED": "rejected",
                "RESOLVED": "resolved",
                "ESCALATED": "rejected",
            }.get(rev["decision"], "pending")
            ex["decided_at"] = rev.get("created_at")

    cash = {
        "cash_applied": (run.get("summary") or {}).get("persistence", {}).get("total_reconciled_amount"),
        "open_ar": None,
        "unapplied_cash": (run.get("summary") or {}).get("persistence", {}).get("total_unmatched_amount"),
        "net_exposure": None,
        "invoice_count": len(invoices),
        "payment_count": len(payments),
    }
    if cash["cash_applied"] is not None and cash["unapplied_cash"] is not None:
        erp_total = sum(float(i.get("amount") or 0) for i in ui_tx if i["kind"] == "invoice")
        cash["open_ar"] = round(erp_total - float(cash["cash_applied"]), 2)
        cash["net_exposure"] = round(float(cash["open_ar"]) - float(cash["unapplied_cash"]), 2)

    return {
        "run": {
            **run,
            "mode": run.get("ar_ap_mode") or "AR",
            "use_ai": True,
        },
        "metrics": {
            **(metrics or {}),
            "auto_reconciled": sum(1 for r in ui_results if r["status"] == "reconciled"),
            "pending_review": sum(1 for e in ui_exceptions if e["status"] == "pending"),
            "total_payments": len(payments),
            "unreconciled_amount": cash.get("unapplied_cash"),
            "details": {
                "cash_position": cash,
                "ai_enabled": True,
                "engine": (run.get("summary") or {}).get("engine") or "crewai",
                "confusion": {
                    "tp": (metrics or {}).get("tp"),
                    "fp": (metrics or {}).get("fp"),
                    "fn": (metrics or {}).get("fn"),
                    "tn": (metrics or {}).get("tn"),
                },
            },
        } if metrics else {
            "details": {
                "cash_position": cash,
                "engine": (run.get("summary") or {}).get("engine") or "crewai-amp",
            }
        },
        "exceptions": ui_exceptions,
        "results": ui_results,
        "transactions": ui_tx,
        "candidates": _db.fetch_candidates(),
        "journals": _db.fetch_journals(),
        "writebacks": _db.fetch_writebacks(),
        "reviews": _db.fetch_reviews(),
        "audit": _db.fetch_audit(80),
        "report": run.get("final_report"),
        "cash_position": cash,
    }
