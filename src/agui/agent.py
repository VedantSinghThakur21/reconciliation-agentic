"""AG-UI agent run — LLM plans UI state; builders fill real DB data; EventEncoder streams."""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

# Windows consoles often use cp1252 — CrewAI event emoji must not crash handlers.
os.environ.setdefault("PYTHONIOENCODING", "utf-8")

from ag_ui.core import (
    EventType,
    RunAgentInput,
    RunErrorEvent,
    RunFinishedEvent,
    RunStartedEvent,
    StateSnapshotEvent,
    StepFinishedEvent,
    StepStartedEvent,
    TextMessageContentEvent,
    TextMessageEndEvent,
    TextMessageStartEvent,
)
from ag_ui.encoder import EventEncoder

from src.agents.crewai_client import CrewAIClient, CrewAIClientError, crewai_configured
from src.agui.builders import clarification, compose_workspace
from src.agui.planner import WorkspacePlan, plan_workspace
from src.agui.ui_schema import IntentObject, UIComponent, WorkspaceUI
from src.finance_controller.amp_sync import finalize_amp_run
from src.finance_controller.db import FinanceDB
from src.finance_controller.flow import run_finance_flow

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[2]
DEMO = ROOT / "data" / "demo"
UPLOADS = ROOT / "data" / "uploads"


def _prefer_amp_engine() -> bool:
    """
    AG-UI Ask → reconciliation should show up in CrewAI Enterprise Executions.

    When CREWAI_* credentials exist, kick the deployed crew (same as dashboard AMP).
    Set AGUI_ENGINE=local to force the in-process Flow instead.
    """
    if (os.getenv("AGUI_ENGINE") or "").lower() == "local":
        return False
    return crewai_configured()


def _amp_bank_pdf_input(raw: str | None) -> str | None:
    """Pass http(s) URLs through; bare/local paths become filenames AMP knowledge can resolve."""
    if not raw:
        return None
    s = str(raw).strip()
    if s.startswith(("http://", "https://")):
        return s
    return Path(s).name

# Unique AG-UI step names (never map two flow stages onto the same active step).
STAGE_PROGRESS_LABEL = {
    "conversational_intake": "Understanding request",
    "extract_bank_pdf": "Loading data",
    "ingest_and_validate": "Validating",
    "normalize": "Normalizing",
    "generate_candidates": "Matching",
    "score_candidates": "Scoring",
    "route_decisions": "Routing",
    "investigate_exceptions": "Investigating exceptions",
    "human_review": "Human review band",
    "persist_results": "Persisting results",
    "create_journal_entries": "Creating journals",
    "evaluate": "Evaluating",
    "summary_report": "Preparing response",
}


def _should_execute_reconciliation(plan: WorkspacePlan) -> bool:
    """Every reconciliation request must kick off CrewAI — never serve stale run metrics."""
    if plan.actions.needs_clarification or plan.intent.primary == "clarification":
        return False
    if plan.actions.run_reconciliation:
        return True
    return plan.intent.primary == "reconciliation"


def _props_dict(input_data: RunAgentInput) -> dict[str, Any]:
    raw = getattr(input_data, "forwarded_props", None)
    if raw is None:
        raw = getattr(input_data, "forwardedProps", None)
    if isinstance(raw, dict):
        return raw
    state = getattr(input_data, "state", None)
    if isinstance(state, dict):
        return state
    return {}


def resolve_bank_pdf_path(raw: str | None = None) -> str | None:
    """
    Resolve an uploaded/local bank statement PDF for the CrewAI flow.

    Same path semantics as POST /api/reconcile so PDF payments and UI metrics
    come from one AIFinanceController run. Only resolves an explicitly provided
    path/URL — never invents a PDF from disk.
    """
    if not raw:
        return None
    p = Path(str(raw).strip())
    if p.is_file():
        return str(p.resolve())
    for base in (UPLOADS, DEMO, ROOT / "data" / "synthetic" / "bank_statements"):
        cand = base / p.name
        if cand.is_file():
            return str(cand.resolve())
    # Keep http(s) URLs for AMP; local flow will skip missing files
    if str(raw).startswith(("http://", "https://")):
        return str(raw).strip()
    return None


