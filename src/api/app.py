from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
import logging

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
        # Project .env wins over stale shell defaults (e.g. RECON_ENGINE=local)
        os.environ[key.strip()] = val.strip().strip('"').strip("'")


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


class ClassifyRequest(BaseModel):
    text: str


@app.get("/api/health")
def health() -> dict[str, Any]:
    engine = _resolve_engine()
    if os.getenv("GEMINI_API_KEY"):
        classifier = "gemini"
    elif os.getenv("OPENAI_API_KEY"):
        classifier = "openai"  # may fall back to heuristic on auth errors
    else:
        classifier = "heuristic"
    return {
        "status": "ok",
        "service": "ai-finance-controller",
        "engine": f"crewai-{engine}",
        "amp_configured": amp_enabled(),
        "amp_configured_url": bool(_crewai_url()),
        "classifier": classifier,
    }


def _amp_bank_pdf_input(raw: str | None) -> str | None:
    """Pass http(s) URLs through intact; bare names stay as filenames for AMP knowledge."""
    if not raw:
        return None
    s = raw.strip()
    if s.startswith("http://") or s.startswith("https://"):
        return s
    return Path(s).name


@app.post("/api/uploads/bank-pdf")
async def upload_bank_pdf(file: UploadFile = File(...)) -> dict[str, Any]:
    """Store a bank statement PDF locally and publish a public URL for AMP fetch."""
    name = Path(file.filename or "bank_statement.pdf").name
    if not name.lower().endswith(".pdf"):
        raise HTTPException(400, "Only PDF files are accepted")
    upload_dir = ROOT / "data" / "uploads"
    upload_dir.mkdir(parents=True, exist_ok=True)
    dest = upload_dir / name
    raw = await file.read()
    if not raw:
        raise HTTPException(400, "Empty upload")
    dest.write_bytes(raw)

    # Best-effort local text preview (AMP OCR handles scans)
    preview: dict[str, Any] = {"local_text_extract": None, "records": 0, "error": None}
    try:
        from src.ingestion.pdf_loader import parse_payment_pdf

        rows = parse_payment_pdf(dest)
        preview["local_text_extract"] = "ok"
        preview["records"] = len(rows)
    except Exception as e:  # noqa: BLE001
        preview["local_text_extract"] = "failed"
        preview["error"] = str(e)
        preview["note"] = "Scanned PDFs are handled by CrewAI AMP once bank_pdf_path is a public URL"

    # AMP cannot read local disk — publish a temporary public URL for bank_pdf_path
    public_url: str | None = None
    public_error: str | None = None
    try:
        from src.ingestion.public_upload import publish_public_url

        public_url = publish_public_url(dest, filename=name)
    except Exception as e:  # noqa: BLE001
        public_error = str(e)

    bank_pdf_path = public_url or name
    _db.audit(
        "bank_pdf_upload",
        f"Uploaded {name}",
        entity_id=name,
        payload={**preview, "public_url": public_url, "public_error": public_error},
    )
    return {
        "filename": name,
        "path": str(dest),
        "bank_pdf_path": bank_pdf_path,  # public URL when publish succeeds
        "public_url": public_url,
        "public_error": public_error,
        "bytes": len(raw),
        "preview": preview,
        "ocr": "CrewAI AMP fetches bank_pdf_path when it is a public https URL",
    }


@app.get("/api/uploads/bank-pdf")
def list_bank_pdfs() -> dict[str, Any]:
    upload_dir = ROOT / "data" / "uploads"
    files = []
    if upload_dir.exists():
        for p in sorted(upload_dir.glob("*.pdf")):
            files.append({"filename": p.name, "path": str(p), "bytes": p.stat().st_size})
    return {"files": files}