def _user_text(input_data: RunAgentInput) -> str:
    for msg in reversed(list(input_data.messages or [])):
        role = getattr(msg, "role", None)
        if role == "user":
            content = getattr(msg, "content", None)
            if isinstance(content, str) and content.strip():
                return content.strip()
            if isinstance(content, list):
                parts = []
                for p in content:
                    t = getattr(p, "text", None) if not isinstance(p, dict) else p.get("text")
                    if t:
                        parts.append(str(t))
                if parts:
                    return " ".join(parts).strip()
    return ""


def _emit(encoder: EventEncoder, event: Any) -> str:
    return encoder.encode(event)


class _StepTracker:
    """Ensure AG-UI never gets duplicate STEP_STARTED for an active step."""

    def __init__(self) -> None:
        self.active: set[str] = set()
        self.finished: set[str] = set()

    def start(self, encoder: EventEncoder, name: str) -> str | None:
        if name in self.active or name in self.finished:
            return None
        self.active.add(name)
        return _emit(
            encoder,
            StepStartedEvent(type=EventType.STEP_STARTED, step_name=name),
        )

    def finish(self, encoder: EventEncoder, name: str) -> str | None:
        if name in self.finished:
            return None
        frames: list[str] = []
        if name not in self.active:
            # Finish without start is invalid for some clients — start first.
            started = self.start(encoder, name)
            if started:
                frames.append(started)
        self.active.discard(name)
        self.finished.add(name)
        frames.append(
            _emit(
                encoder,
                StepFinishedEvent(type=EventType.STEP_FINISHED, step_name=name),
            )
        )
        return "".join(frames)