@app.post("/api/assistant/classify")
def assistant_classify(body: ClassifyRequest) -> dict[str, Any]:
    from src.assistant.classify import classify_intent

    result = classify_intent(body.text or "")
    return {
        "intent": result["intent"],
        "txn_ref": result.get("txn_ref"),
        "source": result.get("source"),
        "llm_intent": result.get("llm_intent"),
        "text": body.text,
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


class AmpFinalizeRequest(BaseModel):
    run_id: str | None = None
    user_request: str | None = None
    ar_ap_mode: Literal["AP", "AR", "BOTH"] = "AR"


@app.post("/api/amp/finalize/{kickoff_id}")
def amp_finalize(kickoff_id: str, body: AmpFinalizeRequest | None = None) -> dict[str, Any]:
    """
    Promote a completed AMP kickoff into the dashboard for this run_id.

    If AMP returns a hollow payload (no usable matches/metrics), fall back to the
    local AIFinanceController flow so the dashboard still populates.
    """
    if not amp_enabled():
        raise HTTPException(400, "AMP not configured")
    body = body or AmpFinalizeRequest()
    try:
        status_payload = CrewAIAmpClient().status(kickoff_id)[2]
    except CrewAIAmpError as e:
        raise HTTPException(502, str(e)) from e

    from src.finance_controller.amp_sync import (
        amp_run_is_hollow,
        finalize_amp_run,
        local_fallback_enabled,
        run_local_fallback_after_amp,
    )

    run_id = body.run_id or f"AMP-{kickoff_id[:8].upper()}"
    out = finalize_amp_run(
        _db,
        run_id=run_id,
        kickoff_id=kickoff_id,
        amp_status=status_payload,
        user_request=body.user_request or "",
        ar_ap_mode=body.ar_ap_mode,
    )

    if local_fallback_enabled() and amp_run_is_hollow(_db, run_id):
        logging.getLogger("reconq.api").warning(
            "AMP_HOLLOW_FALLBACK kickoff_id=%s amp_run=%s → local flow",
            kickoff_id,
            run_id,
        )
        local = run_local_fallback_after_amp(
            _db,
            user_request=body.user_request or "",
            ar_ap_mode=body.ar_ap_mode,
            amp_run_id=run_id,
            kickoff_id=kickoff_id,
        )
        local_id = local["run_id"]
        payload = _payload(local_id)
        return {
            **out,
            **payload,
            "run_id": local_id,
            "engine": "crewai-local",
            "amp_local_fallback": True,
            "amp_run_id": run_id,
            "kickoff_id": kickoff_id,
            "metrics": payload.get("metrics") or local.get("evaluation_metrics"),
            "confirmation_message": (
                f"AMP {kickoff_id[:8]}… returned empty dashboard data — "
                f"ran local reconciliation ({local_id})."
            ),
        }

    return {**out, **_payload(run_id)}



@app.post("/api/agui")
async def agui_workspace(request: Request) -> StreamingResponse:
    """
    Official AG-UI RunAgentInput endpoint (ag-ui-protocol).

    Streams typed AG-UI events + StateSnapshot with controlled WorkspaceUI schema.
    Uses existing CrewAI Flow + FinanceDB — no fabricated metrics.
    """
    from ag_ui.core import RunAgentInput
    from ag_ui.encoder import EventEncoder
    from src.agui.agent import stream_workspace_agent

    body = await request.json()
    input_data = RunAgentInput.model_validate(body)
    accept = request.headers.get("accept")
    encoder = EventEncoder(accept=accept)

    return StreamingResponse(
        stream_workspace_agent(input_data, accept=accept, db=_db),
        media_type=encoder.get_content_type(),
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/api/agui/stream/{kickoff_id}")
async def agui_stream(kickoff_id: str) -> StreamingResponse:
    """
    AG-UI SSE progress for a CrewAI AMP kickoff.

    Additive only — existing /api/amp/status polling remains the source of truth
    for final results. This stream never mutates pipeline state.
    """
    from src.agui.stream import stream_kickoff_progress

    kid = kickoff_id.strip()
    if not kid:
        raise HTTPException(400, "kickoff_id required")

    return StreamingResponse(
        stream_kickoff_progress(kid),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


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
            "bank_pdf_ocr": "CrewAI AMP OCR when bank_pdf_path is set",
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
        pdf_in = _amp_bank_pdf_input(body.bank_pdf_path)
        # Prefer caller text; if a PDF URL is set, make sure the request mentions it.
        user_req = body.user_request
        if pdf_in and pdf_in.startswith("http") and pdf_in not in user_req:
            user_req = (
                f"{user_req.rstrip('.')}. Bank PDF is at {pdf_in}. "
                f"Bank CSV at {bank_in}, payment processor at {processor_in}, "
                f"ground truth at {gt_in}."
            )
        try:
            amp = run_amp_flow(
                user_request=user_req,
                ar_ap_mode=body.ar_ap_mode,
                bank_csv_path=bank_in,
                payment_processor_csv_path=processor_in,
                ground_truth_csv_path=gt_in,
                bank_pdf_path=pdf_in,
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

    # Local deterministic CrewAI Flow — resolve uploaded PDF to absolute path when present
    from src.agui.agent import resolve_bank_pdf_path

    local_pdf = resolve_bank_pdf_path(body.bank_pdf_path)
    logger = logging.getLogger("reconq.api")
    logger.info(
        "CREWAI_INVOKE /api/reconcile engine=crewai-local bank_pdf=%s request=%r",
        local_pdf,
        (body.user_request or "")[:160],
    )

    result = run_finance_flow(
        user_request=body.user_request,
        ar_ap_mode=body.ar_ap_mode,
        bank_csv_path=bank,
        payment_processor_csv_path=processor,
        ground_truth_csv_path=gt,
        bank_pdf_path=local_pdf,
    )
    m = result["evaluation_metrics"]
    return {
        "run_id": result["run_id"],
        "engine": "crewai-local",
        "confirmation_message": result["confirmation_message"],
        "stages": result["stages"],
        "summary": {
            "auto_reconciled": result.get("persistence_summary", {}).get("results_by_status", {}).get("AUTO_MATCHED", 0),
            "pending_review": result.get("human_review_summary", {}).get("pending_human_count")
            or result.get("human_review_summary", {}).get("pending_hitl")
            or 0,
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


def _active_run_id(explicit: str | None = None) -> str | None:
    if explicit:
        return explicit
    run = _db.latest_dashboard_run() or _db.latest_run()
    return run["id"] if run else None


@app.get("/api/runs")
def list_runs(limit: int = 20) -> dict[str, Any]:
    active = _db.latest_dashboard_run()
    return {
        "runs": _db.list_runs(limit),
        "active_run_id": active["id"] if active else None,
    }


@app.get("/api/runs/latest")
def latest_run() -> dict[str, Any]:
    rid = _active_run_id()
    if not rid:
        raise HTTPException(404, "No runs yet")
    return _payload(rid)


@app.get("/api/runs/{run_id}")
def get_run(run_id: str) -> dict[str, Any]:
    run = _db.get_run(run_id)
    if not run:
        raise HTTPException(404, f"Run not found: {run_id}")
    return _payload(run_id)


@app.get("/api/transactions")
def transactions(source: str | None = None, run_id: str | None = None) -> dict[str, Any]:
    rid = _active_run_id(run_id)
    if not rid:
        return {"transactions": [], "run_id": None}
    return {"transactions": _db.fetch_sources(rid, source), "run_id": rid}


@app.get("/api/candidates")
def candidates(run_id: str | None = None) -> dict[str, Any]:
    rid = _active_run_id(run_id)
    if not rid:
        return {"candidates": [], "run_id": None}
    return {"candidates": _db.fetch_candidates(rid), "run_id": rid}


@app.get("/api/exceptions")
def exceptions(run_id: str | None = None) -> dict[str, Any]:
    rid = _active_run_id(run_id)
    if not rid:
        return {"exceptions": [], "run_id": None}
    return {"exceptions": _db.fetch_exceptions(rid), "run_id": rid}


@app.get("/api/reviews")
def reviews(run_id: str | None = None) -> dict[str, Any]:
    rid = _active_run_id(run_id)
    if not rid:
        return {"reviews": [], "run_id": None}
    return {"reviews": _db.fetch_reviews(rid), "run_id": rid}


@app.get("/api/results")
def results(run_id: str | None = None) -> dict[str, Any]:
    rid = _active_run_id(run_id)
    if not rid:
        return {"results": [], "run_id": None}
    return {"results": _db.fetch_results(rid), "run_id": rid}


@app.get("/api/journals")
def journals(run_id: str | None = None) -> dict[str, Any]:
    rid = _active_run_id(run_id)
    if not rid:
        return {"journals": [], "writebacks": [], "run_id": None}
    return {
        "journals": _db.fetch_journals(rid),
        "writebacks": _db.fetch_writebacks(rid),
        "run_id": rid,
    }


@app.get("/api/metrics")
def metrics(run_id: str | None = None) -> dict[str, Any]:
    rid = _active_run_id(run_id)
    if not rid:
        return {"run_id": None, "metrics": None, "summary": None}
    run = _db.get_run(rid)
    return {
        "run_id": rid,
        "metrics": _db.get_metrics(rid),
        "summary": (run or {}).get("summary"),
    }


@app.get("/api/audit")
def audit(limit: int = 100, run_id: str | None = None) -> dict[str, Any]:
    rid = _active_run_id(run_id)
    return {"events": _db.fetch_audit(rid, limit), "run_id": rid}


@app.get("/api/report")
def report(run_id: str | None = None) -> dict[str, Any]:
    """CrewAI final_report for the active (or requested) run — same evaluation embedded in the UI."""
    rid = _active_run_id(run_id)
    if not rid:
        raise HTTPException(404, "No runs yet")
    run = _db.get_run(rid)
    if not run:
        raise HTTPException(404, f"Run not found: {rid}")
    summary = run.get("summary") or {}
    evaluation = summary.get("evaluation") or summary.get("metrics") or {}
    metrics = dict(_db.get_metrics(rid) or {})
    # Prefer run summary (written by CrewAI summary_report) over orphan metric rows
    if evaluation:
        for k in ("accuracy", "precision", "recall", "f1", "match_rate", "false_match_rate"):
            if evaluation.get(k) is not None:
                metrics[k] = evaluation.get(k)
    return {
        "run_id": rid,
        "report": run.get("final_report") or "",
        "evaluation": {
            k: metrics.get(k)
            for k in ("accuracy", "precision", "recall", "f1", "match_rate", "false_match_rate")
        },
        "source_of_truth": (summary.get("source_of_truth") or summary.get("engine") or "crewai-local"),
    }


@app.post("/api/exceptions/{exception_id}/resolve")
def resolve_exception(exception_id: str, body: ResolveRequest) -> dict[str, Any]:
    decision = body.decision.upper()
    mapping = {
        "CONFIRMED": "APPROVED",
        "APPROVED": "APPROVED",
        "APPROVE": "APPROVED",
        "REJECTED": "REJECTED",
        "REJECT": "REJECTED",
        "RESOLVED": "RESOLVED",
        "RESOLVE": "RESOLVED",
        "ESCALATED": "ESCALATED",
        "ESCALATE": "ESCALATED",
        "WRITE_OFF": "WRITE_OFF",
        "WRITEOFF": "WRITE_OFF",
    }
    mapped = mapping.get(decision)
    if not mapped:
        raise HTTPException(400, f"Invalid decision: {body.decision}")

    exc = _db.get_exception(exception_id)
    if not exc:
        raise HTTPException(404, f"Exception not found: {exception_id}")

    rid = exc.get("run_id") or _active_run_id()
    if not rid:
        raise HTTPException(404, "No run associated with exception")

    status_ui = {
        "APPROVED": "confirmed",
        "REJECTED": "rejected",
        "RESOLVED": "resolved",
        "ESCALATED": "rejected",
        "WRITE_OFF": "resolved",
    }

    from src.finance_controller.utils import new_id

    review = _db.apply_human_decision(
        run_id=rid,
        transaction_id=exc["transaction_id"],
        decision=mapped,
        reason=body.note or f"Manual UI decision: {mapped}",
        confidence=float(exc.get("confidence") or 0),
        review_id=new_id("REV"),
    )
    if review.get("created"):
        _db.audit(
            "exception_resolved",
            f"{exception_id} → {mapped}",
            entity_id=exception_id,
            payload=body.model_dump(),
            run_id=rid,
        )

    from src.agui.builders import build_reconciliation_result

    workspace = build_reconciliation_result(_db, rid).as_state()
    decided = str(review.get("decision") or mapped)
    return {
        "id": exception_id,
        "payment_id": exc["transaction_id"],
        "status": status_ui.get(decided, "pending"),
        "reason": exc.get("exception_type"),
        "confidence": exc.get("confidence") or 0,
        "amount_applied": 0,
        "run_id": rid,
        "created_at": review.get("created_at") or exc.get("created_at"),
        "human_decision": decided,
        "idempotent": not review.get("created"),
        "workspace": workspace,
    }


@app.post("/api/qa")
def qa(body: QARequest) -> dict[str, Any]:
    rid = _active_run_id(body.run_id)
    run = _db.get_run(rid) if rid else None
    if not run or not rid:
        return {"answer": "No flow runs yet. Start reconciliation first.", "run_id": None, "question": body.question}

    m = _db.get_metrics(rid) or {}
    exc = _db.fetch_exceptions(rid)
    results = _db.fetch_results(rid)
    journals = _db.fetch_journals(rid)
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

    _db.audit("qa", f"Q: {body.question}", entity_id=rid, payload={"answer": answer}, run_id=rid)
    return {"answer": answer, "run_id": rid, "question": body.question}


def _payload(run_id: str) -> dict[str, Any]:
    run = _db.get_run(run_id)
    assert run is not None
    metrics = dict(_db.get_metrics(run_id) or {})
    summary = run.get("summary") or {}
    # Align dashboard metrics with the CrewAI summary_report for this run (PDF/UI same truth)
    for key in ("evaluation", "metrics"):
        block = summary.get(key)
        if isinstance(block, dict):
            for k in (
                "accuracy", "precision", "recall", "f1", "match_rate", "false_match_rate",
                "auto_reconciled", "pending_review", "exception_count",
            ):
                if block.get(k) is not None:
                    metrics[k] = block.get(k)
    sources = _db.fetch_sources(run_id)
    results = _db.fetch_results(run_id)
    invoices = [s for s in sources if s["source"] == "erp"]
    payments = [s for s in sources if s["source"] in ("bank", "payment_processor")]
    ui_results = [
        {
            "id": r["id"],
            "run_id": run_id,
            "payment_id": r["transaction_id"],
            "invoice_id": r.get("matched_txn_id"),
            "status": (
                "reconciled"
                if r["final_status"]
                in (
                    "AUTO_MATCHED",
                    "HUMAN_APPROVED",
                    "HUMAN_RESOLVED",
                    "EXCEPTION_RESOLVED",
                )
                else "pending_review"
                if r["final_status"] in ("HUMAN_REVIEW", "PENDING")
                else "unreconciled"
            ),
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
    exceptions = _db.fetch_exceptions(run_id)
    reviews = {r["transaction_id"]: r for r in _db.fetch_reviews(run_id)}
    sources_by_tid = {s["transaction_id"]: s for s in sources}
    from src.finance_controller.review_state import pending_review_items, reconciliation_summary

    pending_items = pending_review_items(_db, run_id)
    recon_summary = reconciliation_summary(_db, run_id)
    ui_exceptions = [
        {
            "id": e["id"],
            "run_id": run_id,
            "payment_id": e["transaction_id"],
            "invoice_id": None,
            "confidence": e.get("confidence") or 0,
            "reason": e.get("exception_type"),
            "reasoning": f"{e.get('exception_type')}: {e.get('recommended_action')} | {e.get('details_json')}",
            "amount_applied": float(
                (sources_by_tid.get(e["transaction_id"]) or {}).get("amount_norm")
                or (sources_by_tid.get(e["transaction_id"]) or {}).get("amount")
                or 0
            ),
            "source": (sources_by_tid.get(e["transaction_id"]) or {}).get("source"),
            "ai_recommendation": e.get("recommended_action"),
            "human_decision": (reviews.get(e["transaction_id"]) or {}).get("decision"),
            "status": "pending",
            "created_at": e.get("created_at"),
        }
        for e in exceptions
    ]
    for ex in ui_exceptions:
        rev = reviews.get(ex["payment_id"])
        if rev:
            ex["status"] = {
                "APPROVED": "confirmed",
                "REJECTED": "rejected",
                "RESOLVED": "resolved",
                "ESCALATED": "rejected",
                "WRITE_OFF": "resolved",
            }.get(rev["decision"], "pending")
            ex["decided_at"] = rev.get("created_at")
            ex["human_decision"] = rev["decision"]

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

    summary = run.get("summary") or {}
    amp_metrics = summary.get("metrics") if isinstance(summary.get("metrics"), dict) else {}
    engine = summary.get("engine") or ("crewai-amp" if str(run_id).startswith("AMP-") else "crewai")
    is_amp = engine == "crewai-amp" or summary.get("source_of_truth") == "crewai-amp"
    merged = {**(metrics or {}), **(amp_metrics if is_amp else {})}

    return {
        "run": {
            **run,
            "mode": run.get("ar_ap_mode") or "AR",
            "use_ai": True,
        },
        "metrics": {
            **(merged or {}),
            "auto_reconciled": (
                recon_summary.get("auto_matched")
                if recon_summary.get("total_results")
                else (
                    merged.get("auto_reconciled")
                    if is_amp and merged.get("auto_reconciled") is not None
                    else sum(1 for r in ui_results if r["status"] == "reconciled")
                )
            ),
            "pending_review": len(pending_items),
            "total_payments": len(payments),
            "unreconciled_amount": cash.get("unapplied_cash"),
            "details": {
                "cash_position": cash,
                "ai_enabled": True,
                "engine": "crewai-amp" if is_amp else engine,
                "source_of_truth": "crewai-amp" if is_amp else (summary.get("source_of_truth") or engine),
                "kickoff_id": summary.get("kickoff_id") if is_amp else None,
                "confusion": {
                    "tp": (merged or {}).get("tp"),
                    "fp": (merged or {}).get("fp"),
                    "fn": (merged or {}).get("fn"),
                    "tn": (merged or {}).get("tn"),
                },
            },
        } if merged else {
            "details": {
                "cash_position": cash,
                "engine": "crewai-amp" if is_amp else engine,
            }
        },
        "exceptions": ui_exceptions,
        "pending_review_items": pending_items,
        "reconciliation_summary": recon_summary,
        "results": ui_results,
        "transactions": ui_tx,
        "candidates": _db.fetch_candidates(run_id),
        "journals": _db.fetch_journals(run_id),
        "writebacks": _db.fetch_writebacks(run_id),
        "reviews": _db.fetch_reviews(run_id),
        "audit": _db.fetch_audit(run_id, 80 if not is_amp else 40),
        "report": run.get("final_report"),
        "cash_position": cash,
    }