async def stream_workspace_agent(
    input_data: RunAgentInput,
    *,
    accept: str | None = None,
    db: FinanceDB | None = None,
) -> AsyncIterator[str]:
    """Yield official AG-UI SSE frames for a dynamically planned workspace run."""
    encoder = EventEncoder(accept=accept)
    thread_id = input_data.thread_id or f"thread_{uuid.uuid4().hex[:10]}"
    run_id = input_data.run_id or f"run_{uuid.uuid4().hex[:10]}"
    message_id = f"msg_{uuid.uuid4().hex[:10]}"
    finance_db = db or FinanceDB()
    steps = _StepTracker()

    yield _emit(
        encoder,
        RunStartedEvent(type=EventType.RUN_STARTED, thread_id=thread_id, run_id=run_id),
    )

    try:
        text = _user_text(input_data)
        if not text:
            ui = clarification("Send a finance workspace request to continue.")
            async for frame in _finish_with_ui(
                encoder, thread_id, run_id, message_id, ui, "Please provide a request."
            ):
                yield frame
            return

        frame = steps.start(encoder, "Understanding request")
        if frame:
            yield frame
        plan = await asyncio.to_thread(plan_workspace, text)
        frame = steps.finish(encoder, "Understanding request")
        if frame:
            yield frame

        # Emit early plan snapshot (structure only — no fabricated metrics)
        yield _emit(
            encoder,
            StateSnapshotEvent(
                type=EventType.STATE_SNAPSHOT,
                snapshot={
                    "intent": plan.intent.model_dump(),
                    "reasoning": plan.reasoning,
                    "data": {},
                    "ui": {
                        "title": plan.ui.title,
                        "layout": plan.ui.layout,
                        "components": [{"type": c, "data": {}} for c in plan.ui.components],
                    },
                    "workspace": {
                        "intent": plan.intent.model_dump(),
                        "title": plan.ui.title,
                        "status": "ok",
                        "reasoning": plan.reasoning,
                        "components": [],
                        "message": "Planning workspace…",
                    },
                },
            ),
        )

        if plan.actions.needs_clarification or plan.intent.primary == "clarification":
            msg = (
                plan.actions.clarification_question
                or "Could you clarify what you need in the finance workspace?"
            )
            ui = clarification(msg, txn_ref=plan.txn_ref, reasoning=plan.reasoning)
            async for frame in _finish_with_ui(encoder, thread_id, run_id, message_id, ui, msg):
                yield frame
            return

        if _should_execute_reconciliation(plan):
            props = _props_dict(input_data)
            bank_pdf = resolve_bank_pdf_path(
                props.get("bank_pdf_path") or props.get("bankPdfPath")
            )
            engine = "amp" if _prefer_amp_engine() else "local"
            logger.warning(
                "CREWAI_INVOKE reconciliation engine=%s primary=%s run_reconciliation=%s bank_pdf=%s request=%r",
                engine,
                plan.intent.primary,
                plan.actions.run_reconciliation,
                bank_pdf,
                text[:160],
            )
            async for frame in _run_reconciliation_stream(
                encoder,
                finance_db,
                thread_id=thread_id,
                run_id=run_id,
                message_id=message_id,
                user_request=text,
                plan=plan,
                steps=steps,
                bank_pdf_path=bank_pdf,
            ):
                yield frame
            return

        logger.info(
            "CREWAI_SKIP view-only primary=%s (no reconciliation execution) request=%r",
            plan.intent.primary,
            text[:160],
        )
        frame = steps.start(encoder, "Loading data")
        if frame:
            yield frame
        latest = finance_db.latest_dashboard_run()
        current_run_id = latest["id"] if latest else None
        frame = steps.finish(encoder, "Loading data")
        if frame:
            yield frame

        frame = steps.start(encoder, "Preparing response")
        if frame:
            yield frame
        ui = compose_workspace(finance_db, plan, current_run_id)
        ui = ui.model_copy(
            update={
                "data": {
                    **(ui.data or {}),
                    "crewai_executed": False,
                    "source_of_truth": "finance-db",
                    "run_id": current_run_id,
                }
            }
        )
        frame = steps.finish(encoder, "Preparing response")
        if frame:
            yield frame

        frame = steps.start(encoder, "Completed")
        if frame:
            yield frame
        frame = steps.finish(encoder, "Completed")
        if frame:
            yield frame

        summary = ui.message or ui.reasoning or ui.title
        async for frame in _finish_with_ui(encoder, thread_id, run_id, message_id, ui, summary or ""):
            yield frame

    except Exception as e:  # noqa: BLE001
        logger.exception("AG-UI workspace agent failed")
        fail = WorkspaceUI(
            intent=IntentObject(primary="unsupported", secondary=[]),
            title="Agent failure",
            status="agent_failure",
            message=str(e),
            components=[],
        )
        yield _emit(
            encoder,
            StateSnapshotEvent(type=EventType.STATE_SNAPSHOT, snapshot=fail.as_state()),
        )
        yield _emit(
            encoder,
            RunErrorEvent(type=EventType.RUN_ERROR, message=str(e), code="agent_failure"),
        )


def _progress_snapshot(
    plan: WorkspacePlan,
    *,
    title: str,
    run_id: str | None,
    current: str,
    completed: list[str],
    extra: dict[str, Any] | None = None,
) -> WorkspaceUI:
    data = {
        "current": current,
        "completed": completed,
        **(extra or {}),
    }
    return WorkspaceUI(
        intent=IntentObject(
            primary=plan.intent.primary,
            secondary=list(plan.intent.secondary),
        ),
        title=title,
        status="ok",
        reasoning=plan.reasoning,
        run_id=run_id,
        components=[UIComponent(type="agent_progress", data=data)],
        data={"crewai_executed": True, "engine": extra.get("engine") if extra else None},
    )


async def _run_amp_reconciliation_stream(
    encoder: EventEncoder,
    db: FinanceDB,
    *,
    thread_id: str,
    run_id: str,
    message_id: str,
    user_request: str,
    plan: WorkspacePlan,
    steps: _StepTracker,
    bank_pdf_path: str | None = None,
) -> AsyncIterator[str]:
    """Kick off the deployed CrewAI Enterprise crew — same path as dashboard Run + AMP."""
    bank_in = Path(DEMO / "bank.csv").name
    processor_in = Path(DEMO / "payment_processor.csv").name
    gt_in = Path(DEMO / "ground_truth.csv").name
    pdf_in = _amp_bank_pdf_input(bank_pdf_path)
    user_req = user_request
    if pdf_in and str(pdf_in).startswith("http") and pdf_in not in user_req:
        user_req = (
            f"{user_req.rstrip('.')}. Bank PDF is at {pdf_in}. "
            f"Bank CSV at {bank_in}, payment processor at {processor_in}, "
            f"ground truth at {gt_in}."
        )

    frame = steps.start(encoder, "Kicking off CrewAI AMP")
    if frame:
        yield frame

    result_box: dict[str, Any] = {}
    error_box: dict[str, BaseException] = {}

    def _kickoff() -> None:
        try:
            logger.warning(
                "CREWAI_AMP_KICKOFF user_request=%r bank_pdf=%s",
                user_req[:160],
                pdf_in,
            )
            client = CrewAIClient()
            inputs = {
                "user_request": user_req,
                "ar_ap_mode": "AR",
                "bank_csv_path": bank_in,
                "payment_processor_csv_path": processor_in,
                "ground_truth_csv_path": gt_in,
            }
            if pdf_in:
                inputs["bank_pdf_path"] = pdf_in
            kid = client.kickoff(inputs, meta={"source": "agui-workspace"})
            result_box["amp"] = {
                "engine": "crewai-amp",
                "kickoff_id": kid,
                "inputs_sent": inputs,
                "status": "running",
            }
        except BaseException as e:  # noqa: BLE001
            logger.exception("CREWAI_AMP_KICKOFF_FAILED")
            error_box["error"] = e

    worker = threading.Thread(target=_kickoff, name="amp-kickoff", daemon=True)
    worker.start()
    while worker.is_alive():
        await asyncio.sleep(0.05)
    worker.join(timeout=1)

    if error_box.get("error"):
        err = error_box["error"]
        yield _emit(
            encoder,
            RunErrorEvent(
                type=EventType.RUN_ERROR,
                message=str(err),
                code="amp_kickoff_failed",
            ),
        )
        return

    amp = result_box.get("amp") or {}
    kickoff_id = str(amp.get("kickoff_id") or "")
    flow_run_id = f"AMP-{kickoff_id[:8].upper()}" if kickoff_id else None
    frame = steps.finish(encoder, "Kicking off CrewAI AMP")
    if frame:
        yield frame

    if not kickoff_id:
        yield _emit(
            encoder,
            RunErrorEvent(
                type=EventType.RUN_ERROR,
                message="AMP kickoff returned no kickoff_id",
                code="amp_kickoff_failed",
            ),
        )
        return

    logger.warning("CREWAI_AMP_QUEUED kickoff_id=%s run_id=%s", kickoff_id, flow_run_id)

    # Persist lightweight run row so History / dashboard can track it while AMP runs
    if flow_run_id and not db.get_run(flow_run_id):
        import json as _json
        from datetime import datetime, timezone

        db.conn.execute(
            """INSERT OR REPLACE INTO flow_runs
               (id, user_request, ar_ap_mode, status, stages_json, summary_json, final_report, started_at, finished_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                flow_run_id,
                user_request,
                "AR",
                "running",
                _json.dumps(
                    [
                        {"name": "crewai_amp_kickoff", "status": "completed"},
                        {"name": "crewai_amp_status", "status": "running"},
                    ]
                ),
                _json.dumps(
                    {
                        "kickoff_id": kickoff_id,
                        "engine": "crewai-amp",
                        "inputs": amp.get("inputs_sent"),
                    }
                ),
                "",
                datetime.now(timezone.utc).isoformat(),
                None,
            ),
        )
        db.conn.commit()
        db.audit(
            "amp_kickoff",
            f"AMP kickoff {kickoff_id}",
            entity_id=flow_run_id,
            run_id=flow_run_id,
            payload=amp.get("inputs_sent"),
        )

    frame = steps.start(encoder, "Waiting on CrewAI Enterprise")
    if frame:
        yield frame

    max_wait = float(
        os.getenv("CREWAI_MAX_WAIT_SEC") or os.getenv("CREWAI_AMP_MAX_WAIT_SEC") or "600"
    )
    poll_every = float(os.getenv("CREWAI_POLL_INTERVAL_SEC") or "5")
    deadline = asyncio.get_event_loop().time() + max_wait
    last_state = ""
    status_payload: dict[str, Any] = {}

    from src.agents.crewai_client import _terminal_kind

    client = CrewAIClient()
    while asyncio.get_event_loop().time() < deadline:
        try:
            _http, _raw, status_payload = await asyncio.to_thread(client.status, kickoff_id, retries=2)
        except CrewAIClientError as e:
            logger.info("AMP status transient: %s", e)
            await asyncio.sleep(poll_every)
            continue

        state = str(status_payload.get("state") or status_payload.get("status") or "")
        if state != last_state:
            last_state = state
            label = state or "RUNNING"
            snap = _progress_snapshot(
                plan,
                title=plan.ui.title or "CrewAI Enterprise running",
                run_id=flow_run_id,
                current=f"AMP {label}",
                completed=sorted(steps.finished),
                extra={"engine": "crewai-amp", "kickoff_id": kickoff_id, "amp_state": label},
            )
            yield _emit(
                encoder,
                StateSnapshotEvent(type=EventType.STATE_SNAPSHOT, snapshot=snap.as_state()),
            )

        kind = _terminal_kind(status_payload)
        if kind == "success":
            break
        if kind == "failure":
            frame = steps.finish(encoder, "Waiting on CrewAI Enterprise")
            if frame:
                yield frame
            yield _emit(
                encoder,
                RunErrorEvent(
                    type=EventType.RUN_ERROR,
                    message=f"AMP run failed: {state}",
                    code="amp_failed",
                ),
            )
            return
        await asyncio.sleep(poll_every)
    else:
        frame = steps.finish(encoder, "Waiting on CrewAI Enterprise")
        if frame:
            yield frame
        yield _emit(
            encoder,
            RunErrorEvent(
                type=EventType.RUN_ERROR,
                message=f"Timed out waiting for AMP kickoff {kickoff_id}",
                code="amp_timeout",
            ),
        )
        return

    frame = steps.finish(encoder, "Waiting on CrewAI Enterprise")
    if frame:
        yield frame

    frame = steps.start(encoder, "Finalizing AMP results")
    if frame:
        yield frame

    finalized = finalize_amp_run(
        db,
        run_id=flow_run_id or f"AMP-{kickoff_id[:8].upper()}",
        kickoff_id=kickoff_id,
        amp_status=status_payload,
        user_request=user_request,
        ar_ap_mode="AR",
    )
    flow_run_id = finalized["run_id"]
    flow_metrics = finalized.get("metrics") or {}
    engine_label = "crewai-amp"
    source_of_truth = "crewai-amp"
    logger.warning(
        "CREWAI_AMP_DONE kickoff_id=%s run_id=%s match_rate=%s",
        kickoff_id,
        flow_run_id,
        flow_metrics.get("match_rate"),
    )

    from src.finance_controller.amp_sync import (
        amp_run_is_hollow,
        local_fallback_enabled,
        run_local_fallback_after_amp,
    )

    if local_fallback_enabled() and amp_run_is_hollow(db, flow_run_id):
        frame = steps.finish(encoder, "Finalizing AMP results")
        if frame:
            yield frame
        frame = steps.start(encoder, "Local reconciliation fallback")
        if frame:
            yield frame
        logger.warning(
            "AMP_HOLLOW_FALLBACK kickoff_id=%s amp_run=%s → local flow",
            kickoff_id,
            flow_run_id,
        )
        local = await asyncio.to_thread(
            run_local_fallback_after_amp,
            db,
            user_request=user_request,
            ar_ap_mode="AR",
            bank_pdf_path=resolve_bank_pdf_path(bank_pdf_path),
            amp_run_id=flow_run_id,
            kickoff_id=kickoff_id,
        )
        flow_run_id = local["run_id"]
        flow_metrics = local.get("evaluation_metrics") or {}
        engine_label = "crewai-local"
        source_of_truth = "crewai-local"
        frame = steps.finish(encoder, "Local reconciliation fallback")
        if frame:
            yield frame
    else:
        frame = steps.finish(encoder, "Finalizing AMP results")
        if frame:
            yield frame

    frame = steps.start(encoder, "Completed")
    if frame:
        yield frame
    ui = compose_workspace(db, plan, flow_run_id)
    ui = ui.model_copy(
        update={
            "data": {
                **(ui.data or {}),
                "source_of_truth": source_of_truth,
                "crewai_executed": True,
                "engine": engine_label,
                "kickoff_id": kickoff_id,
                "amp_local_fallback": engine_label == "crewai-local",
                "run_id": flow_run_id,
                "evaluation": {
                    k: flow_metrics.get(k)
                    for k in (
                        "accuracy",
                        "precision",
                        "recall",
                        "f1",
                        "match_rate",
                        "false_match_rate",
                    )
                },
                "bank_pdf_path": pdf_in,
            }
        }
    )
    frame = steps.finish(encoder, "Completed")
    if frame:
        yield frame
    async for frame in _finish_with_ui(
        encoder,
        thread_id,
        run_id,
        message_id,
        ui,
        ui.reasoning
        or ui.title
        or (
            f"Local fallback after AMP ({kickoff_id[:8]}…) → {flow_run_id}"
            if engine_label == "crewai-local"
            else f"CrewAI Enterprise finished ({kickoff_id[:8]}…). Check Executions for this kickoff."
        ),
        result={
            "run_id": flow_run_id,
            "kickoff_id": kickoff_id,
            "evaluation": (ui.data or {}).get("evaluation"),
            "source_of_truth": source_of_truth,
            "amp_local_fallback": engine_label == "crewai-local",
            **ui.as_state(),
        },
    ):
        yield frame

async def _run_reconciliation_stream(
    encoder: EventEncoder,
    db: FinanceDB,
    *,
    thread_id: str,
    run_id: str,
    message_id: str,
    user_request: str,
    plan: WorkspacePlan,
    steps: _StepTracker,
    bank_pdf_path: str | None = None,
) -> AsyncIterator[str]:
    if _prefer_amp_engine():
        async for frame in _run_amp_reconciliation_stream(
            encoder,
            db,
            thread_id=thread_id,
            run_id=run_id,
            message_id=message_id,
            user_request=user_request,
            plan=plan,
            steps=steps,
            bank_pdf_path=bank_pdf_path,
        ):
            yield frame
        return

    result_box: dict[str, Any] = {}
    error_box: dict[str, BaseException] = {}
    pdf_path = resolve_bank_pdf_path(bank_pdf_path)

    def _worker() -> None:
        try:
            logger.warning(
                "CREWAI_FLOW_START AIFinanceController.kickoff bank_csv=%s bank_pdf=%s db=%s",
                DEMO / "bank.csv",
                pdf_path,
                db.path,
            )
            result_box["result"] = run_finance_flow(
                user_request=user_request,
                ar_ap_mode="AR",
                bank_csv_path=str(DEMO / "bank.csv"),
                payment_processor_csv_path=str(DEMO / "payment_processor.csv"),
                ground_truth_csv_path=str(DEMO / "ground_truth.csv"),
                bank_pdf_path=pdf_path,
                db_path=db.path,
            )
            res = result_box["result"]
            logger.warning(
                "CREWAI_FLOW_DONE run_id=%s match_rate=%s accuracy=%s pdf=%s",
                res.get("run_id"),
                (res.get("evaluation_metrics") or {}).get("match_rate"),
                (res.get("evaluation_metrics") or {}).get("accuracy"),
                pdf_path,
            )
        except BaseException as e:  # noqa: BLE001
            logger.exception("CREWAI_FLOW_FAILED")
            error_box["error"] = e

    worker = threading.Thread(target=_worker, name="fc-flow", daemon=True)
    worker.start()

    # Planning already finished "Understanding request" — do not re-START it.
    # Map flow stage conversational_intake onto a distinct label for progress UX.
    flow_labels = dict(STAGE_PROGRESS_LABEL)
    flow_labels["conversational_intake"] = "Starting reconciliation"

    while worker.is_alive():
        latest = db.latest_run()
        if latest:
            for stage in latest.get("stages") or []:
                name = str(stage.get("name") or "")
                status = str(stage.get("status") or "")
                label = flow_labels.get(name, name)
                if not label:
                    continue
                if status == "running":
                    frame = steps.start(encoder, label)
                    if frame:
                        yield frame
                if status == "completed":
                    # Ensure start then finish (idempotent)
                    frame = steps.start(encoder, label)
                    if frame:
                        yield frame
                    frame = steps.finish(encoder, label)
                    if frame:
                        yield frame
                    progress_ui = _progress_snapshot(
                        plan,
                        title=plan.ui.title or "Reconciliation in progress",
                        run_id=latest.get("id"),
                        current=label,
                        completed=sorted(steps.finished),
                        extra={"engine": "crewai-local"},
                    )
                    yield _emit(
                        encoder,
                        StateSnapshotEvent(
                            type=EventType.STATE_SNAPSHOT,
                            snapshot=progress_ui.as_state(),
                        ),
                    )
        await asyncio.sleep(0.05)

    worker.join(timeout=1)

    if error_box.get("error"):
        err = error_box["error"]
        yield _emit(
            encoder,
            RunErrorEvent(type=EventType.RUN_ERROR, message=str(err), code="reconciliation_failed"),
        )
        return

    result = result_box.get("result") or {}
    flow_run_id = result.get("run_id")
    # Authoritative metrics from the same CrewAI evaluate → summary_report path
    flow_metrics = result.get("evaluation_metrics") or {}
    frame = steps.start(encoder, "Completed")
    if frame:
        yield frame
    ui = compose_workspace(db, plan, flow_run_id)
    # Stamp run-aligned metrics into data so UI/report consumers share one source
    ui = ui.model_copy(
        update={
            "data": {
                **(ui.data or {}),
                "source_of_truth": "crewai-local",
                "crewai_executed": True,
                "engine": "crewai-local",
                "run_id": flow_run_id,
                "evaluation": {
                    k: flow_metrics.get(k)
                    for k in (
                        "accuracy",
                        "precision",
                        "recall",
                        "f1",
                        "match_rate",
                        "false_match_rate",
                    )
                },
                "bank_pdf_path": pdf_path,
                "final_report_chars": len(str(result.get("final_report") or "")),
            }
        }
    )
    frame = steps.finish(encoder, "Completed")
    if frame:
        yield frame
    async for frame in _finish_with_ui(
        encoder,
        thread_id,
        run_id,
        message_id,
        ui,
        ui.reasoning or ui.title or f"Reconciliation finished ({flow_run_id}).",
        result={
            "run_id": flow_run_id,
            "evaluation": (ui.data or {}).get("evaluation"),
            "source_of_truth": "crewai-local",
            **ui.as_state(),
        },
    ):
        yield frame


async def _finish_with_ui(
    encoder: EventEncoder,
    thread_id: str,
    run_id: str,
    message_id: str,
    ui: WorkspaceUI,
    text: str,
    result: dict[str, Any] | None = None,
) -> AsyncIterator[str]:
    yield _emit(
        encoder,
        StateSnapshotEvent(type=EventType.STATE_SNAPSHOT, snapshot=ui.as_state()),
    )
    yield _emit(
        encoder,
        TextMessageStartEvent(
            type=EventType.TEXT_MESSAGE_START,
            message_id=message_id,
            role="assistant",
        ),
    )
    yield _emit(
        encoder,
        TextMessageContentEvent(
            type=EventType.TEXT_MESSAGE_CONTENT,
            message_id=message_id,
            delta=text,
        ),
    )
    yield _emit(
        encoder,
        TextMessageEndEvent(type=EventType.TEXT_MESSAGE_END, message_id=message_id),
    )
    yield _emit(
        encoder,
        RunFinishedEvent(
            type=EventType.RUN_FINISHED,
            thread_id=thread_id,
            run_id=run_id,
            result=result or ui.as_state(),
        ),
    )
